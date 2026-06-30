"""
AI 題材股精選 Blueprint
- /AI_Stocks, /ai-stocks    頁面
- /api/ai-stocks            取得本週精選清單
- /api/ai-stocks/refresh    手動觸發更新
"""
import json
import os
import time
import threading
import requests
import urllib3
from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request, session, redirect, Response, current_app

from db import get_db

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ai_stocks_bp = Blueprint("ai_stocks", __name__)

# 模組內快取（避免對 app.py 的 cache dict 產生依賴）
_cache = {}

# ──────────────────────────────────────────────────────────────
# 候選清單（每週五 18:00 自動更新）
# ──────────────────────────────────────────────────────────────
AI_STOCK_CANDIDATES = [
    {"code": "2330", "name": "台積電",    "tags": ["AI晶片", "HBM", "CoWoS封裝"]},
    {"code": "2317", "name": "鴻海",      "tags": ["AI伺服器", "GB200", "輝達夥伴"]},
    {"code": "3034", "name": "聯詠",      "tags": ["AI驅動IC", "車用AI"]},
    {"code": "6669", "name": "緯穎",      "tags": ["AI伺服器", "液冷散熱"]},
    {"code": "2382", "name": "廣達",      "tags": ["AI伺服器", "雲端AI"]},
    {"code": "2308", "name": "台達電",    "tags": ["電源管理", "散熱模組", "AI基礎設施"]},
    {"code": "3661", "name": "世芯-KY",   "tags": ["AI ASIC", "客製晶片設計"]},
    {"code": "2379", "name": "瑞昱",      "tags": ["AI連網", "Edge AI"]},
    {"code": "2454", "name": "聯發科",    "tags": ["AI手機", "車用AI", "MediaTek Dimensity"]},
    {"code": "2303", "name": "聯電",      "tags": ["特殊製程", "AI週邊晶片"]},
    {"code": "3711", "name": "日月光投控", "tags": ["先進封裝", "SiP", "AI封測"]},
    {"code": "2345", "name": "智邦",      "tags": ["AI交換器", "網路基礎設施"]},
    {"code": "6279", "name": "胡連",      "tags": ["AI連接器", "高速傳輸"]},
    {"code": "8046", "name": "南電",      "tags": ["ABF載板", "AI高階封裝基板"]},
    {"code": "3037", "name": "欣興",      "tags": ["HDI基板", "AI封裝載板"]},
]

_YF_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


# ──────────────────────────────────────────────────────────────
# 資料抓取與寫入
# ──────────────────────────────────────────────────────────────

