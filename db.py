"""
資料庫連線與初始化（Supabase PostgreSQL via pg8000）
"""
import os
import json
import pg8000
import bcrypt


DATABASE_URL = os.environ.get("DATABASE_URL", "")


def get_db():
    """取得資料庫連線（pg8000，相容 Python 3.14）"""
    if not DATABASE_URL:
        print("[DB] DATABASE_URL 未設定")
        return None
    try:
        import urllib.parse, ssl
        p = urllib.parse.urlparse(DATABASE_URL)
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        conn = pg8000.connect(
            host=p.hostname,
            port=p.port or 5432,
            database=p.path.lstrip("/"),
            user=p.username,
            password=p.password,
            timeout=10,
            ssl_context=ssl_ctx,
        )
        print(f"[DB] 連線成功: {p.hostname}")
        return conn
    except Exception as e:
        print(f"[DB] 連線失敗: {type(e).__name__}: {e}")
        import traceback; traceback.print_exc()
        return None


def init_db():
    """初始化資料庫表格"""
    conn = get_db()
    if not conn:
        print("[DB] 無法初始化，跳過")
        return
    try:
        cur = conn.cursor()
        # 用戶表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(50) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # 持倉紀錄表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS portfolios (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                etf_code VARCHAR(10) NOT NULL,
                qty NUMERIC(10,2) DEFAULT 0,
                avg_price NUMERIC(10,4) DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, etf_code)
            )
        """)
        # 主題設定表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS themes (
                id SERIAL PRIMARY KEY,
                user_id INTEGER UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                theme_json TEXT DEFAULT '{}',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # ETF設定表（移自環境變數）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS etf_configs (
                id SERIAL PRIMARY KEY,
                code VARCHAR(10) UNIQUE NOT NULL,
                name VARCHAR(100) NOT NULL,
                issuer VARCHAR(100) DEFAULT '',
                source_key VARCHAR(20) DEFAULT 'etfinfo',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # 每日操作日報快照表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS etf_changes_history (
                id SERIAL PRIMARY KEY,
                etf_code VARCHAR(10) NOT NULL,
                trade_date DATE NOT NULL,
                date_range VARCHAR(50) DEFAULT '',
                add_count INTEGER DEFAULT 0,
                buy_count INTEGER DEFAULT 0,
                sell_count INTEGER DEFAULT 0,
                remove_count INTEGER DEFAULT 0,
                buy_amount NUMERIC(12,4) DEFAULT 0,
                sell_amount NUMERIC(12,4) DEFAULT 0,
                changes_json TEXT DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(etf_code, trade_date)
            )
        """)
        # 個股收盤價快取表（避免重複查詢 TWSE/Yahoo）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stock_close_cache (
                id SERIAL PRIMARY KEY,
                stock_code VARCHAR(10) NOT NULL,
                trade_date DATE NOT NULL,
                close_price NUMERIC(10,2) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(stock_code, trade_date)
            )
        """)
        # AI 題材股每週精選資料表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_stock_picks (
                id SERIAL PRIMARY KEY,
                week_start DATE NOT NULL,
                stock_code VARCHAR(10) NOT NULL,
                stock_name VARCHAR(100) DEFAULT '',
                rank_order INTEGER DEFAULT 0,
                reason TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                sources_json TEXT DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(week_start, stock_code)
            )
        """)
        # AI 題材股基本面資料快取表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_stock_data (
                id SERIAL PRIMARY KEY,
                stock_code VARCHAR(10) UNIQUE NOT NULL,
                stock_name VARCHAR(100) DEFAULT '',
                price NUMERIC(10,2),
                change_val NUMERIC(10,2),
                change_pct NUMERIC(8,4),
                eps_ttm NUMERIC(10,2),
                eps_growth_pct NUMERIC(8,2),
                roe NUMERIC(8,2),
                revenue_growth_pct NUMERIC(8,2),
                net_margin NUMERIC(8,2),
                capex_b NUMERIC(10,2),
                market_cap_b NUMERIC(10,2),
                pe_ratio NUMERIC(10,2),
                extra_json TEXT DEFAULT '{}',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        print("[DB] 資料表初始化完成")

        # 建立預設管理員帳號（若不存在）
        cur.execute("SELECT id FROM users WHERE username = 'admin'")
        if not cur.fetchone():
            default_pwd = os.environ.get("ADMIN_PASSWORD", "etf2026")
            pwd_hash = bcrypt.hashpw(default_pwd.encode(), bcrypt.gensalt()).decode()
            cur.execute(
                "INSERT INTO users (username, password_hash) VALUES (%s, %s)",
                ("admin", pwd_hash)
            )
            conn.commit()
            print(f"[DB] 建立預設管理員帳號 admin / {default_pwd}")

        # 建立測試帳號 test1、test2（若不存在）
        test_pwd = "987654"
        test_pwd_hash = bcrypt.hashpw(test_pwd.encode(), bcrypt.gensalt()).decode()
        for _tuser in ["test1", "test2"]:
            cur.execute("SELECT id FROM users WHERE username = %s", (_tuser,))
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO users (username, password_hash) VALUES (%s, %s)",
                    (_tuser, test_pwd_hash)
                )
                conn.commit()
                print(f"[DB] 建立測試帳號 {_tuser} / {test_pwd}")

        # 把現有 ETF_LIST_JSON 環境變數遷移到資料庫
        env_json = os.environ.get("ETF_LIST_JSON", "")
        if env_json:
            try:
                etf_list = json.loads(env_json)
                for etf in etf_list:
                    cur.execute("""
                        INSERT INTO etf_configs (code, name, issuer, source_key)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (code) DO NOTHING
                    """, (etf["code"], etf["name"], etf.get("issuer",""), etf.get("source_key","etfinfo")))
                conn.commit()
                print(f"[DB] 遷移 {len(etf_list)} 檔 ETF 設定")
            except Exception as e:
                print(f"[DB] 遷移失敗: {e}")
        else:
            # 插入預設 ETF（若表格空）
            cur.execute("SELECT COUNT(*) FROM etf_configs")
            if cur.fetchone()[0] == 0:
                default_etfs = [
                    ("00992A","主動群益科技創新","群益投信","etfinfo"),
                    ("00991A","主動復華未來50","復華投信","etfinfo"),
                    ("00981A","主動統一台股增長","統一投信","etfinfo"),
                    ("00403A","主動統一升級50","統一投信","etfinfo"),
                ]
                cur.executemany(
                    "INSERT INTO etf_configs (code,name,issuer,source_key) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    default_etfs
                )
                conn.commit()
                print("[DB] 插入預設 4 檔 ETF")
    except Exception as e:
        print(f"[DB] 初始化錯誤: {e}")
        import traceback; traceback.print_exc()
    finally:
        conn.close()
