"""
頁面路由 Blueprint
/ /login /admin  → 回傳 static/ 下的 HTML 檔案
"""
import os
from flask import Blueprint, Response, redirect, session, current_app

pages_bp = Blueprint("pages", __name__)


def _serve_html(filename):
    path = os.path.join(current_app.root_path, "static", filename)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return None


@pages_bp.route("/login")
def login_page():
    html = _serve_html("login.html")
    if html:
        return Response(html, mimetype="text/html")
    return "Login page not found", 404


@pages_bp.route("/admin")
def admin():
    if "user_id" not in session:
        return redirect("/login")
    html = _serve_html("admin.html")
    if html:
        return Response(html, mimetype="text/html")
    return "Admin page not found", 404


@pages_bp.route("/")
def index():
    if "user_id" not in session:
        return redirect("/login")
    html = _serve_html("index.html")
    if html:
        html = html.replace(
            'const API = "https://etf-tracker-1.onrender.com/api";',
            'const API = "/api";'
        )
        return Response(html, mimetype="text/html")
    return "ETF Tracker - index.html not found", 404