def fetch_stock_fundamentals(stock_code):
    """透過 Yahoo Finance V8 + quoteSummary 抓取個股基本面"""
    result = {
        "stock_code": stock_code, "price": None, "change_val": None,
        "change_pct": None, "eps_ttm": None, "eps_growth_pct": None,
        "roe": None, "revenue_growth_pct": None, "net_margin": None,
        "capex_b": None, "market_cap_b": None, "pe_ratio": None,
        "extra_json": {}
    }
    ticker = f"{stock_code}.TW"

    for host in ["query1", "query2"]:
        try:
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
            r = requests.get(url, headers=_YF_HEADERS, timeout=12, verify=False)
            if r.status_code == 200:
                meta = r.json()["chart"]["result"][0]["meta"]
                price = meta.get("regularMarketPrice")
                prev  = meta.get("previousClose")
                if price and float(price) > 0:
                    result["price"]      = round(float(price), 2)
                    result["change_val"] = round(float(price) - float(prev), 2) if prev else None
                    result["change_pct"] = round((float(price) - float(prev)) / float(prev) * 100, 2) if prev else None
                    print(f"[AI股] {stock_code} price={result['price']}")
                    break
        except Exception as e:
            print(f"[AI股] Yahoo/{host} {stock_code} 失敗: {e}")

    try:
        url2 = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
                f"?modules=defaultKeyStatistics,financialData,incomeStatementHistory")
        r2 = requests.get(url2, headers={**_YF_HEADERS, "Accept": "application/json"},
                          timeout=15, verify=False)
        if r2.status_code == 200:
            qs = r2.json().get("quoteSummary", {}).get("result", [{}])[0]
            ks = qs.get("defaultKeyStatistics", {})
            fd = qs.get("financialData", {})

            def gv(d, k):
                return d.get(k, {}).get("raw") if isinstance(d.get(k), dict) else None

            eps = gv(ks, "trailingEps")
            pe  = gv(ks, "trailingPE") or gv(ks, "forwardPE")
            roe = gv(fd, "returnOnEquity")
            nm  = gv(fd, "profitMargins")
            rev_growth = gv(fd, "revenueGrowth")
            eg  = gv(fd, "earningsGrowth")
            mcap= gv(ks, "enterpriseValue")
            if eps:        result["eps_ttm"]           = round(float(eps), 2)
            if pe:         result["pe_ratio"]           = round(float(pe), 1)
            if roe:        result["roe"]                = round(float(roe) * 100, 2)
            if nm:         result["net_margin"]         = round(float(nm) * 100, 2)
            if rev_growth: result["revenue_growth_pct"] = round(float(rev_growth) * 100, 2)
            if eg:         result["eps_growth_pct"]     = round(float(eg) * 100, 2)
            if mcap:       result["market_cap_b"]       = round(float(mcap) / 1e8, 1)
            try:
                cf = qs.get("cashflowStatementHistory", {}).get("cashflowStatements", [])
                if cf:
                    capex_raw = cf[0].get("capitalExpenditures", {}).get("raw", 0)
                    result["capex_b"] = round(abs(float(capex_raw)) / 1e8, 1)
            except Exception:
                pass
            print(f"[AI股] {stock_code} EPS={result['eps_ttm']} ROE={result['roe']}%")
    except Exception as e:
        print(f"[AI股] quoteSummary {stock_code} 失敗: {e}")

    return result


def save_ai_stock_data(data):
    """將個股基本面資料 UPSERT 至 ai_stock_data"""
    try:
        conn = get_db()
        if not conn:
            return
        try:
            cur = conn.cursor()  # Bug 4 修正：pg8000 cursor 不支援 context manager
            cur.execute("""
                INSERT INTO ai_stock_data
                    (stock_code, stock_name, price, change_val, change_pct,
                     eps_ttm, eps_growth_pct, roe, revenue_growth_pct,
                     net_margin, capex_b, market_cap_b, pe_ratio, extra_json, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, CURRENT_TIMESTAMP)
                ON CONFLICT (stock_code) DO UPDATE SET
                    stock_name=EXCLUDED.stock_name, price=EXCLUDED.price,
                    change_val=EXCLUDED.change_val, change_pct=EXCLUDED.change_pct,
                    eps_ttm=EXCLUDED.eps_ttm, eps_growth_pct=EXCLUDED.eps_growth_pct,
                    roe=EXCLUDED.roe, revenue_growth_pct=EXCLUDED.revenue_growth_pct,
                    net_margin=EXCLUDED.net_margin, capex_b=EXCLUDED.capex_b,
                    market_cap_b=EXCLUDED.market_cap_b, pe_ratio=EXCLUDED.pe_ratio,
                    extra_json=EXCLUDED.extra_json, updated_at=CURRENT_TIMESTAMP
            """, (
                data["stock_code"], data.get("stock_name", ""),
                data.get("price"), data.get("change_val"), data.get("change_pct"),
                data.get("eps_ttm"), data.get("eps_growth_pct"), data.get("roe"),
                data.get("revenue_growth_pct"), data.get("net_margin"),
                data.get("capex_b"), data.get("market_cap_b"), data.get("pe_ratio"),
                json.dumps(data.get("extra_json", {}), ensure_ascii=False)
            ))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[AI股] save_ai_stock_data 失敗: {e}")


