"""
每日操作日報爬蟲
- scrape_daily_changes: 從 etfinfo.tw/active 抓取加碼/減碼/新增/刪除明細
- save_changes_snapshot: 將日報快照寫入 DB etf_changes_history
"""
import json
from datetime import date, timedelta

from db import get_db, taipei_today
from scrapers.holdings import safe_get
from bs4 import BeautifulSoup


def save_holdings_snapshot(etf_code, holdings):
    """每次成功爬取持股後儲存快照，供期間 diff 使用。"""
    if not holdings:
        return
    try:
        today = taipei_today().isoformat()
        conn = get_db()
        if not conn:
            return
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO etf_holdings_snapshot (etf_code, snapshot_date, holdings_json)
                VALUES (%s, %s, %s)
                ON CONFLICT (etf_code, snapshot_date) DO UPDATE SET
                    holdings_json = EXCLUDED.holdings_json, scraped_at = NOW()
            """, (etf_code, today, json.dumps(holdings, ensure_ascii=False)))
            conn.commit()
            print(f"[DB] {etf_code} {today} 持股快照已儲存")
        finally:
            conn.close()
    except Exception as e:
        print(f"[DB] save_holdings_snapshot 失敗: {e}")


def compute_holdings_diff(old_holdings, new_holdings, old_date, new_date):
    """比較兩份持股清單，以 pct（權重%）為指標，回傳 changes dict。"""
    old_map = {h["code"]: h for h in old_holdings if h.get("code")}
    new_map = {h["code"]: h for h in new_holdings if h.get("code")}
    changes = []

    for code, nh in new_map.items():
        new_pct = float(nh.get("pct", 0))
        if code not in old_map:
            changes.append({"code": code, "name": nh.get("name", ""),
                             "shares": 0, "pct_change": round(new_pct, 3),
                             "amount": f"+{new_pct:.2f}%", "type": "新增"})
        else:
            old_pct = float(old_map[code].get("pct", 0))
            diff = round(new_pct - old_pct, 3)
            if abs(diff) < 0.01:
                continue
            changes.append({"code": code, "name": nh.get("name", ""),
                             "shares": 0, "pct_change": diff,
                             "amount": f"{'+' if diff > 0 else ''}{diff:.2f}%",
                             "type": "加碼" if diff > 0 else "減碼"})

    for code, oh in old_map.items():
        if code not in new_map:
            old_pct = float(oh.get("pct", 0))
            changes.append({"code": code, "name": oh.get("name", ""),
                             "shares": 0, "pct_change": -old_pct,
                             "amount": f"-{old_pct:.2f}%", "type": "刪除"})

    changes.sort(key=lambda x: abs(x.get("pct_change", 0)), reverse=True)
    return {
        "date_range": f"{old_date} → {new_date}",
        "add":    sum(1 for c in changes if c["type"] == "新增"),
        "buy":    sum(1 for c in changes if c["type"] == "加碼"),
        "sell":   sum(1 for c in changes if c["type"] == "減碼"),
        "remove": sum(1 for c in changes if c["type"] == "刪除"),
        "buy_amount": 0.0, "sell_amount": 0.0,
        "changes": changes
    }


def get_period_diff(etf_code, since_date):
    """
    取最新持股快照 vs since_date 當日（或其後最近）快照的 diff。
    since_date: date 物件（週一 or 月初）
    """
    conn = get_db()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT snapshot_date, holdings_json FROM etf_holdings_snapshot
            WHERE etf_code=%s ORDER BY snapshot_date DESC LIMIT 1
        """, (etf_code,))
        row_latest = cur.fetchone()
        if not row_latest:
            return None
        cur.execute("""
            SELECT snapshot_date, holdings_json FROM etf_holdings_snapshot
            WHERE etf_code=%s AND snapshot_date >= %s AND snapshot_date < %s
            ORDER BY snapshot_date ASC LIMIT 1
        """, (etf_code, since_date.isoformat(), str(row_latest[0])))
        row_base = cur.fetchone()
        if not row_base:
            return None
        return compute_holdings_diff(
            json.loads(row_base[1]), json.loads(row_latest[1]),
            str(row_base[0]), str(row_latest[0])
        )
    except Exception as e:
        print(f"[DB] get_period_diff 失敗: {e}")
        return None
    finally:
        conn.close()


