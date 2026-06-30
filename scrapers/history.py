"""
ETF 歷史個股交易記錄爬蟲
來源：etfinfo.tw /active 頁的 NUXT_DATA 內嵌的 entry/exit 記錄
"""
import re
import json
from datetime import date

import requests
from bs4 import BeautifulSoup

from db import get_db

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
}


def _safe_get(url, timeout=12):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, verify=False)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"[history] 無法抓取 {url}: {e}")
        return None


def scrape_trade_records(etf_code):
    """
    從 etfinfo.tw/etf/{code}/active 的 NUXT_DATA 解析個股進出記錄。
    回傳 list of dict: {code, name, entry_date, exit_date, holding_days, entry_price}
    """
    url = f"https://www.etfinfo.tw/etf/{etf_code}/active"
    r = _safe_get(url, timeout=12)
    if not r:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    nuxt = soup.find("script", id="__NUXT_DATA__")
    if not nuxt or not nuxt.string:
        return []

    try:
        data = json.loads(nuxt.string)
    except Exception as e:
        print(f"[history] NUXT JSON 解析失敗 {etf_code}: {e}")
        return []

    def resolve(ref):
        if isinstance(ref, int) and 0 <= ref < len(data):
            val = data[ref]
            # Avoid recursive dicts
            if isinstance(val, (str, int, float)) or val is None:
                return val
        return ref

    trades = []
    seen = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        if "stockCode" not in item or "entryDate" not in item:
            continue
        code = resolve(item["stockCode"])
        name = resolve(item.get("stockName", ""))
        entry_date = resolve(item["entryDate"])
        exit_ref = item.get("exitDate")
        exit_date = resolve(exit_ref) if exit_ref is not None else None
        holding_days   = resolve(item.get("holdingDays"))
        entry_price    = resolve(item.get("entryPrice"))
        exit_price     = resolve(item.get("exitPrice"))
        current_price  = resolve(item.get("currentPrice"))
        shares         = resolve(item.get("shares"))
        avg_cost       = resolve(item.get("avgCost"))
        return_pct     = resolve(item.get("returnPct"))
        realized_pnl   = resolve(item.get("realizedPnL"))
        unrealized_pnl = resolve(item.get("unrealizedPnL"))
        total_invested = resolve(item.get("totalInvested"))
        status         = resolve(item.get("status", ""))

        # Validate
        if not code or not re.match(r"^\d{4,6}[A-Za-z]?$", str(code)):
            continue
        if not entry_date or not re.match(r"^\d{4}-\d{2}-\d{2}$", str(entry_date)):
            continue
        if exit_date and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(exit_date)):
            exit_date = None

        key = (str(code), str(entry_date))
        if key in seen:
            continue
        seen.add(key)

        def _f(v):
            return float(v) if isinstance(v, (int, float)) and v is not None else None
        def _i(v):
            return int(v) if isinstance(v, (int, float)) and v is not None else None

        trades.append({
            "code": str(code),
            "name": str(name) if name else "",
            "entry_date":    str(entry_date),
            "exit_date":     str(exit_date) if exit_date else None,
            "holding_days":  _i(holding_days),
            "entry_price":   _f(entry_price),
            "exit_price":    _f(exit_price),
            "current_price": _f(current_price),
            "shares":        _i(shares),
            "avg_cost":      _f(avg_cost),
            "return_pct":    _f(return_pct),
            "realized_pnl":  _f(realized_pnl),
            "unrealized_pnl":_f(unrealized_pnl),
            "total_invested":_f(total_invested),
            "status":        str(status) if status else "",
        })

    trades.sort(key=lambda x: x["entry_date"])
    print(f"[history] {etf_code} 解析到 {len(trades)} 筆交易記錄，"
          f"最早 {trades[0]['entry_date'] if trades else 'N/A'}")
    return trades


