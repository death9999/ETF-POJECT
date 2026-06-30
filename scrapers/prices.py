"""
股價爬蟲
- scrape_moneydj_price: ETF 即時/收盤股價（Yahoo Finance V8 + TWSE）
- get_stock_close: 個股指定日期收盤價（DB 快取 → TWSE → Yahoo）
"""
import re
import requests
import urllib3
from datetime import datetime, timedelta, timezone

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_PRICE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "zh-TW,zh;q=0.9",
}


def scrape_moneydj_price(etf_code):
    """
    抓取 ETF 現價
    1. Yahoo Finance V8 API（盤中即時+昨收）
    2. TWSE STOCK_DAY 收盤行情（休市備援）
    """
    ticker = f"{etf_code}.TW"

    for host in ["query1", "query2"]:
        try:
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
            r = requests.get(url, headers=_PRICE_HEADERS, timeout=12, verify=False)
            if r.status_code == 200:
                result = r.json()["chart"]["result"][0]
                meta  = result["meta"]
                price = meta.get("regularMarketPrice")
                prev  = meta.get("previousClose")
                if not price or float(price) <= 0:
                    closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
                    closes = [c for c in closes if c is not None]
                    if closes:
                        price = closes[-1]
                        prev  = closes[-2] if len(closes) >= 2 else price
                if price and float(price) > 0:
                    price = round(float(price), 2)
                    prev  = round(float(prev), 2) if prev else None
                    chg   = round(price - prev, 2) if prev else None
                    chg_p = round(chg / prev * 100, 2) if (chg is not None and prev) else None
                    print(f"[Yahoo] {etf_code} = {price} prev={prev}")
                    return {"price": price, "prev": prev, "change": chg,
                            "change_pct": chg_p, "source": "Yahoo"}
        except Exception as e:
            print(f"[Yahoo/{host}] {etf_code} 失敗: {e}")

    try:
        check = datetime.now()
        for _ in range(3):
            ym = check.strftime("%Y%m%d")
            url2 = (f"https://www.twse.com.tw/exchangeReport/STOCK_DAY"
                    f"?response=json&date={ym}&stockNo={etf_code}")
            r2 = requests.get(url2, headers={"User-Agent": "Mozilla/5.0"},
                              timeout=10, verify=False)
            if r2.status_code == 200:
                d2 = r2.json()
                if d2.get("stat") == "OK" and d2.get("data"):
                    rows  = d2["data"]
                    last  = rows[-1]
                    prev_r = rows[-2] if len(rows) >= 2 else rows[-1]
                    close = float(last[6].replace(",", ""))
                    prev  = float(prev_r[6].replace(",", ""))
                    chg   = round(close - prev, 2)
                    chg_p = round(chg / prev * 100, 2) if prev else 0
                    print(f"[TWSE] {etf_code} = {close}")
                    return {"price": close, "prev": prev, "change": chg,
                            "change_pct": chg_p, "source": "TWSE收盤"}
            check = (check.replace(day=1) - timedelta(days=1))
    except Exception as e:
        print(f"[TWSE] {etf_code} 失敗: {e}")

    print(f"[股價] {etf_code} 所有來源均失敗")
    return None


def _to_roc(yyyymmdd):
    """西元 YYYYMMDD → 民國 YYY/MM/DD（TWSE 格式）"""
    return str(int(yyyymmdd[:4]) - 1911) + "/" + yyyymmdd[4:6] + "/" + yyyymmdd[6:]


def get_stock_close(stock_code, yyyymmdd):
    """
    取得個股指定日期收盤價
    來源1: DB stock_close_cache（最快）
    來源2: TWSE STOCK_DAY（台灣官方）
    來源3: Yahoo Finance previousClose（備援）
    """
    from db import get_db

    td_str = f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"

    # ── DB 快取 ───────────────────────────────────────────────────────
    try:
        _db = get_db()
        if _db:
            try:
                _cur = _db.cursor()  # Bug 4 修正：pg8000 cursor 不支援 context manager
                _cur.execute(
                    "SELECT close_price FROM stock_close_cache WHERE stock_code=%s AND trade_date=%s",
                    (stock_code, td_str)
                )
                _row = _cur.fetchone()
                if _row:
                    print(f"[DB cache] {stock_code} {td_str} = {_row[0]}")
                    return float(_row[0])
            finally:
                _db.close()
    except Exception as e:
        print(f"[DB cache] 查詢失敗: {e}")

    def _write_cache(price):
        try:
            _db2 = get_db()
            if _db2:
                try:
                    _c = _db2.cursor()  # Bug 4 修正：pg8000 cursor 不支援 context manager
                    _c.execute(
                        """INSERT INTO stock_close_cache (stock_code, trade_date, close_price)
                           VALUES (%s, %s, %s) ON CONFLICT (stock_code, trade_date) DO NOTHING""",
                        (stock_code, td_str, price)
                    )
                    _db2.commit()
                finally:
                    _db2.close()
        except Exception:
            pass

    # ── TWSE STOCK_DAY ────────────────────────────────────────────────
    check = datetime.strptime(yyyymmdd, "%Y%m%d")
    for _ in range(3):
        ym = check.strftime("%Y%m%d")
        try:
            url = (f"https://www.twse.com.tw/exchangeReport/STOCK_DAY"
                   f"?response=json&date={ym}&stockNo={stock_code}")
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                             timeout=10, verify=False)
            if r.status_code == 200:
                d = r.json()
                if d.get("stat") == "OK" and d.get("data"):
                    target = _to_roc(yyyymmdd)
                    for row in reversed(d["data"]):
                        if row[0] == target:
                            price = float(row[6].replace(",", ""))
                            print(f"[TWSE] {stock_code} {target} = {price}")
                            _write_cache(price)
                            return price
                    for row in reversed(d["data"]):
                        try:
                            price = float(row[6].replace(",", ""))
                            print(f"[TWSE] {stock_code} fallback last = {price}")
                            return price
                        except Exception:
                            pass
        except Exception as e:
            print(f"[TWSE] {stock_code} 失敗: {e}")
        check = (check.replace(day=1) - timedelta(days=1))

    # ── Yahoo Finance ─────────────────────────────────────────────────
    target_ymd = td_str
    for host in ["query1", "query2"]:
        try:
            ticker = f"{stock_code}.TW"
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                             timeout=8, verify=False)
            if r.status_code == 200:
                result_j = r.json().get("chart", {}).get("result", [{}])[0]
                meta = result_j.get("meta", {})
                timestamps = result_j.get("timestamp", [])
                closes = result_j.get("indicators", {}).get("quote", [{}])[0].get("close", [])
                closes = [c for c in closes if c is not None]
                if timestamps and closes:
                    for i, ts in enumerate(timestamps):
                        ts_date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                        if ts_date == target_ymd and i < len(closes):
                            price = float(closes[i])
                            print(f"[Yahoo] {stock_code} {ts_date} close = {price}")
                            _write_cache(price)
                            return price
                prev = meta.get("previousClose")
                if prev and float(prev) > 0:
                    price = float(prev)
                    print(f"[Yahoo] {stock_code} previousClose = {price}")
                    return price
        except Exception as e:
            print(f"[Yahoo/{host}] {stock_code} 失敗: {e}")

    return None