def seed_period_snapshots(etf_code, holdings):
    """
    以當前持股建立歷史參考快照（首次執行或無舊快照時）。
    種子日期：本週一、本月一日、上月一日。
    ON CONFLICT DO NOTHING 確保冪等。
    """
    if not holdings:
        return
    today = taipei_today()
    monday = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    if month_start.month == 1:
        prev_month = month_start.replace(year=month_start.year - 1, month=12)
    else:
        prev_month = month_start.replace(month=month_start.month - 1)

    seed_dates = {monday, month_start, prev_month} - {today}
    if not seed_dates:
        return

    holdings_json = json.dumps(holdings, ensure_ascii=False)
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        seeded = []
        for sd in sorted(seed_dates):
            cur.execute("""
                INSERT INTO etf_holdings_snapshot (etf_code, snapshot_date, holdings_json)
                VALUES (%s, %s, %s)
                ON CONFLICT (etf_code, snapshot_date) DO NOTHING
            """, (etf_code, sd.isoformat(), holdings_json))
            if cur.rowcount > 0:
                seeded.append(str(sd))
        conn.commit()
        if seeded:
            print(f"[DB] {etf_code} 歷史種子快照已建立: {seeded}")
    except Exception as e:
        print(f"[DB] seed_period_snapshots 失敗: {e}")
    finally:
        conn.close()


def _nuxt_resolve(data, ref, depth=0, seen=None):
    """展開 Nuxt devalue 格式的索引參照為實際巢狀結構。"""
    if seen is None:
        seen = set()
    if depth > 12:
        return None
    if isinstance(ref, int) and 0 <= ref < len(data):
        if ref in seen:
            return None
        val = data[ref]
        if isinstance(val, (str, int, float, bool)) or val is None:
            return val
        if isinstance(val, list):
            return [_nuxt_resolve(data, v, depth + 1, seen | {ref}) for v in val]
        if isinstance(val, dict):
            return {k: _nuxt_resolve(data, v, depth + 1, seen | {ref}) for k, v in val.items()}
        return val
    return ref


_CHANGE_TYPE_LABEL = {"added": "新增", "increased": "加碼", "decreased": "減碼", "removed": "刪除"}
_CHANGE_TYPE_KEY = {"新增": "add", "加碼": "buy", "減碼": "sell", "刪除": "remove"}


def scrape_daily_changes(etf_code):
    """
    從 etfinfo.tw/active 頁面的 __NUXT_DATA__ 解析最新一次持股揭露的異動明細。
    （改用結構化資料而非頁面文字，避免頁面上其他數字（如天數篩選按鈕）與異動筆數混在一起被誤判）
    """
    url = f"https://www.etfinfo.tw/etf/{etf_code}/active"
    r = safe_get(url, timeout=12)
    if not r:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    nuxt = soup.find("script", id="__NUXT_DATA__")
    if not nuxt or not nuxt.string:
        return None

    try:
        data = json.loads(nuxt.string)
    except Exception as e:
        print(f"[操作日報] NUXT JSON 解析失敗 {etf_code}: {e}")
        return None

    cache_key = f"active-changes-{etf_code}"
    entry_idx = None
    for item in data:
        if isinstance(item, dict) and cache_key in item:
            entry_idx = item[cache_key]
            break
    if entry_idx is None:
        return None

    payload = _nuxt_resolve(data, entry_idx) or {}
    latest = payload.get("latestDiff") or {}
    from_date, to_date = latest.get("fromDate", ""), latest.get("toDate", "")

    result = {
        "date_range": f"{from_date} → {to_date}" if from_date and to_date else "",
        "add": 0, "buy": 0, "sell": 0, "remove": 0,
        "buy_amount": 0.0, "sell_amount": 0.0, "changes": []
    }

    for ch in (latest.get("changes") or []):
        type_str = _CHANGE_TYPE_LABEL.get(ch.get("type", ""))
        if not type_str:
            continue
        shares = int((ch.get("sharesDelta") or 0) / 1000)
        result["changes"].append({
            "code": ch.get("code", ""), "name": ch.get("name", ""),
            "shares": shares, "amount": "", "type": type_str
        })
        result[_CHANGE_TYPE_KEY[type_str]] += 1

    return result if (result["changes"] or result["date_range"]) else None