def sync_trade_records(etf_code):
    """爬取並 upsert 至 etf_trade_records 表。回傳新增筆數。"""
    trades = scrape_trade_records(etf_code)
    if not trades:
        return 0

    conn = get_db()
    if not conn:
        return 0

    inserted = 0
    try:
        cur = conn.cursor()
        for t in trades:
            cur.execute("""
                INSERT INTO etf_trade_records
                    (etf_code, stock_code, stock_name, entry_date, exit_date,
                     holding_days, entry_price, exit_price, current_price,
                     shares, avg_cost, return_pct, realized_pnl, unrealized_pnl,
                     total_invested, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (etf_code, stock_code, entry_date) DO UPDATE SET
                    stock_name     = EXCLUDED.stock_name,
                    exit_date      = EXCLUDED.exit_date,
                    holding_days   = EXCLUDED.holding_days,
                    entry_price    = EXCLUDED.entry_price,
                    exit_price     = EXCLUDED.exit_price,
                    current_price  = EXCLUDED.current_price,
                    shares         = EXCLUDED.shares,
                    avg_cost       = EXCLUDED.avg_cost,
                    return_pct     = EXCLUDED.return_pct,
                    realized_pnl   = EXCLUDED.realized_pnl,
                    unrealized_pnl = EXCLUDED.unrealized_pnl,
                    total_invested = EXCLUDED.total_invested,
                    status         = EXCLUDED.status,
                    scraped_at     = NOW()
            """, (
                etf_code,
                t["code"], t["name"],
                t["entry_date"], t["exit_date"],
                t["holding_days"], t["entry_price"],
                t["exit_price"], t["current_price"],
                t["shares"], t["avg_cost"],
                t["return_pct"], t["realized_pnl"], t["unrealized_pnl"],
                t["total_invested"], t["status"],
            ))
            if cur.rowcount > 0:
                inserted += 1
        conn.commit()
        print(f"[history] {etf_code} 同步完成，upsert {inserted} 筆")
    except Exception as e:
        print(f"[history] sync_trade_records 失敗: {e}")
    finally:
        conn.close()

    return inserted


def get_period_trade_changes(etf_code, period_start, period_end=None):
    """
    查詢特定期間的個股進出記錄。
    period_start / period_end: date 物件（或 isoformat 字串）
    回傳 { 'added': [...], 'removed': [...] }
    """
    if period_end is None:
        period_end = date.today()

    conn = get_db()
    if not conn:
        return {"added": [], "removed": []}
    _COLS = """stock_code, stock_name, entry_date, exit_date, holding_days,
               entry_price, exit_price, current_price, shares,
               return_pct, realized_pnl, unrealized_pnl, status"""
    try:
        cur = conn.cursor()
        # 新增（進場日在期間內）
        cur.execute(f"""
            SELECT {_COLS} FROM etf_trade_records
            WHERE etf_code=%s AND entry_date >= %s AND entry_date <= %s
            ORDER BY entry_date
        """, (etf_code, str(period_start), str(period_end)))
        added_rows = cur.fetchall()

        # 清倉（出場日在期間內）
        cur.execute(f"""
            SELECT {_COLS} FROM etf_trade_records
            WHERE etf_code=%s AND exit_date >= %s AND exit_date <= %s
            ORDER BY exit_date
        """, (etf_code, str(period_start), str(period_end)))
        removed_rows = cur.fetchall()
    except Exception as e:
        print(f"[history] get_period_trade_changes 失敗: {e}")
        return {"added": [], "removed": []}
    finally:
        conn.close()

    def _f(v):
        return float(v) if v is not None else None

    def row_to_dict(row, action):
        realized = _f(row[10])
        unrealized = _f(row[11])
        pnl = realized if row[3] else unrealized  # 已出場用realized，持有中用unrealized
        return {
            "code": row[0], "name": row[1],
            "entry_date":    str(row[2]) if row[2] else None,
            "exit_date":     str(row[3]) if row[3] else None,
            "holding_days":  row[4],
            "entry_price":   _f(row[5]),
            "exit_price":    _f(row[6]),
            "current_price": _f(row[7]),
            "shares":        row[8],
            "return_pct":    _f(row[9]),
            "realized_pnl":  realized,
            "unrealized_pnl":unrealized,
            "pnl":           pnl,
            "status":        row[12] or "",
            "type": "新增" if action == "add" else "清倉",
            "shares_display": 0, "amount": "",
        }

    return {
        "added": [row_to_dict(r, "add") for r in added_rows],
        "removed": [row_to_dict(r, "remove") for r in removed_rows],
    }
