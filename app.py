"""
主動式ETF持股追蹤器 - 後端伺服器
執行方式: python app.py
"""

from flask import Flask, jsonify, request, session, redirect, url_for, Response
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import json
import time
import threading
from datetime import datetime
import re
import os
import bcrypt
import secrets

from db import get_db, init_db, DATABASE_URL
from scrapers.holdings import safe_get, scrape_etfinfo, scrape_fhtrust, scrape_capital, FALLBACK
from scrapers.prices import scrape_moneydj_price, get_stock_close
from scrapers.changes import (scrape_daily_changes, save_changes_snapshot,
                              save_holdings_snapshot, seed_period_snapshots,
                              get_period_diff)
from scrapers.history import sync_trade_records, get_period_trade_changes
from routes.auth import auth_bp
from routes.ai_stocks import ai_stocks_bp, _ai_stock_weekly_scheduler
from routes.pages import pages_bp

app = Flask(__name__)

# SECRET_KEY 必須固定，否則重啟後 session 失效
_secret = os.environ.get("SECRET_KEY")
if not _secret:
    print("[警告] 未設定 SECRET_KEY 環境變數，session 將在重啟後失效！")
    _secret = "etf-tracker-default-secret-change-me-2026"
app.secret_key = _secret

# Session cookie 設定
app.config.update(
    SESSION_COOKIE_SECURE=False,      # 允許 HTTP（Render 前端是 HTTPS，但內部是 HTTP）
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=86400 * 30,  # 30天
)
CORS(app, supports_credentials=True, origins=["https://etf-tracker-1.onrender.com"])
app.register_blueprint(auth_bp)
app.register_blueprint(ai_stocks_bp)
app.register_blueprint(pages_bp)

# 啟動時初始化資料庫
if DATABASE_URL:
    init_db()

# ──────────────────────────────────────────
# 全域 ETF 設定快取（模組載入時初始化）
# admin_add_etf / admin_delete_etf 會以 global ETF_CONFIG 更新
# ──────────────────────────────────────────
ETF_CONFIG = {}  # 先建立空字典，下方 get_etf_config 定義後立即填入

# ──────────────────────────────────────────
# 快取：避免每次請求都重新爬蟲
# ──────────────────────────────────────────
cache = {}
# 昨日收盤價快取（每日更新，供 get_all_prices 用於計算漲跌幅）
_prev_close_cache = {
    "00403A": 10.70,   # 2026-06-03 收盤
    "00981A": 31.33,   # 2026-06-03 收盤
    "00991A": 19.81,   # 2026-06-03 收盤
    "00992A": 18.14,   # 2026-06-03 收盤
}
_live_price_cache = {}  # 即時股價快取（背景執行緒每60秒更新）
CACHE_TTL = 300  # 5分鐘更新一次（秒）

# ──────────────────────────────────────────
# 各投信持股公告時程（每營業日揭露時間）
# 統一投信：2026/5/12起調整為 16:30 後
# 群益投信：收盤後約 15:30
# 復華投信：收盤後約 16:00
# ──────────────────────────────────────────
ETF_ANNOUNCEMENT_SCHEDULE = {
    "00403A": {"issuer": "統一投信", "announce_hour": 16, "announce_min": 30},
    "00981A": {"issuer": "統一投信", "announce_hour": 16, "announce_min": 30},
    "00991A": {"issuer": "復華投信", "announce_hour": 16, "announce_min": 0},
    "00992A": {"issuer": "群益投信", "announce_hour": 15, "announce_min": 30},
}

def get_announcement_label(etf_code):
    """根據公告時程判斷目前資料狀態，回傳標籤文字與類型"""
    now = datetime.now()
    sch = ETF_ANNOUNCEMENT_SCHEDULE.get(etf_code, {"announce_hour": 16, "announce_min": 30})
    ah, am = sch["announce_hour"], sch["announce_min"]
    is_weekday = now.weekday() < 5
    announce_dt = now.replace(hour=ah, minute=am, second=0, microsecond=0)
    if is_weekday and now >= announce_dt:
        return {"label": f"今日公告（{ah:02d}:{am:02d} 後更新）", "type": "published"}
    elif is_weekday:
        return {"label": f"公告時間 {ah:02d}:{am:02d} 後更新", "type": "pending"}
    else:
        return {"label": "最新公告（上個交易日）", "type": "published"}

# ──────────────────────────────────────────
# 動態 ETF 設定（可透過管理 API 新增/刪除）
# 儲存在 etf_config.json，啟動時載入
# ──────────────────────────────────────────
import os, json

ETF_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "etf_config.json")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "etf2026")  # 可在 Render 環境變數設定

# 預設ETF清單（etf_config.json 不存在時使用）
DEFAULT_ETF_LIST = [
    {"code": "00992A", "name": "主動群益科技創新", "issuer": "群益投信"},
    {"code": "00991A", "name": "主動復華未來50",   "issuer": "復華投信"},
    {"code": "00981A", "name": "主動統一台股增長", "issuer": "統一投信"},
    {"code": "00403A", "name": "主動統一升級50",   "issuer": "統一投信"},
]