def refresh_ai_stock_picks():
    """抓取所有候選股基本面，依分數排序取前10，寫入 ai_stock_picks"""
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    print(f"[AI股] 開始更新每週精選（week_start={week_start}）")

    all_data = []
    for cand in AI_STOCK_CANDIDATES:
        try:
            d = fetch_stock_fundamentals(cand["code"])
            d["stock_name"] = cand["name"]
            d["tags"] = cand["tags"]
            d["sources"] = [
                {"label": f"Yahoo Finance - {cand['name']}",
                 "url": f"https://tw.stock.yahoo.com/quote/{cand['code']}"},
                {"label": f"TWSE 財報 - {cand['code']}",
                 "url": (f"https://mops.twse.com.tw/mops/web/t05st22_q1"
                         f"?encodeURIComponent=1&step=1&firstin=1&off=1&keyword4=&code1=&TYPEK2=&checkbtn="
                         f"&queryName=co_id&inpuType=co_id&TYPEK=all&isnew=false&co_id={cand['code']}")},
                {"label": f"ETF資訊網 - {cand['code']}",
                 "url": f"https://www.etfinfo.tw/stock/{cand['code']}"},
            ]
            save_ai_stock_data(d)
            all_data.append(d)
        except Exception as e:
            print(f"[AI股] {cand['code']} 失敗: {e}")

    def score(d):
        return (d.get("roe") or 0) * 0.4 + (d.get("eps_growth_pct") or 0) * 0.4 + (d.get("revenue_growth_pct") or 0) * 0.2

    all_data.sort(key=score, reverse=True)
    top10 = all_data[:10]

    try:
        conn = get_db()
        if conn:
            try:
                cur = conn.cursor()  # Bug 4 修正：pg8000 cursor 不支援 context manager
                for i, d in enumerate(top10):
                    cand_info = next((x for x in AI_STOCK_CANDIDATES if x["code"] == d["stock_code"]), {})
                    tags_str = ",".join(cand_info.get("tags", []))
                    reason = (f"ROE {d.get('roe','N/A')}% | "
                              f"EPS成長 {d.get('eps_growth_pct','N/A')}% | "
                              f"本益比 {d.get('pe_ratio','N/A')}倍")
                    cur.execute("""
                        INSERT INTO ai_stock_picks
                            (week_start, stock_code, stock_name, rank_order, reason, tags, sources_json)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (week_start, stock_code) DO UPDATE SET
                            rank_order=EXCLUDED.rank_order, reason=EXCLUDED.reason,
                            tags=EXCLUDED.tags, sources_json=EXCLUDED.sources_json
                    """, (
                        week_start.isoformat(), d["stock_code"], d.get("stock_name", ""),
                        i + 1, reason, tags_str,
                        json.dumps(d.get("sources", []), ensure_ascii=False)
                    ))
                conn.commit()
                print(f"[AI股] 本週精選已更新，共 {len(top10)} 檔")
            finally:
                conn.close()
    except Exception as e:
        print(f"[AI股] 寫入 ai_stock_picks 失敗: {e}")

    return top10


def _ai_stock_weekly_scheduler():
    """背景執行緒：每週五 18:00（台灣時間）自動更新"""
    print("[AI股] 每週更新排程啟動")
    while True:
        try:
            now = datetime.utcnow() + timedelta(hours=8)
            if now.weekday() == 4 and now.hour == 18 and now.minute == 0:
                refresh_ai_stock_picks()
                _cache.pop("ai_stocks_weekly", None)
        except Exception as e:
            print(f"[AI股] 排程錯誤: {e}")
        time.sleep(60)


# ──────────────────────────────────────────────────────────────
# 路由
# ──────────────────────────────────────────────────────────────

