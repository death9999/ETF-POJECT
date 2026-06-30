"""
每日操作日報爬蟲
- scrape_daily_changes: 從 etfinfo.tw/active 抓取加碼/減碼/新增/刪除明細
- save_changes_snapshot: 將日報快照寫入 DB etf_changes_history
"""
import re
import json
from datetime import date

from db import get_db
from scrapers.holdings import safe_get
from bs4 import BeautifulSoup


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