def load_etf_list():
    """從資料庫載入 ETF 清單"""
    if DATABASE_URL:
        conn = get_db()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT code,name,issuer,source_key,inception_date FROM etf_configs ORDER BY id")
                rows = cur.fetchall()
                if rows:
                    print(f"[設定] 從資料庫載入 {len(rows)} 檔 ETF")
                    return [{"code": r[0], "name": r[1], "issuer": r[2],
                             "source_key": r[3],
                             "inception_date": str(r[4]) if r[4] else ""} for r in rows]
            except Exception as e:
                print(f"[設定] 資料庫載入失敗: {e}")
            finally:
                conn.close()

    # 備援：環境變數
    env_json = os.environ.get("ETF_LIST_JSON", "")
    if env_json:
        try:
            return json.loads(env_json)
        except Exception:
            pass

    # 備援：JSON 檔
    if os.path.exists(ETF_CONFIG_FILE):
        try:
            with open(ETF_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    return DEFAULT_ETF_LIST.copy()

def save_etf_list(etf_list):
    """
    儲存 ETF 清單並自動更新 Render 環境變數
    - 寫入 JSON 檔（本機開發用）
    - 自動呼叫 Render API 更新 ETF_LIST_JSON 環境變數
    """
    env_str = json.dumps(etf_list, ensure_ascii=False)

    # 寫入 JSON 檔（本機開發）
    try:
        with open(ETF_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(etf_list, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[設定] JSON 寫入失敗: {e}")

    # 自動更新 Render 環境變數
    render_key = os.environ.get("RENDER_API_KEY", "")
    service_id = os.environ.get("RENDER_SERVICE_ID", "")

    if render_key and service_id:
        try:
            import requests as rq
            # 取得目前所有環境變數
            get_url = f"https://api.render.com/v1/services/{service_id}/env-vars"
            headers = {
                "Authorization": f"Bearer {render_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            resp = rq.get(get_url, headers=headers, timeout=15)
            resp.raise_for_status()
            env_vars = resp.json()

            # 找到 ETF_LIST_JSON 的 ID（若存在）
            etf_var_id = None
            for ev in env_vars:
                item = ev.get("envVar", ev)
                if item.get("key") == "ETF_LIST_JSON":
                    etf_var_id = item.get("id")
                    break

            if etf_var_id:
                # 更新既有的環境變數
                patch_url = f"https://api.render.com/v1/services/{service_id}/env-vars/{etf_var_id}"
                patch_resp = rq.put(patch_url, headers=headers,
                    json={"key": "ETF_LIST_JSON", "value": env_str}, timeout=15)
                patch_resp.raise_for_status()
                print(f"[Render] ETF_LIST_JSON 已更新（{len(etf_list)} 檔）")
            else:
                # 新增環境變數
                post_url = f"https://api.render.com/v1/services/{service_id}/env-vars"
                post_resp = rq.post(post_url, headers=headers,
                    json={"key": "ETF_LIST_JSON", "value": env_str}, timeout=15)
                post_resp.raise_for_status()
                print(f"[Render] ETF_LIST_JSON 已新增（{len(etf_list)} 檔）")

        except Exception as e:
            print(f"[Render] 自動更新環境變數失敗: {e}")
            # 失敗不影響主流程，仍回傳 env_str 讓前端可手動操作
    else:
        print("[Render] 未設定 RENDER_API_KEY 或 RENDER_SERVICE_ID，跳過自動更新")

    return env_str

def get_scraper_for_source(code, source_key):
    """根據偵測到的最佳來源回傳爬蟲函式"""
    if source_key == "pocket":
        def pocket_scraper():
            url = f"https://www.pocket.tw/etf/tw/{code.lower()}/fundholding/"
            r = safe_get(url, timeout=10)
            if not r or r.status_code != 200:
                return None
            soup = BeautifulSoup(r.text, "html.parser")
            holdings = []
            for table in soup.find_all("table"):
                for row in table.find_all("tr")[1:]:
                    cols = row.find_all("td")
                    if len(cols) < 2: continue
                    try:
                        cell = cols[0].get_text(strip=True)
                        code_m = re.match(r"^(\d{4,6}[A-Za-z]?)", cell)
                        if not code_m: continue
                        c = code_m.group(1)
                        name = cell[len(c):].strip()
                        pct = 0.0
                        for col in cols[1:]:
                            txt = col.get_text(strip=True)
                            if "%" in txt:
                                pct_str = re.sub(r"[^\d.]", "", txt.split("%")[0])
                                if pct_str: pct = float(pct_str)
                                break
                        if c and pct > 0:
                            holdings.append({"code": c, "name": name, "pct": round(pct,2), "status": "hold"})
                    except Exception: continue
            return holdings if holdings else None
        return pocket_scraper
    # 預設：etfinfo.tw
    return lambda c=code: scrape_etfinfo(c)


def get_etf_config():
    """動態產生 ETF_CONFIG（從 JSON 載入，依 source_key 選最佳爬蟲）"""
    etf_list = load_etf_list()
    return {
        item["code"]: {
            "name":       item["name"],
            "issuer":     item.get("issuer", ""),
            "source_key": item.get("source_key", "etfinfo"),
            "scraper":    get_scraper_for_source(item["code"], item.get("source_key", "etfinfo")),
            "fallback":   lambda c=item["code"]: scrape_etfinfo(c),  # 備援永遠用 etfinfo
        }
        for item in etf_list
    }

def is_cache_valid(key):
    if key not in cache:
        return False
    return (time.time() - cache[key]["timestamp"]) < CACHE_TTL

# ── 模組載入後立即填入 ETF_CONFIG（覆蓋上方的空字典佔位） ──
ETF_CONFIG = get_etf_config()

# ──────────────────────────────────────────
# API 路由
# ──────────────────────────────────────────

# ──────────────────────────────────────────
# 管理 API：新增/刪除 ETF（需密碼驗證）
# ──────────────────────────────────────────

def verify_admin(req):
    """驗證管理員密碼（支援 Header 或 JSON body）"""
    # 優先從 Header 取得
    pwd = req.headers.get("X-Admin-Password", "")
    # 若 Header 沒有，嘗試從 JSON body 取得
    if not pwd and req.is_json:
        try:
            pwd = req.get_json(silent=True).get("password", "")
        except Exception:
            pwd = ""
    return pwd == ADMIN_PASSWORD

@app.route("/api/admin/etf/list", methods=["GET"])
def admin_get_etf_list():
    """取得目前所有 ETF 清單（管理用）"""
    from flask import request as req
    if not verify_admin(req):
        return jsonify({"success": False, "error": "密碼錯誤"}), 401
    return jsonify({"success": True, "data": load_etf_list()})

@app.route("/api/admin/etf/add", methods=["POST"])
def admin_add_etf():
    """新增一檔 ETF"""
    from flask import request as req
    if not req.is_json:
        return jsonify({"success": False, "error": "需要 JSON"}), 400
    if not verify_admin(req):
        return jsonify({"success": False, "error": "密碼錯誤"}), 401

    data = req.json
    code   = data.get("code", "").strip().upper()
    name   = data.get("name", "").strip()
    issuer = data.get("issuer", "").strip()

    if not code or not name:
        return jsonify({"success": False, "error": "代號和名稱為必填"}), 400

    etf_list = load_etf_list()

    # 檢查是否已存在
    if any(e["code"] == code for e in etf_list):
        return jsonify({"success": False, "error": f"{code} 已存在"}), 409

    # 自動偵測最佳來源並嘗試補充名稱
    detected = detect_best_source(code)
    source_key = detected["source_key"] if detected else "etfinfo"
    source_name = detected["source"] if detected else "未知"

    # 若名稱未填，嘗試從 etfinfo.tw 頁面抓取
    if not name:
        try:
            r = safe_get(f"https://www.etfinfo.tw/etf/{code}", timeout=8)
            if r and r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                h1 = soup.find("h1")
                if h1:
                    name = h1.get_text(strip=True).replace(code, "").strip()
        except Exception:
            pass

    if not name:
        return jsonify({"success": False, "error": "無法自動取得名稱，請手動填入"}), 400

    etf_list.append({
        "code": code, "name": name, "issuer": issuer,
        "source_key": source_key
    })
    save_etf_list(etf_list)

    # 清除快取，讓新ETF立即生效
    global ETF_CONFIG
    ETF_CONFIG = get_etf_config()
    cache.pop(code, None)
    cache.pop("all_prices", None)

    env_str = save_etf_list(etf_list)
    render_auto = bool(os.environ.get("RENDER_API_KEY") and os.environ.get("RENDER_SERVICE_ID"))
    return jsonify({
        "success": True,
        "message": f"已新增 {code} {name}",
        "source": source_name,
        "holdings_count": detected["holdings_count"] if detected else 0,
        "verified": detected is not None,
        "env_json": env_str,
        "env_hint": not render_auto,  # 只在無法自動更新時顯示手動提示
        "auto_updated": render_auto,
    })

@app.route("/api/admin/etf/delete", methods=["POST"])
def admin_delete_etf():
    """刪除一檔 ETF"""
    from flask import request as req
    if not req.is_json:
        return jsonify({"success": False, "error": "需要 JSON"}), 400
    if not verify_admin(req):
        return jsonify({"success": False, "error": "密碼錯誤"}), 401

    code = req.json.get("code", "").strip().upper()
    if not code:
        return jsonify({"success": False, "error": "代號為必填"}), 400

    etf_list = load_etf_list()
    original_len = len(etf_list)
    etf_list = [e for e in etf_list if e["code"] != code]

    if len(etf_list) == original_len:
        return jsonify({"success": False, "error": f"{code} 不存在"}), 404

    env_str = save_etf_list(etf_list)

    global ETF_CONFIG
    ETF_CONFIG = get_etf_config()
    cache.pop(code, None)
    cache.pop(f"{code}_changes", None)

    render_auto = bool(os.environ.get("RENDER_API_KEY") and os.environ.get("RENDER_SERVICE_ID"))
    return jsonify({
        "success": True,
        "message": f"已刪除 {code}",
        "env_json": env_str,
        "env_hint": not render_auto,
        "auto_updated": render_auto,
    })

def detect_best_source(etf_code):
    """
    自動偵測最佳持股來源
    依序嘗試：etfinfo.tw → pocket.tw → etfinfo 簡易解析
    回傳 {"source": "etfinfo", "holdings_count": N, "top3": [...]} 或 None
    """
    # 來源1：etfinfo.tw（完整持股，自動翻頁）
    try:
        holdings = scrape_etfinfo(etf_code)
        if holdings and len(holdings) >= 3:
            return {
                "source": "etfinfo.tw",
                "source_key": "etfinfo",
                "holdings_count": len(holdings),
                "top3": holdings[:3],
                "note": "etfinfo.tw 完整持股，自動翻頁"
            }
    except Exception as e:
        print(f"[偵測] etfinfo 失敗: {e}")

    # 來源2：pocket.tw
    try:
        url = f"https://www.pocket.tw/etf/tw/{etf_code.lower()}/fundholding/"
        r = safe_get(url, timeout=10)
        if r and r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            holdings = []
            for table in soup.find_all("table"):
                for row in table.find_all("tr")[1:]:
                    cols = row.find_all("td")
                    if len(cols) < 2: continue
                    try:
                        cell = cols[0].get_text(strip=True)
                        code_m = re.match(r'^(\d{4,6}[A-Za-z]?)', cell)
                        if not code_m: continue
                        code = code_m.group(1)
                        name = cell[len(code):].strip()
                        pct = 0.0
                        for col in cols[1:]:
                            txt = col.get_text(strip=True)
                            if "%" in txt:
                                pct_str = re.sub(r"[^\d.]", "", txt.split("%")[0])
                                if pct_str: pct = float(pct_str)
                                break
                        if code and pct > 0:
                            holdings.append({"code": code, "name": name, "pct": round(pct,2), "status": "hold"})
                    except Exception: continue
            if holdings and len(holdings) >= 3:
                return {
                    "source": "pocket.tw",
                    "source_key": "pocket",
                    "holdings_count": len(holdings),
                    "top3": holdings[:3],
                    "note": "pocket.tw 持股資料"
                }
    except Exception as e:
        print(f"[偵測] pocket 失敗: {e}")

    return None


@app.route("/api/admin/etf/lookup/<etf_code>", methods=["GET"])
def admin_lookup_etf(etf_code):
    """查詢 ETF 名稱與投信公司（輸入代號時自動帶入）"""
    from flask import request as req
    if not verify_admin(req):
        return jsonify({"success": False, "error": "密碼錯誤"}), 401

    etf_code = etf_code.upper()

    # 已知投信對應表（依代號前綴判斷）
    known_issuers = {
        "004": "統一投信",  "00403A": "統一投信",
        "009": "各家投信",
        "00981A": "統一投信", "00982A": "群益投信",
        "00991A": "復華投信", "00992A": "群益投信",
        "00993A": "安聯投信", "00994A": "第一金投信",
        "00995A": "中信投信", "00996A": "富邦投信",
        "00997A": "國泰投信", "00998A": "元大投信",
        "00999A": "永豐投信", "00400A": "國泰投信",
        "00401A": "富邦投信", "00402A": "元大投信",
    }

    name = ""
    issuer = known_issuers.get(etf_code, "")

    # 從 etfinfo.tw 抓取名稱和投信
    try:
        r = safe_get(f"https://www.etfinfo.tw/etf/{etf_code}", timeout=8)
        if r and r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")

            # 找名稱：h1 標籤
            h1 = soup.find("h1")
            if h1:
                raw = h1.get_text(strip=True)
                # 去掉代號本身
                name = raw.replace(etf_code, "").strip()
                # 去掉「主動型」等標籤
                name = re.sub(r"主動型|被動型|ETF", "", name).strip()

            # 找投信：頁面通常有「發行公司」或投信名稱
            if not issuer:
                text = soup.get_text()
                issuer_m = re.search(r"([\w]+投信|[\w]+投顧)", text)
                if issuer_m:
                    issuer = issuer_m.group(1)

            # 從 meta description 也可能有名稱
            if not name:
                meta = soup.find("meta", {"name": "description"}) or                        soup.find("meta", {"property": "og:description"})
                if meta:
                    desc = meta.get("content", "")
                    name_m = re.search(r"(主動[\w]+|[\w]+主動式ETF)", desc)
                    if name_m:
                        name = name_m.group(1)
    except Exception as e:
        print(f"[lookup] etfinfo 失敗: {e}")

    return jsonify({
        "success": True,
        "code": etf_code,
        "name": name,
        "issuer": issuer,
        "found": bool(name),
    })


@app.route("/api/admin/etf/test/<etf_code>", methods=["GET"])
def admin_test_etf(etf_code):
    """自動偵測 ETF 最佳持股來源"""
    from flask import request as req
    if not verify_admin(req):
        return jsonify({"success": False, "error": "密碼錯誤"}), 401

    etf_code = etf_code.upper()
    result = detect_best_source(etf_code)
    if result:
        return jsonify({"success": True, "code": etf_code, **result})
    return jsonify({
        "success": False,
        "error": "無法從任何來源抓取，請確認代號是否正確（格式如 00981A）"
    })


@app.route("/api/etf/list", methods=["GET"])
def get_etf_list():
    """回傳所有支援的ETF清單"""
    cfg = get_etf_config()
    result = []
    for code, data in cfg.items():
        result.append({
            "code": code,
            "name": data["name"],
            "issuer": data["issuer"],
        })
    return jsonify({"success": True, "data": result})


@app.route("/api/etf/<etf_code>", methods=["GET"])
def get_etf_holdings(etf_code):
    """回傳指定ETF的持股明細"""
    etf_code = etf_code.upper()

    if etf_code not in ETF_CONFIG:
        return jsonify({"success": False, "error": f"不支援的ETF代號: {etf_code}"}), 404

    # 檢查快取
    if is_cache_valid(etf_code):
        print(f"[快取] {etf_code} 使用快取資料")
        return jsonify(cache[etf_code]["data"])

    # 執行爬蟲
    print(f"[爬蟲] 開始抓取 {etf_code} ...")
    cfg = get_etf_config().get(etf_code, ETF_CONFIG.get(etf_code, {}))
    holdings = None

    # 第一優先：etfinfo.tw（完整持股）
    try:
        holdings = cfg["scraper"]()
        if holdings:
            print(f"[etfinfo] {etf_code} 抓到 {len(holdings)} 檔")
    except Exception as e:
        print(f"[etfinfo] {etf_code} 失敗: {e}")

    # 第二優先：各投信官網備援
    if not holdings and cfg.get("fallback"):
        try:
            holdings = cfg["fallback"]()
            if holdings:
                print(f"[官網備援] {etf_code} 抓到 {len(holdings)} 檔")
        except Exception as e:
            print(f"[官網備援] {etf_code} 失敗: {e}")

    # 最後備援：靜態示範資料
    if not holdings:
        print(f"[靜態備援] {etf_code} 使用靜態資料")
        holdings = FALLBACK.get(etf_code, [])
        source = "demo"
    else:
        source = "live"
        # 儲存今日持股快照，並補建週初/月初種子快照（ON CONFLICT DO NOTHING）
        def _snapshot_tasks(code, h):
            save_holdings_snapshot(code, h)
            seed_period_snapshots(code, h)
        threading.Thread(target=_snapshot_tasks,
                         args=(etf_code, holdings), daemon=True).start()

    result = {
        "success": True,
        "code": etf_code,
        "name": cfg["name"],
        "issuer": cfg["issuer"],
        "source": source,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "announcement": get_announcement_label(etf_code),
        "holdings": holdings,
        "total_holdings": len(holdings),
    }

    # 存入快取
    cache[etf_code] = {"timestamp": time.time(), "data": result}
    return jsonify(result)


@app.route("/api/etf/all", methods=["GET"])
def get_all_etf():
    """一次抓取所有ETF（前端載入時使用）"""
    results = {}
    for code in ETF_CONFIG:
        # 重複利用單一ETF路由的邏輯
        resp = get_etf_holdings(code)
        results[code] = resp.get_json()
    return jsonify({"success": True, "data": results})


@app.route("/api/prices", methods=["GET"])
def get_all_prices():
    """抓取所有ETF股價與即時規模
    盤中（週一到週五 09:00-13:31）：Yahoo Finance V8 即時報價
    盤後/休市：etfinfo.tw 收盤資料
    """
    cache_key = "all_prices"
    _now = datetime.utcnow() + __import__('datetime').timedelta(hours=8)  # UTC+8 台灣時間
    _is_trading = (_now.weekday() < 5 and
                   (9*60) <= (_now.hour*60 + _now.minute) < (13*60+31))
    _ttl = 15 if _is_trading else 600
    if cache_key in cache and (time.time() - cache[cache_key]["timestamp"]) < _ttl:
        return jsonify(cache[cache_key]["data"])

    codes = list(ETF_CONFIG.keys())
    results = {}

    # 使用全域快取昨日收盤價（由背景執行緒每日更新）
    known_prices = {}
    for _code in codes:
        _prev = _prev_close_cache.get(_code)
        known_prices[_code] = {"prev": _prev}

    if _is_trading:
        # ── 盤中：優先使用 Yahoo Finance V8 即時股價 ──────────────────────
        for code in codes:
            try:
                pd = scrape_moneydj_price(code)
                if pd and pd.get("price") and float(pd["price"]) > 0:
                    price = float(pd["price"])
                    prev = known_prices.get(code, {}).get("prev") or pd.get("prev")  # 優先用_prev_close_cache，Yahoo Finance previousClose可能不準確
                    chg = round(price - prev, 2) if prev else None
                    chg_p = round(chg / prev * 100, 2) if (chg is not None and prev) else None
                    entry = {
                        "code": code, "price": price, "prev": prev,
                        "change": chg, "change_pct": chg_p,
                        "updated_at": datetime.now().strftime("%H:%M:%S") + "（即時）"
                    }
                    # 補上 AUM：從 _live_price_cache 或 etfinfo 快取取
                    _live = _live_price_cache.get(code, {})
                    if _live.get("aum"):
                        entry["aum"] = _live["aum"]
                    results[code] = entry
                    print(f"[Yahoo即時] {code}={price}")
            except Exception as e:
                print(f"[Yahoo即時] {code} 失敗: {e}")

        # 若 Yahoo 失敗，從 _live_price_cache 補救
        for code in codes:
            if code not in results:
                _live = _live_price_cache.get(code, {})
                _live_ts = _live.get("ts", 0)
                _live_age = time.time() - _live_ts if _live_ts else 9999
                if _live and _live.get("price") and _live_age < 300:
                    _sp = _live["price"]
                    _sv = _live.get("prev") or known_prices.get(code, {}).get("prev")
                    _sc = round(_sp - _sv, 2) if _sv else None
                    _scp = round(_sc / _sv * 100, 2) if (_sc is not None and _sv) else None
                    results[code] = {
                        "code": code, "price": _sp, "prev": _sv,
                        "change": _sc, "change_pct": _scp,
                        "updated_at": datetime.fromtimestamp(_live_ts).strftime("%H:%M:%S") + "（即時備援）"
                    }
                    if _live.get("aum"):
                        results[code]["aum"] = _live["aum"]

        # 最終備援：靜態預設值
        _static_prices = {
            "00403A": 10.47, "00981A": 31.54,
            "00991A": 19.10, "00992A": 18.01,
        }
        for code in codes:
            if code not in results:
                _sv = known_prices.get(code, {}).get("prev")
                _sp = _static_prices.get(code)
                if _sp and _sv:
                    _sc = round(_sp - _sv, 2)
                    _scp = round(_sc / _sv * 100, 2) if _sv else None
                    results[code] = {
                        "code": code, "price": _sp, "prev": _sv,
                        "change": _sc, "change_pct": _scp,
                        "updated_at": "靜態備援"
                    }

    else:
        # ── 盤後/休市：etfinfo.tw 股價 + AUM ──────────────────────
        for code in codes:
            try:
                url = f"https://www.etfinfo.tw/etf/{code}"
                r = safe_get(url, timeout=10)
                if not r:
                    continue
                soup = BeautifulSoup(r.text, "html.parser")
                price = None
                aum = None
                full_text = soup.get_text()

                # 股價解析：優先從 price-value class 取（處理 19.1 等一位小數）
                pv_tag = soup.find(class_="price-value")
                if pv_tag:
                    try:
                        v = float(pv_tag.get_text(strip=True))
                        if 5 < v < 5000:
                            price = v
                            print(f"[etfinfo price-value] {code}={price}")
                    except Exception:
                        pass
                if not price:
                    matches = re.findall(r"(\d{1,4}\.\d{1,2})\s*TWD", full_text)
                    if matches:
                        for m in matches:
                            v = float(m)
                            if 5 < v < 5000:
                                price = v
                                break
                if not price:
                    matches2 = re.findall(r"[▲▼]?\s*(\d{1,4}\.\d{1,2})\s*[\(（]", full_text)
                    if matches2:
                        v = float(matches2[0])
                        if 5 < v < 5000:
                            price = v

                # AUM 解析：etfinfo 格式「基金規模XXXX.X 億」
                aum_m = re.search(r'基金規模\s*([\d,]+\.?\d*)\s*億', full_text)
                if aum_m:
                    raw = aum_m.group(1).replace(',', '')
                    v = float(raw)
                    if v > 0.1:
                        aum = round(v, 1)
                        print(f"[etfinfo AUM] {code} = {aum}億")

                # 漲跌幅解析：從 change-row 取（格式：▲ 0.48 或 ▼ 0.37）
                chg_etfinfo = None
                chg_p_etfinfo = None
                change_tag = soup.find(class_="change-row")
                if change_tag:
                    try:
                        spans = change_tag.find_all("span")
                        if spans:
                            chg_text = spans[0].get_text(strip=True)  # "▲ 0.48" or "▼ 0.37"
                            sign = -1 if "▼" in chg_text or "-" in chg_text else 1
                            chg_val = re.search(r"(\d+\.\d+)", chg_text)
                            if chg_val:
                                chg_etfinfo = round(sign * float(chg_val.group(1)), 2)
                        if len(spans) >= 2:
                            pct_text = spans[1].get_text(strip=True)  # "(1.53%)"
                            pct_val = re.search(r"([\d.]+)%", pct_text)
                            if pct_val:
                                chg_p_etfinfo = round(sign * float(pct_val.group(1)), 2)
                        print(f"[etfinfo chg] {code} chg={chg_etfinfo} pct={chg_p_etfinfo}")
                    except Exception as _ce:
                        print(f"[etfinfo chg] {code} 解析失敗: {_ce}")

                if price:
                    # 優先用 _prev_close_cache 計算漲跌幅（最準確，人工維護）
                    # etfinfo SSR HTML 的 change-row 可能包含非最新日期的快取值，不可靠
                    known = known_prices.get(code, {})
                    prev = known.get("prev")
                    if prev:
                        chg = round(price - prev, 2)
                        chg_p = round(chg / prev * 100, 2)
                        print(f"[etfinfo] {code}={price} prev={prev} chg={chg}({chg_p}%)（_prev_close_cache計算）")
                    elif chg_etfinfo is not None and chg_p_etfinfo is not None:
                        # fallback: 用 etfinfo 解析的漲跌幅（prev_close_cache 無資料時才用）
                        chg = chg_etfinfo
                        chg_p = chg_p_etfinfo
                        prev = round(price - chg, 2) if chg is not None else None
                        print(f"[etfinfo] {code}={price} chg={chg}({chg_p}%) prev={prev}（etfinfo直接解析fallback）")
                    else:
                        chg = None
                        chg_p = None
                        prev = None
                        print(f"[etfinfo] {code}={price} 無法計算漲跌幅")
                    entry = {
                        "code": code, "price": price, "prev": prev,
                        "change": chg, "change_pct": chg_p,
                        "updated_at": datetime.now().strftime("%H:%M:%S") + "（etfinfo）"
                    }
                    if aum:
                        entry["aum"] = aum
                    results[code] = entry
            except Exception as e:
                print(f"[etfinfo] {code} 失敗: {e}")

        # 備援（etfinfo 抓取失敗時）：優先用即時快取，否則用靜態預設值
        _static_prices = {
            "00403A": 10.47, "00981A": 31.54,
            "00991A": 19.10, "00992A": 18.01,
        }
        for code in codes:
            if code not in results:
                _sv = known_prices.get(code, {}).get("prev")
                # 優先使用背景執行緒更新的即時股價
                _live = _live_price_cache.get(code, {})
                _live_ts = _live.get("ts", 0)
                _live_age = time.time() - _live_ts if _live_ts else 9999
                if _live and _live.get("price") and _live_age < 300:
                    # 即時快取未超過5分鐘，直接使用
                    _sp = _live["price"]
                    _sv2 = _live.get("prev") or _sv
                    _sc = round(_sp - _sv2, 2) if _sv2 else None
                    _scp = round(_sc / _sv2 * 100, 2) if (_sc is not None and _sv2) else None
                    results[code] = {
                        "code": code, "price": _sp, "prev": _sv2,
                        "change": _sc, "change_pct": _scp,
                        "updated_at": datetime.fromtimestamp(_live_ts).strftime("%H:%M:%S") + "（即時備援）"
                    }
                else:
                    # 最終備援：使用靜態預設值
                    _sp = _static_prices.get(code)
                    if _sp and _sv:
                        _sc = round(_sp - _sv, 2)
                        _scp = round(_sc / _sv * 100, 2) if _sv else None
                        results[code] = {
                            "code": code, "price": _sp, "prev": _sv,
                            "change": _sc, "change_pct": _scp,
                            "updated_at": "靜態備援"
                        }

    print(f"[股價/AUM] { {c:(results[c].get('price'), results[c].get('aum')) for c in results} }")
    result = {"success": True, "data": results, "updated_at": datetime.now().strftime("%H:%M:%S")}
    cache[cache_key] = {"timestamp": time.time(), "data": result}
    return jsonify(result)

@app.route("/api/price/<etf_code>", methods=["GET"])
def get_etf_price(etf_code):
    """單一ETF股價（從 all_prices 快取取得）"""
    etf_code = etf_code.upper()
    # 先嘗試從批次快取拿
    all_cache = cache.get("all_prices", {})
    if all_cache and (time.time() - all_cache["timestamp"]) < 180:
        info = all_cache["data"]["data"].get(etf_code)
        if info:
            return jsonify({"success": True, **info})
    # 否則觸發批次抓取
    resp = get_all_prices()
    data = resp.get_json()
    info = data.get("data", {}).get(etf_code)
    if info:
        return jsonify({"success": True, **info})
    return jsonify({"success": False, "code": etf_code, "price": None})





@app.route("/api/etf/<etf_code>/changes", methods=["GET"])
def get_etf_changes(etf_code):
    """回傳指定ETF的每日操作日報"""
    etf_code = etf_code.upper()
    if etf_code not in ETF_CONFIG:
        return jsonify({"success": False, "error": f"不支援的ETF代號: {etf_code}"}), 404

    cache_key = f"{etf_code}_changes"
    if is_cache_valid(cache_key):
        return jsonify(cache[cache_key]["data"])

    print(f"[操作日報] 抓取 {etf_code}...")
    changes = None

    # ── 先查 DB：最新快照（2天內），有則直接回傳避免重複爬取 ────────────
    try:
        from datetime import date as _dtoday
        _today_str = _dtoday.today().isoformat()   # Bug 2 修正：原先未定義
        _cutoff = (_dtoday.today() - __import__('datetime').timedelta(days=2)).isoformat()
        _dbconn = get_db()
        if _dbconn:
            try:
                _dc = _dbconn.cursor()  # Bug 4 修正：pg8000 cursor 不支援 context manager
                _dc.execute("""
                    SELECT date_range, add_count, buy_count, sell_count,
                           remove_count, buy_amount, sell_amount, changes_json
                    FROM etf_changes_history
                    WHERE etf_code=%s AND trade_date >= %s
                    ORDER BY trade_date DESC LIMIT 1
                """, (etf_code, _cutoff))
                _row = _dc.fetchone()
                if _row:
                    print(f"[操作日報] {etf_code} 命中 DB 快照（{_today_str}），直接回傳")
                    _cached = {
                        "date_range": _row[0] or "",
                        "add": _row[1] or 0, "buy": _row[2] or 0,
                        "sell": _row[3] or 0, "remove": _row[4] or 0,
                        "buy_amount": float(_row[5] or 0),
                        "sell_amount": float(_row[6] or 0),
                        "changes": json.loads(_row[7] or "[]")
                    }
                    _result_cached = {
                        "success": True, "code": etf_code,
                        "name": ETF_CONFIG[etf_code]["name"],
                        "source": "db_cache",
                        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "announcement": get_announcement_label(etf_code),
                        "data": _cached
                    }
                    cache[cache_key] = {"timestamp": time.time(), "data": _result_cached}
                    return jsonify(_result_cached)
            finally:
                _dbconn.close()
    except Exception as _dbe:
        print(f"[操作日報] DB 查詢失敗（繼續爬取）: {_dbe}")
    # ─────────────────────────────────────────────────────────────────────

    try:
        changes = scrape_daily_changes(etf_code)
    except Exception as e:
        print(f"[操作日報] {etf_code} 失敗: {e}")

    # ── 用對應日期收盤價補算每筆 amount 和總 buy/sell_amount ──────────
    if changes and changes.get("changes"):
        # Bug 3 修正：從 date_range 提取 trade_date（YYYYMMDD 格式供 get_stock_close 使用）
        _dr_str = changes.get("date_range", "")
        _dr_m = re.search(r"(\d{4}-\d{2}-\d{2})\s*[→>-]+\s*(\d{4}-\d{2}-\d{2})", _dr_str)
        trade_date = _dr_m.group(2).replace("-", "") if _dr_m else datetime.now().strftime("%Y%m%d")
        buy_total = 0.0
        sell_total = 0.0
        for ch in changes["changes"]:
            shares = ch.get("shares", 0)
            if shares == 0:
                ch["amount"] = ""
                continue
            stock_code = ch.get("code", "")
            price = get_stock_close(stock_code, trade_date)
            if price and price > 0:
                amt_wan = round(abs(shares) * 1000 * price / 10000, 2)
                ch["amount"] = f"+{amt_wan:.0f}萬" if shares > 0 else f"-{amt_wan:.0f}萬"
                ch["price"] = round(price, 2)
                if ch.get("type") in ("加碼", "新增"):
                    buy_total += amt_wan
                elif ch.get("type") in ("減碼", "刪除"):
                    sell_total += amt_wan
            else:
                ch["amount"] = ""
        if buy_total > 0:
            changes["buy_amount"] = round(buy_total / 10000, 2)
        if sell_total > 0:
            changes["sell_amount"] = round(sell_total / 10000, 2)
    # ────────────────────────────────────────────────────────────────

    result = {
        "success": True,
        "code": etf_code,
        "name": ETF_CONFIG[etf_code]["name"],
        "source": "live" if changes else "demo",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "announcement": get_announcement_label(etf_code),
        "data": changes or {
            "date_range": "資料載入中",
            "add": 0, "buy": 0, "sell": 0, "remove": 0,
            "buy_amount": 0.0, "sell_amount": 0.0,
            "changes": []
        }
    }
    # 儲存今日快照到 DB（live 資料才儲存）
    if changes:
        try:
            threading.Thread(target=save_changes_snapshot, args=(etf_code, changes), daemon=True).start()
        except Exception:
            pass

    cache[cache_key] = {"timestamp": time.time(), "data": result}
    return jsonify(result)


@app.route("/api/etf/<etf_code>/changes/history", methods=["GET"])
def get_etf_changes_history(etf_code):
    """
    個股進出記錄（滾動視窗）。
    week: 每次 7 天視窗；month: 每次 30 天視窗。
    offset=0 為最近一期，offset=1 往前推一個視窗，以此類推。
    """
    etf_code = etf_code.upper()
    if etf_code not in ETF_CONFIG:
        return jsonify({"success": False, "error": f"不支援的ETF代號: {etf_code}"}), 404

    from datetime import date as _date, timedelta as _td
    today = _date.today()
    period = request.args.get("period", "week")
    try:
        offset = int(request.args.get("offset", 0))
    except ValueError:
        offset = 0

    # 滾動視窗：week=7天，month=30天
    window = _td(days=7) if period == "week" else _td(days=30)
    period_end   = today - window * offset
    period_start = period_end - window + _td(days=1)

    if period == "week":
        period_label = f"{period_start.strftime('%m/%d')}～{period_end.strftime('%m/%d')}"
    else:
        period_label = f"{period_start.strftime('%Y/%m/%d')}～{period_end.strftime('%m/%d')}"

    # offset=0 時背景同步最新資料
    if offset == 0:
        threading.Thread(target=sync_trade_records, args=(etf_code,), daemon=True).start()

    result = get_period_trade_changes(etf_code, period_start, period_end)
    added   = result["added"]
    removed = result["removed"]

    def _fmt(s, action):
        return {
            "code": s["code"], "name": s["name"],
            "shares": 0, "pct_change": 0,
            "type": "新增" if action == "add" else "刪除",
            "entry_date": s.get("entry_date"),
            "exit_date":  s.get("exit_date"),
            "holding_days": s.get("holding_days"),
            "entry_price":  s.get("entry_price"),
        }

    changes = [_fmt(s, "add") for s in added] + [_fmt(s, "remove") for s in removed]
    has_data = bool(changes)

    return jsonify({
        "success": True,
        "code": etf_code,
        "name": ETF_CONFIG[etf_code]["name"],
        "period": period,
        "period_label": period_label,
        "period_start": period_start.isoformat(),
        "period_end":   period_end.isoformat(),
        "offset": offset,
        "trade_mode": True,
        "has_data": has_data,
        "data": [{
            "trade_date": period_end.isoformat(),
            "date_range": f"{period_start} → {period_end}",
            "add": len(added), "buy": 0, "sell": 0, "remove": len(removed),
            "buy_amount": 0.0, "sell_amount": 0.0,
            "changes": changes
        }] if has_data else []
    })


@app.route("/api/etf/<etf_code>/changes/all", methods=["GET"])
def get_all_trade_history(etf_code):
    """回傳該ETF自成立以來所有個股進出記錄，按進場日降冪排列。"""
    etf_code = etf_code.upper()
    if etf_code not in ETF_CONFIG:
        return jsonify({"success": False, "error": f"不支援的ETF代號: {etf_code}"}), 404

    # 確保資料最新
    threading.Thread(target=sync_trade_records, args=(etf_code,), daemon=True).start()

    conn = get_db()
    if not conn:
        return jsonify({"success": False, "error": "DB 連線失敗"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT stock_code, stock_name, entry_date, exit_date, holding_days, entry_price
            FROM etf_trade_records
            WHERE etf_code = %s
            ORDER BY entry_date DESC, stock_code
        """, (etf_code,))
        rows = cur.fetchall()
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        conn.close()

    records = []
    for r in rows:
        records.append({
            "code": r[0], "name": r[1],
            "entry_date": str(r[2]) if r[2] else None,
            "exit_date":  str(r[3]) if r[3] else None,
            "holding_days": r[4],
            "entry_price":  float(r[5]) if r[5] else None,
            "status": "持有中" if not r[3] else f"已出場（{r[4] or '?'}天）",
        })

    return jsonify({
        "success": True,
        "code": etf_code,
        "name": ETF_CONFIG[etf_code]["name"],
        "total": len(records),
        "records": records,
    })



@app.route("/api/debug/holdings/<etf_code>", methods=["GET"])
def debug_holdings(etf_code):
    """調試持股爬蟲"""
    import requests as rq
    log = []

    # 測試 Basic0007a
    url = f"https://www.moneydj.com/ETF/X/Basic/Basic0007a.xdjhtm?etfid={etf_code}.TW"
    try:
        r = rq.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=15, verify=False)
        log.append(f"Basic0007a status={r.status_code} len={len(r.text)}")

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")

        # 找 sdate3 錨點
        anchor = soup.find(id="ctl00_ctl00_MainContent_MainContent_sdate3")
        log.append(f"sdate3 anchor found: {anchor is not None}")

        if anchor:
            # 找 anchor 後面所有兄弟元素
            siblings = list(anchor.next_siblings)
            log.append(f"siblings count: {len(siblings)}")
            for i, sib in enumerate(siblings[:10]):
                tag = getattr(sib, "name", "text")
                cls = sib.get("class", []) if hasattr(sib, "get") else []
                sid = sib.get("id", "") if hasattr(sib, "get") else ""
                log.append(f"  sibling[{i}] tag={tag} id={sid!r} class={cls}")

            # 找 anchor 之後第一個含有 td 的 table（不限直接兄弟）
            all_tables = soup.find_all("table")
            log.append(f"Total tables: {len(all_tables)}")
            anchor_pos = str(anchor)
            found_tables = 0
            for i, t in enumerate(all_tables):
                tds = t.find_all("td")
                if len(tds) >= 4:
                    sample = tds[0].get_text(strip=True)[:15]
                    log.append(f"  table[{i}] tds={len(tds)} first={sample!r}")
                    found_tables += 1
                if found_tables >= 8:
                    break

    except Exception as e:
        import traceback
        log.append(f"ERROR: {e}")
        log.append(traceback.format_exc()[-300:])

    return jsonify({"log": log})


@app.route("/api/debug/prices", methods=["GET"])
def debug_prices():
    """診斷各股價來源是否可用"""
    log = []
    code = "00981A"
    ticker = f"{code}.TW"

    # 測試1：yfinance
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        info = t.fast_info
        price = getattr(info, "last_price", None)
        prev  = getattr(info, "previous_close", None)
        log.append(f"[yfinance] last_price={price}, prev={prev}")
        if not price:
            hist = t.history(period="2d")
            log.append(f"[yfinance] history rows={len(hist)}, last={hist['Close'].iloc[-1] if not hist.empty else 'empty'}")
    except Exception as e:
        import traceback
        log.append(f"[yfinance] ERROR: {e}")
        log.append(traceback.format_exc()[-300:])

    # 測試2：Yahoo v8 API
    import requests as rq, urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    for url in [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d",
    ]:
        try:
            r = rq.get(url, headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"},
                       timeout=8, verify=False)
            log.append(f"[YahooV8] {url[-30:]} status={r.status_code} len={len(r.text)}")
            if r.status_code == 200:
                meta = r.json()["chart"]["result"][0]["meta"]
                log.append(f"[YahooV8] price={meta.get('regularMarketPrice')} prev={meta.get('previousClose')}")
        except Exception as e:
            log.append(f"[YahooV8] ERROR: {e}")

    # 測試3：Yahoo v7
    try:
        r3 = rq.get(f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={ticker}",
                    headers={"User-Agent":"Mozilla/5.0"}, timeout=8, verify=False)
        log.append(f"[YahooV7] status={r3.status_code}")
        if r3.status_code == 200:
            q = r3.json().get("quoteResponse",{}).get("result",[{}])[0]
            log.append(f"[YahooV7] price={q.get('regularMarketPrice')} prev={q.get('regularMarketPreviousClose')}")
    except Exception as e:
        log.append(f"[YahooV7] ERROR: {e}")

    # 測試4：mis.twse
    try:
        r4 = rq.get(f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{code}.tw&json=1&delay=0",
                    headers={"User-Agent":"Mozilla/5.0","Referer":"https://mis.twse.com.tw/"},
                    timeout=8, verify=False)
        log.append(f"[mis.twse] status={r4.status_code} len={len(r4.text)} text[:80]={r4.text[:80]!r}")
    except Exception as e:
        log.append(f"[mis.twse] ERROR: {e}")

    # 測試5：TWSE STOCK_DAY
    try:
        from datetime import datetime as _dt
        ym = _dt.now().strftime("%Y%m%d")
        r5 = rq.get(f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={ym}&stockNo={code}",
                    headers={"User-Agent":"Mozilla/5.0"}, timeout=8, verify=False)
        log.append(f"[TWSE_DAY] status={r5.status_code}")
        if r5.status_code == 200:
            d5 = r5.json()
            log.append(f"[TWSE_DAY] stat={d5.get('stat')} rows={len(d5.get('data',[]))}")
    except Exception as e:
        log.append(f"[TWSE_DAY] ERROR: {e}")

    return jsonify({"log": log})


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

@app.route("/api/debug/aum/<etf_code>", methods=["GET"])
def debug_aum(etf_code):
    """Debug: 測試 etfinfo.tw AUM 解析，回傳 full_text 中基金規模位置"""
    etf_code = etf_code.upper()
    result = {"code": etf_code}
    try:
        url = f"https://www.etfinfo.tw/etf/{etf_code}"
        r = safe_get(url, timeout=12)
        if not r:
            result["error"] = "safe_get 失敗"
            return jsonify(result)
        soup = BeautifulSoup(r.text, "html.parser")
        full_text = soup.get_text()
        result["full_text_len"] = len(full_text)

        # 找「基金規模」的位置
        idx = full_text.find('基金規模')
        result["jijin_idx"] = idx
        if idx >= 0:
            result["jijin_context"] = full_text[max(0,idx-10):idx+30]

        # 直接測試 regex
        aum_m = re.search(r'基金規模\s*([\d,]+\.?\d*)\s*億', full_text)
        result["regex_match"] = aum_m.group(0) if aum_m else None
        result["regex_aum"] = aum_m.group(1) if aum_m else None

        # 找含億的文字
        billion_ctx = re.findall(r'.{0,20}億.{0,20}', full_text)
        result["billion_contexts"] = billion_ctx[:5]

        return jsonify(result)
    except Exception as e:
        result["error"] = str(e)
        return jsonify(result)








# ──────────────────────────────────────────
# 背景預熱：伺服器啟動後自動抓一次
# ──────────────────────────────────────────
def warmup():
    time.sleep(2)
    print("[預熱] 開始背景抓取所有ETF...")
    with app.app_context():
        for code in ETF_CONFIG:
            get_etf_holdings(code)
    print("[預熱] 完成")

def _update_live_prices():
    """背景執行緒：交易時間每60秒透過Yahoo Finance V8更新各ETF即時股價
    供 etfinfo 抓取失敗時作備援，確保漲跌幅計算使用最新價格"""
    import time as _time
    print("[live_price] 即時股價更新執行緒啟動")
    while True:
        try:
            from datetime import datetime as _dt
            now = _dt.utcnow() + __import__('datetime').timedelta(hours=8)
            _is_trading = (now.weekday() < 5 and
                           (9*60) <= (now.hour*60 + now.minute) < (13*60+35))
            if _is_trading:
                for _code in list(_prev_close_cache.keys()):
                    try:
                        pd = scrape_moneydj_price(_code)
                        if pd and pd.get("price") and float(pd["price"]) > 0:
                            _live_price_cache[_code] = {
                                "price": pd["price"],
                                "prev":  pd.get("prev") or _prev_close_cache.get(_code),
                                "ts":    _time.time()
                            }
                            print(f"[live_price] {_code}={pd['price']} prev={pd.get('prev')}")
                    except Exception as _e:
                        print(f"[live_price] {_code} 失敗: {_e}")
        except Exception as e:
            print(f"[live_price] 執行緒錯誤: {e}")
        _time.sleep(60)


def _update_prev_close():
    """每日兩次更新各ETF昨日收盤價快取：
    1. 09:01 開盤前：抓取前一交易日收盤價作為今日昨收基準
    2. 13:35 收盤後：抓取今日收盤價，更新為明日昨收基準
    """
    global _prev_close_cache
    import time as _time
    _last_open_date = None   # 09:01 更新記錄
    _last_close_date = None  # 13:35 更新記錄

    # ── 服務啟動時立即初始化昨收快取（優先用 TWSE 倒數第二日收盤）──────
    print("[prev_close] 服務啟動，初始化昨收快取...")
    _init_cache = {}
    for _code in list(_prev_close_cache.keys()):
        try:
            from datetime import datetime as _dtt, timedelta as _tdd
            import urllib3 as _ul3; _ul3.disable_warnings()
            import requests as _rqs
            _chk = _dtt.utcnow() + _tdd(hours=8)
            for _retry in range(3):
                _ym = _chk.strftime("%Y%m%d")
                _resp = _rqs.get(
                    f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
                    f"?stockNo={_code}&response=json&date={_ym}",
                    timeout=10, verify=False)
                if _resp.status_code == 200:
                    _jd = _resp.json()
                    if _jd.get("stat") == "OK" and _jd.get("data") and len(_jd["data"]) >= 2:
                        _rows = _jd["data"]
                        # 倒數第二列收盤 = 最近一個交易日的昨收
                        _prev_close = float(_rows[-2][6].replace(",", ""))
                        _init_cache[_code] = _prev_close
                        print(f"[prev_close] 啟動(TWSE) {_code}={_prev_close}")
                        break
                _chk = (_chk.replace(day=1) - _tdd(days=1))
        except Exception as _e:
            print(f"[prev_close] 啟動TWSE {_code} 失敗: {_e}")
            try:
                _pd = scrape_moneydj_price(_code)
                if _pd and _pd.get("prev") and float(_pd["prev"]) > 0:
                    _init_cache[_code] = _pd["prev"]
                    print(f"[prev_close] 啟動Yahoo降級 {_code}={_pd['prev']}")
            except Exception as _e2:
                print(f"[prev_close] 啟動全失敗 {_code}: {_e2}")
    if _init_cache:
        _prev_close_cache.update(_init_cache)
        print(f"[prev_close] 啟動初始化完成: {_prev_close_cache}")

    while True:
        try:
            from datetime import datetime as _dt
            now = _dt.utcnow() + __import__('datetime').timedelta(hours=8)
            today_str = now.strftime("%Y-%m-%d")
            if now.weekday() < 5:
                # ── 觸發點1：09:01 開盤前（抓昨日收盤作為今日昨收基準）──
                if now.hour == 9 and now.minute == 1 and _last_open_date != today_str:
                    print("[prev_close] 09:01 開始更新昨收...")
                    new_cache = {}
                    for _code in list(_prev_close_cache.keys()):
                        try:
                            pd = scrape_moneydj_price(_code)
                            if pd and pd.get("prev") and float(pd["prev"]) > 0:
                                new_cache[_code] = pd["prev"]
                                print(f"[prev_close] 09:01 {_code} 昨收={pd['prev']}")
                        except Exception as _e:
                            print(f"[prev_close] {_code} 失敗: {_e}")
                    if new_cache:
                        _prev_close_cache.update(new_cache)
                    _last_open_date = today_str
                    print(f"[prev_close] 09:01 更新完成: {_prev_close_cache}")
                # ── 觸發點2：13:35 收盤後（抓今日收盤，更新為明日昨收基準）──
                elif now.hour == 13 and now.minute == 35 and _last_close_date != today_str:
                    print("[prev_close] 13:35 收盤後更新今日收盤價...")
                    new_cache = {}
                    for _code in list(_prev_close_cache.keys()):
                        try:
                            pd = scrape_moneydj_price(_code)
                            if pd and pd.get("price") and float(pd["price"]) > 0:
                                new_cache[_code] = pd["price"]  # 今日收盤 = 明日昨收
                                print(f"[prev_close] 13:35 {_code} 今收={pd['price']}（更新為明日昨收）")
                        except Exception as _e:
                            print(f"[prev_close] {_code} 失敗: {_e}")
                    if new_cache:
                        _prev_close_cache.update(new_cache)
                    _last_close_date = today_str
                    print(f"[prev_close] 13:35 更新完成: {_prev_close_cache}")
        except Exception as e:
            print(f"[prev_close] 更新執行緒錯誤: {e}")
        _time.sleep(60)


def _scheduled_refresh():
    """背景排程：依各投信公告時間，於公告後自動清除快取並重抓持股"""
    import time as _time
    print("[排程] 持股自動更新排程啟動")
    while True:
        try:
            now = datetime.now()
            if now.weekday() < 5:
                for code, sch in ETF_ANNOUNCEMENT_SCHEDULE.items():
                    ah, am = sch["announce_hour"], sch["announce_min"]
                    if now.hour == ah and am <= now.minute < am + 2:
                        with app.app_context():
                            if code in cache:
                                del cache[code]
                                print(f"[排程] 清除 {code} 快取，觸發重抓（{ah:02d}:{am:02d} 公告後）")
                            try:
                                get_etf_holdings(code)
                                ck = f"{code}_changes"
                                if ck in cache:
                                    del cache[ck]
                            except Exception as e:
                                print(f"[排程] {code} 重抓失敗: {e}")
        except Exception as e:
            print(f"[排程] 錯誤: {e}")
        _time.sleep(60)


# ══════════════════════════════════════════════════════════════════════
# AI 題材股調查系統
# ══════════════════════════════════════════════════════════════════════


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    t = threading.Thread(target=warmup, daemon=True)
    t.start()
    t2 = threading.Thread(target=_scheduled_refresh, daemon=True)
    t2.start()
    t3 = threading.Thread(target=_update_prev_close, daemon=True)
    t3.start()
    t4 = threading.Thread(target=_update_live_prices, daemon=True)
    t4.start()
    t5 = threading.Thread(target=_ai_stock_weekly_scheduler, daemon=True)
    t5.start()
    print("=" * 50)
    print("  ETF追蹤器後端已啟動")
    print(f"  API: http://localhost:{port}/api/etf/list")
    print("  停止伺服器: 按 Ctrl+C")
    print("=" * 50)
    app.run(host="0.0.0.0", port=port, debug=False)
