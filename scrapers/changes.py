"""
每日操作日報爬蟲
- scrape_daily_changes: 從 etfinfo.tw/active 抓取加碼/減碼/新增/刪除明細
- save_changes_snapshot: 將日報快照寫入 DB etf_changes_history
"""
import re
import json
from datetime import date, timedelta

from db import get_db
from scrapers.holdings import safe_get
from bs4 import BeautifulSoup


def save_holdings_snapshot(etf_code, holdings):
    """每次成功爬取持股後儲存快照，供期間 diff 使用。"""
    if not holdings:
        return
    try:
        today = date.today().isoformat()
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
    today = date.today()
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


def scrape_daily_changes(etf_code):
    """從 etfinfo.tw/active 抓取最新操作日報"""
    url = f"https://www.etfinfo.tw/etf/{etf_code}/active"
    r = safe_get(url, timeout=12)
    if not r:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    result = {
        "date_range": "", "add": 0, "buy": 0, "sell": 0, "remove": 0,
        "buy_amount": 0.0, "sell_amount": 0.0, "changes": []
    }
    text = soup.get_text()

    date_m = re.search(r"(\d{4}-\d{2}-\d{2})\s*[→\->]+\s*(\d{4}-\d{2}-\d{2})", text)
    if date_m:
        result["date_range"] = f"{date_m.group(1)} → {date_m.group(2)}"

    for key, pattern in [("add","新增"), ("buy","加碼"), ("sell","減碼"), ("remove","刪除")]:
        m = re.search(pattern + r"\s*(\d+)", text)
        if m:
            result[key] = int(m.group(1))

    amt_m = re.search(r"加碼\s*\+([\d.]+)\s*億.*?減碼\s*-([\d.]+)\s*億", text, re.DOTALL)
    if amt_m:
        result["buy_amount"]  = float(amt_m.group(1))
        result["sell_amount"] = float(amt_m.group(2))

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cols = row.find_all("td")
            if len(cols) < 2:
                continue
            try:
                cell0 = cols[0].get_text(strip=True)
                code_m2 = re.match(r"(\d{4,6})", cell0)
                if not code_m2:
                    continue
                code = code_m2.group(1)
                name = cell0.replace(code, "").strip()
                cell1 = cols[1].get_text(strip=True)
                shares_m = re.search(r"([+-][\d,]+)\s*張", cell1)
                shares = int(shares_m.group(1).replace(",","")) if shares_m else 0
                amount_m = re.search(r"([+-][\d.]+\s*億|[+-][\d,]+\s*萬)", cell1)
                amount_str = amount_m.group(1).strip() if amount_m else ""
                type_str = cols[2].get_text(strip=True) if len(cols) > 2 else ""
                if type_str not in ["新增","加碼","減碼","刪除"]:
                    type_str = "加碼" if shares > 0 else "減碼" if shares < 0 else ""
                if code and type_str:
                    result["changes"].append({
                        "code": code, "name": name,
                        "shares": shares, "amount": amount_str, "type": type_str
                    })
            except Exception:
                continue

    return result if (result["changes"] or result["date_range"]) else None


def save_changes_snapshot(etf_code, changes):
    """將當日操作日報快照 UPSERT 至 etf_changes_history"""
    if not changes:
        return
    try:
        trade_date = date.today().isoformat()
        dr = changes.get("date_range", "")
        dr_m = re.search(r"(\d{4}-\d{2}-\d{2})\s*[→>\-]+\s*(\d{4}-\d{2}-\d{2})", dr)
        if dr_m:
            trade_date = dr_m.group(2)
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
                    changes_json=EXCLUDED.changes_json
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
