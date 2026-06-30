"""
認證、持倉、主題 API Blueprint
/api/auth/*  /api/portfolio  /api/theme
"""
import json
import bcrypt
from flask import Blueprint, request, jsonify, session

from db import get_db

auth_bp = Blueprint("auth", __name__)


# ──────────────────────────────────────────
# 認證 API
# ──────────────────────────────────────────

@auth_bp.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return jsonify({"success": False, "error": "請輸入帳號和密碼"}), 400
    conn = get_db()
    if not conn:
        return jsonify({"success": False, "error": "資料庫連線失敗"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, username, password_hash FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
        if not row:
            return jsonify({"success": False, "error": "帳號或密碼錯誤"}), 401
        uid, uname, uhash = row[0], row[1], row[2]
        if not bcrypt.checkpw(password.encode(), uhash.encode()):
            return jsonify({"success": False, "error": "帳號或密碼錯誤"}), 401
        session["user_id"] = uid
        session["username"] = uname
        session.permanent = True
        return jsonify({"success": True, "username": uname})
    finally:
        conn.close()


@auth_bp.route("/api/auth/register", methods=["POST"])
def auth_register():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password or len(password) < 6:
        return jsonify({"success": False, "error": "帳號或密碼格式不正確（密碼至少6字元）"}), 400
    conn = get_db()
    if not conn:
        return jsonify({"success": False, "error": "資料庫連線失敗"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE username=%s", (username,))
        if cur.fetchone():
            return jsonify({"success": False, "error": "帳號已存在"}), 409
        pwd_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        cur.execute("INSERT INTO users (username, password_hash) VALUES (%s,%s)", (username, pwd_hash))
        conn.commit()
        cur.execute("SELECT id, username FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
        session["user_id"] = row[0]
        session["username"] = row[1]
        session.permanent = True
        return jsonify({"success": True, "username": username})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        conn.close()


@auth_bp.route("/api/auth/reset_password", methods=["POST"])
def auth_reset_password():
    data = request.get_json(silent=True) or {}
    username  = data.get("username", "").strip()
    new_pwd   = data.get("new_password", "").strip()
    admin_pwd = data.get("admin_password", "").strip()
    if not all([username, new_pwd, admin_pwd]) or len(new_pwd) < 6:
        return jsonify({"success": False, "error": "欄位不完整或密碼太短"}), 400
    conn = get_db()
    if not conn:
        return jsonify({"success": False, "error": "資料庫連線失敗"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM users WHERE username='admin'")
        row = cur.fetchone()
        if not row or not bcrypt.checkpw(admin_pwd.encode(), row[0].encode()):
            return jsonify({"success": False, "error": "管理員密碼錯誤"}), 401
        new_hash = bcrypt.hashpw(new_pwd.encode(), bcrypt.gensalt()).decode()
        cur.execute("UPDATE users SET password_hash=%s WHERE username=%s", (new_hash, username))
        if cur.rowcount == 0:
            return jsonify({"success": False, "error": "帳號不存在"}), 404
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()


@auth_bp.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"success": True})


@auth_bp.route("/api/auth/me", methods=["GET"])
def auth_me():
    if "user_id" not in session:
        return jsonify({"success": False, "error": "未登入"}), 401
    return jsonify({"success": True, "username": session.get("username")})


@auth_bp.route("/api/auth/change_password", methods=["POST"])
def auth_change_password():
    if "user_id" not in session:
        return jsonify({"success": False, "error": "未登入"}), 401
    data = request.get_json(silent=True) or {}
    old_pwd = data.get("old_password", "")
    new_pwd = data.get("new_password", "")
    if not old_pwd or not new_pwd or len(new_pwd) < 4:
        return jsonify({"success": False, "error": "密碼格式不正確（至少4字元）"}), 400
    conn = get_db()
    if not conn:
        return jsonify({"success": False, "error": "資料庫連線失敗"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM users WHERE id=%s", (session["user_id"],))
        row = cur.fetchone()
        if not row or not bcrypt.checkpw(old_pwd.encode(), row[0].encode()):
            return jsonify({"success": False, "error": "舊密碼錯誤"}), 401
        new_hash = bcrypt.hashpw(new_pwd.encode(), bcrypt.gensalt()).decode()
        cur.execute("UPDATE users SET password_hash=%s WHERE id=%s", (new_hash, session["user_id"]))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()


# ──────────────────────────────────────────
# 持倉 API
# ──────────────────────────────────────────

@auth_bp.route("/api/portfolio", methods=["GET"])
def get_portfolio():
    if "user_id" not in session:
        return jsonify({"success": False, "error": "未登入"}), 401
    conn = get_db()
    if not conn:
        return jsonify({"success": False, "data": {}}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT etf_code,qty,avg_price FROM portfolios WHERE user_id=%s", (session["user_id"],))
        rows = cur.fetchall()
        data = {r[0]: {"qty": float(r[1]), "avg": float(r[2])} for r in rows}  # Bug 5 修正：pg8000 回傳 list，不支援字串鍵
        return jsonify({"success": True, "data": data})
    finally:
        conn.close()


@auth_bp.route("/api/portfolio", methods=["POST"])
def save_portfolio():
    if "user_id" not in session:
        return jsonify({"success": False, "error": "未登入"}), 401
    data = request.get_json(silent=True) or {}
    portfolio = data.get("portfolio", {})
    conn = get_db()
    if not conn:
        return jsonify({"success": False, "error": "資料庫連線失敗"}), 500
    try:
        cur = conn.cursor()
        for code, info in portfolio.items():
            qty = float(info.get("qty", 0))
            avg = float(info.get("avg", 0))
            cur.execute("""
                INSERT INTO portfolios (user_id, etf_code, qty, avg_price, updated_at)
                VALUES (%s,%s,%s,%s,NOW())
                ON CONFLICT (user_id, etf_code)
                DO UPDATE SET qty=%s, avg_price=%s, updated_at=NOW()
            """, (session["user_id"], code, qty, avg, qty, avg))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()


# ──────────────────────────────────────────
# 主題 API
# ──────────────────────────────────────────

@auth_bp.route("/api/theme", methods=["GET"])
def get_theme():
    if "user_id" not in session:
        return jsonify({"success": False, "data": {}})
    conn = get_db()
    if not conn:
        return jsonify({"success": False, "data": {}})
    try:
        cur = conn.cursor()
        cur.execute("SELECT theme_json FROM themes WHERE user_id=%s", (session["user_id"],))
        row = cur.fetchone()
        theme = json.loads(row[0]) if row else {}  # Bug 6 修正：pg8000 回傳 list，不支援字串鍵
        return jsonify({"success": True, "data": theme})
    finally:
        conn.close()


@auth_bp.route("/api/theme", methods=["POST"])
def save_theme_api():
    if "user_id" not in session:
        return jsonify({"success": False, "error": "未登入"}), 401
    data = request.get_json(silent=True) or {}
    theme_json = json.dumps(data.get("theme", {}), ensure_ascii=False)
    conn = get_db()
    if not conn:
        return jsonify({"success": False, "error": "資料庫連線失敗"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO themes (user_id, theme_json, updated_at)
            VALUES (%s,%s,NOW())
            ON CONFLICT (user_id)
            DO UPDATE SET theme_json=%s, updated_at=NOW()
        """, (session["user_id"], theme_json, theme_json))
        conn.commit()
        return jsonify({"success": True})
    finally:
        conn.close()