@ai_stocks_bp.route("/AI_Stocks")
@ai_stocks_bp.route("/ai-stocks")
def ai_stocks_page():
    if "user_id" not in session:
        return redirect("/login")
    html_path = os.path.join(current_app.root_path, "static", "ai_stocks.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return Response(f.read(), mimetype="text/html")
    return "AI Stocks page not found", 404


@ai_stocks_bp.route("/api/ai-stocks", methods=["GET"])
def get_ai_stocks():
    cache_key = "ai_stocks_weekly"
    if cache_key in _cache and (time.time() - _cache[cache_key]["timestamp"]) < 3600:
        return jsonify(_cache[cache_key]["data"])

    today = date.today()
    week_start = today - timedelta(days=today.weekday())

    try:
        conn = get_db()
        if not conn:
            return jsonify({"success": False, "error": "DB 連線失敗"}), 500
        try:
            cur = conn.cursor()  # Bug 4 修正：pg8000 cursor 不支援 context manager
            cur.execute("""
                SELECT p.stock_code, p.stock_name, p.rank_order, p.reason, p.tags, p.sources_json,
                       d.price, d.change_val, d.change_pct, d.eps_ttm, d.eps_growth_pct,
                       d.roe, d.revenue_growth_pct, d.net_margin, d.capex_b, d.market_cap_b,
                       d.pe_ratio, d.updated_at
                FROM ai_stock_picks p
                LEFT JOIN ai_stock_data d ON p.stock_code = d.stock_code
                WHERE p.week_start = %s
                ORDER BY p.rank_order ASC
            """, (week_start.isoformat(),))
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    picks = []
    for row in rows:
        picks.append({
            "code": row[0], "name": row[1], "rank": row[2],
            "reason": row[3], "tags": row[4].split(",") if row[4] else [],
            "sources": json.loads(row[5] or "[]"),
            "price": float(row[6]) if row[6] else None,
            "change_val": float(row[7]) if row[7] else None,
            "change_pct": float(row[8]) if row[8] else None,
            "eps_ttm": float(row[9]) if row[9] else None,
            "eps_growth_pct": float(row[10]) if row[10] else None,
            "roe": float(row[11]) if row[11] else None,
            "revenue_growth_pct": float(row[12]) if row[12] else None,
            "net_margin": float(row[13]) if row[13] else None,
            "capex_b": float(row[14]) if row[14] else None,
            "market_cap_b": float(row[15]) if row[15] else None,
            "pe_ratio": float(row[16]) if row[16] else None,
            "data_updated_at": str(row[17]) if row[17] else None,
        })

    if not picks:
        try:
            result = refresh_ai_stock_picks()
            for i, d in enumerate(result):
                cand = next((x for x in AI_STOCK_CANDIDATES if x["code"] == d["stock_code"]), {})
                picks.append({
                    "code": d["stock_code"], "name": d.get("stock_name", ""),
                    "rank": i + 1, "reason": d.get("reason", ""),
                    "tags": cand.get("tags", []),
                    "sources": d.get("sources", []),
                    "price": d.get("price"),
                    "change_val": d.get("change_val"),
                    "change_pct": d.get("change_pct"),
                    "eps_ttm": d.get("eps_ttm"),
                    "eps_growth_pct": d.get("eps_growth_pct"),
                    "roe": d.get("roe"),
                    "revenue_growth_pct": d.get("revenue_growth_pct"),
                    "net_margin": d.get("net_margin"),
                    "capex_b": d.get("capex_b"),
                    "market_cap_b": d.get("market_cap_b"),
                    "pe_ratio": d.get("pe_ratio"),
                    "data_updated_at": None,
                })
        except Exception as e:
            return jsonify({"success": False, "error": f"初始化失敗: {e}"}), 500

    result_data = {
        "success": True,
        "week_start": week_start.isoformat(),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "picks": picks,
    }
    _cache[cache_key] = {"timestamp": time.time(), "data": result_data}
    return jsonify(result_data)


@ai_stocks_bp.route("/api/ai-stocks/refresh", methods=["POST"])
def refresh_ai_stocks_api():
    if "user_id" not in session:
        return jsonify({"success": False, "error": "未登入"}), 401
    _cache.pop("ai_stocks_weekly", None)
    threading.Thread(target=refresh_ai_stock_picks, daemon=True).start()
    return jsonify({"success": True, "message": "已觸發背景更新，約 1-2 分鐘後完成"})