def save_changes_snapshot(etf_code, changes):
    """將當日操作日報快照 UPSERT 至 etf_changes_history"""
    if not changes:
        return
    try:
        trade_date = taipei_today().isoformat()
        conn = get_db()
        if not conn:
            return
        try:
            cur = conn.cursor()  # Bug 4 修正：pg8000 cursor 不支援 context manager
            cur.execute("""
                INSERT INTO etf_changes_history
                    (etf_code, trade_date, date_range, add_count, buy_count,
                     sell_count, remove_count, buy_amount, sell_amount, changes_json)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (etf_code, trade_date) DO UPDATE SET
                    date_range=EXCLUDED.date_range,
                    add_count=EXCLUDED.add_count, buy_count=EXCLUDED.buy_count,
                    sell_count=EXCLUDED.sell_count, remove_count=EXCLUDED.remove_count,
                    buy_amount=EXCLUDED.buy_amount, sell_amount=EXCLUDED.sell_amount,
                    changes_json=EXCLUDED.changes_json, created_at=CURRENT_TIMESTAMP
            """, (
                etf_code, trade_date,
                changes.get("date_range",""),
                changes.get("add",0), changes.get("buy",0),
                changes.get("sell",0), changes.get("remove",0),
                changes.get("buy_amount",0), changes.get("sell_amount",0),
                json.dumps(changes.get("changes",[]), ensure_ascii=False)
            ))
            conn.commit()
            print(f"[DB] {etf_code} {trade_date} 操作日報快照已儲存")
        finally:
            conn.close()
    except Exception as e:
        print(f"[DB] save_changes_snapshot 失敗: {e}")


def get_period_buy_sell_summary(etf_code, period_start, period_end):
    """彙總期間內加碼/減碼統計（依 etf_changes_history 每日快照加總）。"""
    empty = {"buy": 0, "sell": 0, "buy_amount": 0.0, "sell_amount": 0.0, "stocks": []}
    conn = get_db()
    if not conn:
        return empty
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT buy_count, sell_count, buy_amount, sell_amount, changes_json
            FROM etf_changes_history
            WHERE etf_code=%s AND trade_date >= %s AND trade_date <= %s
            ORDER BY trade_date ASC
        """, (etf_code, str(period_start), str(period_end)))
        rows = cur.fetchall()
        # 過濾掉損壞的快照（異動筆數與明細對不上，例如舊版爬蟲留下的壞資料），避免污染加總
        valid_rows = []
        for r in rows:
            try:
                row_changes_len = len(json.loads(r[4] or "[]"))
            except Exception:
                row_changes_len = 0
            if ((r[0] or 0) + (r[1] or 0)) > 0 and row_changes_len == 0:
                continue
            valid_rows.append(r)
        rows = valid_rows
        buy = sum(r[0] or 0 for r in rows)
        sell = sum(r[1] or 0 for r in rows)
        buy_amount = sum(float(r[2] or 0) for r in rows)
        sell_amount = sum(float(r[3] or 0) for r in rows)
        stocks = []
        for r in rows:
            try:
                stocks += [c for c in json.loads(r[4] or "[]") if c.get("type") in ("加碼", "減碼")]
            except Exception:
                pass
        return {"buy": buy, "sell": sell, "buy_amount": round(buy_amount, 2),
                "sell_amount": round(sell_amount, 2), "stocks": stocks}
    except Exception as e:
        print(f"[DB] get_period_buy_sell_summary 失敗: {e}")
        return empty
    finally:
        conn.close()
