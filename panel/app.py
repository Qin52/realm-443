#!/usr/bin/env python3
"""GOST SNI Manager carpool edition.

A lightweight multi-user panel for managing GOST v3 SNI forwarding rules.
GOST listens on 443 and dispatches by TLS SNI. The panel supports two modes:
self-use mode forwards directly to landing VPS targets for maximum performance;
carpool mode forwards through a local managed TCP relay for bandwidth, traffic
quota, account expiry, and per-user rule limits.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import random
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

APP_NAME = "GOST SNI Manager"
APP_VERSION = "2.2.0"
DATA_DIR = Path(os.environ.get("GOST_PANEL_DATA", "/etc/gost-panel"))
DB_FILE = Path(os.environ.get("GOST_PANEL_DB", str(DATA_DIR / "gsm.db")))
SECRET_FILE = Path(os.environ.get("GOST_PANEL_SECRET", str(DATA_DIR / "secret.key")))
GOST_CONFIG = Path(os.environ.get("GOST_CONFIG", "/etc/gost/config.yaml"))
GOST_SERVICE = os.environ.get("GOST_SERVICE", "gost")
RELAY_SERVICE = os.environ.get("GSM_RELAY_SERVICE", "gsm-relay")
CADDY_SERVICE = os.environ.get("CADDY_SERVICE", "caddy")
DEFAULT_LISTEN = os.environ.get("GOST_LISTEN", ":443")
DEFAULT_FALLBACK = os.environ.get("GOST_FALLBACK", "127.0.0.1:8053")
RELAY_HOST = os.environ.get("GSM_RELAY_HOST", "127.0.0.1")
RELAY_PORT_MIN = int(os.environ.get("GSM_RELAY_PORT_MIN", "56000"))
RELAY_PORT_MAX = int(os.environ.get("GSM_RELAY_PORT_MAX", "60999"))
PANEL_PORT = int(os.environ.get("PANEL_PORT", os.environ.get("GOST_PANEL_PORT", "7080")))
CADDY_HTTPS_PORT = int(os.environ.get("CADDY_HTTPS_PORT", "8053"))
APPLY_DELAY = float(os.environ.get("GOST_APPLY_DELAY", "2.5"))
PANEL_DOMAIN = os.environ.get("PANEL_DOMAIN", "").strip().lower()

DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$|^\[[0-9A-Fa-f:.]+\]$")
LISTEN_RE = re.compile(r"^(:\d{1,5}|[A-Za-z0-9.:-]+:\d{1,5}|\[[0-9A-Fa-f:.]+\]:\d{1,5})$")


def create_app() -> Flask:
    ensure_dirs()
    init_db()
    app = Flask(__name__)
    app.secret_key = read_or_create_secret()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=3600 * 8,
    )

    @app.context_processor
    def inject_globals() -> Dict[str, Any]:
        user = get_current_user()
        return {
            "app_name": APP_NAME,
            "version": APP_VERSION,
            "csrf_token": get_csrf_token(),
            "current_user": user,
            "is_admin": bool(user and user.get("role") == "admin"),
            "fmt_bytes": fmt_bytes,
            "fmt_gb": fmt_gb,
            "fmt_dt": fmt_dt,
            "fmt_limit": fmt_limit,
            "account_usage": account_usage if user else None,
        }

    @app.get("/login")
    def login_page() -> str | Response:
        if is_logged_in():
            return redirect(url_for("dashboard"))
        return render_template("login.html")

    @app.post("/login")
    def login_submit() -> Response | str:
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        time.sleep(0.15)
        user = find_user_by_username(username)
        if not user or not check_password_hash(user.get("password_hash", ""), password):
            flash("用户名或密码错误。", "error")
            return render_template("login.html"), 401
        if not user_is_active(user):
            flash("账号已禁用或已过期，请联系主账号。", "error")
            return render_template("login.html"), 403
        session.clear()
        session["uid"] = user["id"]
        session["csrf"] = secrets.token_urlsafe(32)
        flash("登录成功。", "success")
        return redirect(url_for("dashboard"))

    @app.post("/logout")
    @require_login
    @require_csrf
    def logout() -> Response:
        session.clear()
        flash("已退出登录。", "success")
        return redirect(url_for("login_page"))

    @app.get("/")
    @require_login
    def dashboard() -> str:
        user = require_current_user()
        stats = dashboard_stats(user)
        rules = list_rules_for_user(user)
        users = list_users() if user["role"] == "admin" else []
        status = collect_status()
        settings = load_settings()
        return render_template(
            "dashboard.html",
            stats=stats,
            rules=rules,
            users=users,
            status=status,
            settings=settings,
            apply_state=dict(APPLY_STATE),
            default_port=default_listen_port(),
        )

    @app.post("/rules")
    @require_login
    @require_csrf
    def add_rule() -> Response:
        user = require_current_user()
        try:
            owner_id = request.form.get("owner_id", user["id"]).strip() if user["role"] == "admin" else user["id"]
            owner = get_user(owner_id)
            if not owner:
                raise ValueError("找不到规则所属账号。")
            if user["role"] != "admin" and owner_id != user["id"]:
                raise ValueError("无权给其他账号添加规则。")
            if owner["role"] != "admin" and not user_is_active(owner):
                raise ValueError("该子账号已禁用或过期，不能添加规则。")
            enforce_rule_count(owner)
            rule = parse_rule_form(request.form, owner, actor=user, existing_rule=None)
            rule["id"] = uuid.uuid4().hex[:12]
            rule["owner_id"] = owner_id
            rule["relay_port"] = allocate_relay_port()
            insert_rule(rule)
            apply_config(restart=True)
            flash("规则已添加，配置将在几秒内自动生效。", "success")
        except (ValueError, RuntimeError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("dashboard"))

    @app.post("/rules/<rule_id>/update")
    @require_login
    @require_csrf
    def update_rule(rule_id: str) -> Response:
        user = require_current_user()
        rule = get_rule(rule_id)
        if not rule or not can_manage_rule(user, rule):
            flash("找不到这条规则，或无权操作。", "error")
            return redirect(url_for("dashboard"))
        try:
            owner = get_user(rule["owner_id"])
            if not owner:
                raise ValueError("找不到规则所属账号。")
            new_rule = parse_rule_form(request.form, owner, actor=user, existing_rule=rule)
            update_rule_row(rule_id, new_rule)
            apply_config(restart=True)
            flash("规则已更新，配置将在几秒内自动生效。", "success")
        except (ValueError, RuntimeError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("dashboard"))

    @app.post("/rules/<rule_id>/toggle")
    @require_login
    @require_csrf
    def toggle_rule(rule_id: str) -> Response:
        user = require_current_user()
        rule = get_rule(rule_id)
        if not rule or not can_manage_rule(user, rule):
            flash("找不到这条规则，或无权操作。", "error")
            return redirect(url_for("dashboard"))
        enabled = 0 if int(rule.get("enabled", 1)) else 1
        execute("UPDATE rules SET enabled=?, updated_at=? WHERE id=?", (enabled, utc_now(), rule_id))
        try:
            apply_config(restart=True)
            flash("规则状态已切换。", "success")
        except RuntimeError as exc:
            flash(f"状态已保存，但应用配置失败：{exc}", "error")
        return redirect(url_for("dashboard"))

    @app.post("/rules/<rule_id>/delete")
    @require_login
    @require_csrf
    def delete_rule(rule_id: str) -> Response:
        user = require_current_user()
        rule = get_rule(rule_id)
        if not rule or not can_manage_rule(user, rule):
            flash("找不到这条规则，或无权操作。", "error")
            return redirect(url_for("dashboard"))
        execute("DELETE FROM rules WHERE id=?", (rule_id,))
        try:
            apply_config(restart=True)
            flash("规则已删除。", "success")
        except RuntimeError as exc:
            flash(f"规则已删除，但应用配置失败：{exc}", "error")
        return redirect(url_for("dashboard"))

    @app.post("/accounts")
    @require_admin
    @require_csrf
    def add_account() -> Response:
        try:
            data = parse_account_form(request.form, new=True)
            password = data.pop("password")
            user_id = uuid.uuid4().hex[:12]
            execute(
                """INSERT INTO users
                (id, username, password_hash, role, enabled, max_rules, rule_speed_limit_mbps,
                 rule_traffic_limit_gb, account_traffic_limit_gb, expires_at, note, created_at, updated_at)
                VALUES (?, ?, ?, 'user', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    data["username"],
                    generate_password_hash(password, method="pbkdf2:sha256", salt_length=16),
                    data["enabled"],
                    data["max_rules"],
                    data["rule_speed_limit_mbps"],
                    data["rule_traffic_limit_gb"],
                    data["account_traffic_limit_gb"],
                    data["expires_at"],
                    data["note"],
                    utc_now(),
                    utc_now(),
                ),
            )
            flash(f"子账号已创建：{data['username']}。请把密码单独发给用户。", "success")
        except (ValueError, sqlite3.IntegrityError) as exc:
            flash(f"创建失败：{exc}", "error")
        return redirect(url_for("dashboard"))

    @app.post("/accounts/<user_id>/update")
    @require_admin
    @require_csrf
    def update_account(user_id: str) -> Response:
        target = get_user(user_id)
        if not target or target["role"] == "admin":
            flash("找不到子账号，或不允许编辑主账号。", "error")
            return redirect(url_for("dashboard"))
        try:
            data = parse_account_form(request.form, new=False)
            execute(
                """UPDATE users SET enabled=?, max_rules=?, rule_speed_limit_mbps=?, rule_traffic_limit_gb=?,
                account_traffic_limit_gb=?, expires_at=?, note=?, updated_at=? WHERE id=?""",
                (
                    data["enabled"],
                    data["max_rules"],
                    data["rule_speed_limit_mbps"],
                    data["rule_traffic_limit_gb"],
                    data["account_traffic_limit_gb"],
                    data["expires_at"],
                    data["note"],
                    utc_now(),
                    user_id,
                ),
            )
            clamp_user_rule_limits(user_id)
            apply_config(restart=True)
            flash("子账号已更新。", "success")
        except (ValueError, RuntimeError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("dashboard"))

    @app.post("/accounts/<user_id>/reset-password")
    @require_admin
    @require_csrf
    def reset_account_password(user_id: str) -> Response:
        target = get_user(user_id)
        if not target or target["role"] == "admin":
            flash("找不到子账号，或不允许重置主账号。", "error")
            return redirect(url_for("dashboard"))
        password = generate_password()
        execute("UPDATE users SET password_hash=?, updated_at=? WHERE id=?", (generate_password_hash(password), utc_now(), user_id))
        flash(f"已重置 {target['username']} 的密码：{password}。请立即复制保存。", "success")
        return redirect(url_for("dashboard"))

    @app.post("/accounts/<user_id>/delete")
    @require_admin
    @require_csrf
    def delete_account(user_id: str) -> Response:
        target = get_user(user_id)
        if not target or target["role"] == "admin":
            flash("找不到子账号，或不允许删除主账号。", "error")
            return redirect(url_for("dashboard"))
        execute("DELETE FROM users WHERE id=?", (user_id,))
        execute("DELETE FROM rules WHERE owner_id=?", (user_id,))
        try:
            apply_config(restart=True)
        except RuntimeError as exc:
            flash(f"账号已删除，但应用配置失败：{exc}", "error")
        else:
            flash("子账号和其规则已删除。", "success")
        return redirect(url_for("dashboard"))

    @app.post("/settings")
    @require_admin
    @require_csrf
    def update_settings() -> Response:
        try:
            listen = request.form.get("listen", "").strip() or DEFAULT_LISTEN
            fallback = request.form.get("fallback_addr", "").strip() or DEFAULT_FALLBACK
            mode = request.form.get("forwarding_mode", "direct").strip().lower()
            if mode not in {"direct", "carpool"}:
                raise ValueError("运行模式不正确。")
            if not validate_listen(listen):
                raise ValueError("GOST 监听地址格式不正确，例如 :443。")
            if not validate_backend_addr(fallback):
                raise ValueError("默认后端格式不正确，例如 127.0.0.1:8053。")
            set_setting("listen", listen)
            set_setting("fallback_addr", fallback)
            set_setting("forwarding_mode", mode)
            apply_config(restart=True)
            flash("基础设置已保存，配置将在几秒内自动生效。", "success")
        except (ValueError, RuntimeError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("dashboard"))

    @app.post("/apply")
    @require_login
    @require_csrf
    def apply_now() -> Response:
        try:
            apply_config(restart=True)
            flash("配置已重新生成，GOST 将在几秒内自动重启。", "success")
        except RuntimeError as exc:
            flash(f"应用失败：{exc}", "error")
        return redirect(url_for("dashboard"))

    @app.get("/config")
    @require_admin
    def view_config() -> Response:
        return Response(generate_gost_config(), mimetype="text/plain; charset=utf-8")

    @app.get("/logs")
    @require_login
    def logs() -> str:
        gost_logs = run_cmd(["journalctl", "-u", GOST_SERVICE, "-n", "120", "--no-pager"], allow_fail=True)[1]
        relay_logs = run_cmd(["journalctl", "-u", RELAY_SERVICE, "-n", "120", "--no-pager"], allow_fail=True)[1]
        caddy_logs = run_cmd(["journalctl", "-u", CADDY_SERVICE, "-n", "60", "--no-pager"], allow_fail=True)[1]
        return render_template("logs.html", gost_logs=gost_logs, relay_logs=relay_logs, caddy_logs=caddy_logs)

    return app


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GOST_CONFIG.parent.mkdir(parents=True, exist_ok=True)


def get_db() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin','user')),
                enabled INTEGER NOT NULL DEFAULT 1,
                max_rules INTEGER NOT NULL DEFAULT 3,
                rule_speed_limit_mbps REAL,
                rule_traffic_limit_gb REAL,
                account_traffic_limit_gb REAL,
                expires_at TEXT,
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rules (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                sni TEXT NOT NULL UNIQUE,
                target TEXT NOT NULL,
                port INTEGER NOT NULL,
                note TEXT DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                relay_port INTEGER NOT NULL UNIQUE,
                speed_limit_mbps REAL,
                traffic_limit_gb REAL,
                bytes_up INTEGER NOT NULL DEFAULT 0,
                bytes_down INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        migrate_schema(db)
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('listen',?)", (DEFAULT_LISTEN,))
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('fallback_addr',?)", (DEFAULT_FALLBACK,))
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('panel_domain',?)", (PANEL_DOMAIN,))
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('forwarding_mode', 'direct')")
        db.commit()


def migrate_schema(db: sqlite3.Connection) -> None:
    """Add columns introduced after the first release, keeping old databases usable."""
    rule_columns = {row["name"] for row in db.execute("PRAGMA table_info(rules)").fetchall()}
    if "listen_port" not in rule_columns:
        db.execute("ALTER TABLE rules ADD COLUMN listen_port INTEGER NOT NULL DEFAULT 443")
        db.commit()
    db.execute(
        "UPDATE rules SET listen_port=? WHERE listen_port IS NULL OR listen_port<=0",
        (listen_port_of(get_setting("listen", DEFAULT_LISTEN)) or 443,),
    )
    db.commit()


def default_listen_port() -> int:
    return listen_port_of(get_setting("listen", DEFAULT_LISTEN)) or 443


def listen_port_of(addr: str) -> Optional[int]:
    """Extract the port from a GOST listen address such as ':443' or '1.2.3.4:8443'."""
    addr = (addr or "").strip()
    if not addr:
        return None
    try:
        port = int(addr.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        return None
    return port if 1 <= port <= 65535 else None


def validate_listen_port(port: int) -> None:
    """Reject inbound ports that would fight with the panel, Caddy or the relay range."""
    if not (1 <= port <= 65535):
        raise ValueError("入站端口范围必须是 1-65535。")
    if port == PANEL_PORT:
        raise ValueError(f"入站端口 {port} 是面板自身端口，请换一个。")
    if port == CADDY_HTTPS_PORT:
        raise ValueError(f"入站端口 {port} 是 Caddy fallback 端口，请换一个。")
    if RELAY_PORT_MIN <= port <= RELAY_PORT_MAX:
        raise ValueError(f"入站端口不能落在本地中继保留段 {RELAY_PORT_MIN}-{RELAY_PORT_MAX} 内。")


def port_in_use(port: int) -> bool:
    """True when any TCP listener already holds the port.

    Checking with `ss` rather than a probe bind avoids briefly occupying the
    port and avoids reporting a false negative on servers where SO_REUSEADDR
    lets a second bind succeed.
    """
    if shutil.which("ss"):
        code, out = run_cmd(["ss", "-ltnH", f"sport = :{port}"], allow_fail=True)
        if code == 0:
            return any(line.split()[0].startswith("LISTEN") for line in out.splitlines() if line.strip())
    code, out = run_cmd(["bash", "-lc", f"ss -ltn 2>/dev/null | grep -E ':{port}\\b' || true"], allow_fail=True)
    if code == 0 and out.strip():
        return True
    return False


def validate_listen_port_available(port: int, keep: Optional[int] = None) -> None:
    """Reject a new inbound port that another process already occupies.

    GOST binds these ports itself, so a port held by anything else (another
    GOST listener, a service, a stray process) can never serve the rule.
    """
    if keep is not None and port == keep:
        return
    if not port_in_use(port):
        return
    if port == default_listen_port():
        return
    if any(int(r.get("listen_port") or 0) == port for r in query_all("SELECT listen_port FROM rules")):
        return
    raise ValueError(f"入站端口 {port} 已被其他程序占用，请换一个。")


def row_to_dict(row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def query_one(sql: str, params: Tuple[Any, ...] = ()) -> Optional[Dict[str, Any]]:
    with get_db() as db:
        return row_to_dict(db.execute(sql, params).fetchone())


def query_all(sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    with get_db() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def execute(sql: str, params: Tuple[Any, ...] = ()) -> None:
    with get_db() as db:
        db.execute(sql, params)
        db.commit()


def executemany(sql: str, params: Iterable[Tuple[Any, ...]]) -> None:
    with get_db() as db:
        db.executemany(sql, params)
        db.commit()


def read_or_create_secret() -> str:
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text(encoding="utf-8").strip()
    secret = secrets.token_urlsafe(48)
    atomic_write_text(SECRET_FILE, secret + "\n", mode=0o600)
    return secret


def atomic_write_text(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_dt(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def fmt_dt(value: str | None) -> str:
    dt = parse_dt(value)
    if not dt:
        return "永久"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def fmt_bytes(n: int | float | None) -> str:
    n = float(n or 0)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    for unit in units:
        if n < 1024 or unit == units[-1]:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_gb(value: Any) -> str:
    if value is None or value == "" or float(value or 0) <= 0:
        return "不限"
    return f"{float(value):g} GB"


def fmt_limit(value: Any, suffix: str) -> str:
    if value is None or value == "" or float(value or 0) <= 0:
        return "不限"
    return f"{float(value):g} {suffix}"


def gb_to_bytes(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    v = float(value)
    if v <= 0:
        return None
    return int(v * 1024 * 1024 * 1024)


def mbps_to_bps(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    v = float(value)
    if v <= 0:
        return None
    return int(v * 1024 * 1024 / 8)


def get_current_user() -> Optional[Dict[str, Any]]:
    uid = session.get("uid")
    if not uid:
        return None
    user = get_user(uid)
    if not user or not user_is_active(user):
        session.clear()
        return None
    return user


def require_current_user() -> Dict[str, Any]:
    user = get_current_user()
    if not user:
        raise RuntimeError("not logged in")
    return user


def is_logged_in() -> bool:
    return get_current_user() is not None


def require_login(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for("login_page"))
        return func(*args, **kwargs)
    return wrapper


def require_admin(func):
    @functools.wraps(func)
    @require_login
    def wrapper(*args, **kwargs):
        user = require_current_user()
        if user.get("role") != "admin":
            flash("只有主账号可以操作此功能。", "error")
            return redirect(url_for("dashboard"))
        return func(*args, **kwargs)
    return wrapper


def require_csrf(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        token = request.form.get("csrf_token", "")
        if not token or token != session.get("csrf"):
            flash("页面已过期，请刷新后重试。", "error")
            return redirect(url_for("dashboard"))
        return func(*args, **kwargs)
    return wrapper


def get_csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def get_user(uid: str) -> Optional[Dict[str, Any]]:
    return query_one("SELECT * FROM users WHERE id=?", (uid,))


def find_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    return query_one("SELECT * FROM users WHERE username=?", (username,))


def list_users() -> List[Dict[str, Any]]:
    users = query_all("SELECT * FROM users ORDER BY role, created_at DESC")
    for user in users:
        user.update(account_usage(user))
        user["active"] = user_is_active(user)
        user["rule_count"] = query_one("SELECT COUNT(*) AS c FROM rules WHERE owner_id=?", (user["id"],))["c"]
    return users


def user_is_active(user: Dict[str, Any]) -> bool:
    if int(user.get("enabled", 1)) != 1:
        return False
    exp = parse_dt(user.get("expires_at"))
    if exp and exp <= datetime.now(timezone.utc):
        return False
    return True


def account_usage(user: Dict[str, Any]) -> Dict[str, Any]:
    row = query_one("SELECT COALESCE(SUM(bytes_up + bytes_down),0) AS used FROM rules WHERE owner_id=?", (user["id"],))
    used = int(row.get("used", 0) if row else 0)
    limit_b = gb_to_bytes(user.get("account_traffic_limit_gb"))
    remaining = None if limit_b is None else max(0, limit_b - used)
    return {"used_bytes": used, "limit_bytes": limit_b, "remaining_bytes": remaining, "traffic_percent": percent(used, limit_b)}


def percent(used: int, limit: Optional[int]) -> int:
    if not limit or limit <= 0:
        return 0
    return max(0, min(100, round(used * 100 / limit)))


def get_rule(rule_id: str) -> Optional[Dict[str, Any]]:
    return query_one("SELECT r.*, u.username owner_username, u.role owner_role FROM rules r JOIN users u ON u.id=r.owner_id WHERE r.id=?", (rule_id,))


def list_rules_for_user(user: Dict[str, Any]) -> List[Dict[str, Any]]:
    if user["role"] == "admin":
        rules = query_all("SELECT r.*, u.username owner_username, u.enabled owner_enabled, u.expires_at owner_expires_at, u.account_traffic_limit_gb FROM rules r JOIN users u ON u.id=r.owner_id ORDER BY r.created_at DESC")
    else:
        rules = query_all("SELECT r.*, u.username owner_username, u.enabled owner_enabled, u.expires_at owner_expires_at, u.account_traffic_limit_gb FROM rules r JOIN users u ON u.id=r.owner_id WHERE r.owner_id=? ORDER BY r.created_at DESC", (user["id"],))
    usage_by_owner: Dict[str, Dict[str, Any]] = {}
    for rule in rules:
        owner = get_user(rule["owner_id"])
        if owner and owner["id"] not in usage_by_owner:
            usage_by_owner[owner["id"]] = account_usage(owner)
        rule["used_bytes"] = int(rule.get("bytes_up", 0)) + int(rule.get("bytes_down", 0))
        rule["limit_bytes"] = gb_to_bytes(rule.get("traffic_limit_gb"))
        rule["traffic_percent"] = percent(rule["used_bytes"], rule["limit_bytes"])
        rule["account_usage"] = usage_by_owner.get(rule["owner_id"], {})
        rule["active"] = rule_is_active(rule, owner)
    return rules


def can_manage_rule(user: Dict[str, Any], rule: Dict[str, Any]) -> bool:
    return user["role"] == "admin" or rule.get("owner_id") == user.get("id")


def enforce_rule_count(owner: Dict[str, Any]) -> None:
    if owner["role"] == "admin":
        return
    max_rules = int(owner.get("max_rules") or 0)
    count = query_one("SELECT COUNT(*) AS c FROM rules WHERE owner_id=?", (owner["id"],))["c"]
    if max_rules >= 0 and int(count) >= max_rules:
        raise ValueError(f"该账号最多只能添加 {max_rules} 条转发规则。")


def parse_float_or_none(raw: str, field: str) -> Optional[float]:
    raw = (raw or "").strip()
    if raw == "":
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{field} 必须是数字。") from exc
    if value < 0:
        raise ValueError(f"{field} 不能小于 0。")
    if value == 0:
        return None
    return value


def parse_rule_form(form, owner: Dict[str, Any], actor: Dict[str, Any], existing_rule: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    sni = form.get("sni", "").strip().lower()
    target = form.get("target", "").strip()
    port_str = form.get("port", "443").strip()
    note = form.get("note", "").strip()[:120]
    enabled = 1 if (form.get("enabled") == "on" or form.get("enabled") == "true" or form.get("enabled") == "1") else 0

    if not target.startswith("[") and target.count(":") == 1:
        host_part, port_part = target.rsplit(":", 1)
        if port_part.isdigit():
            target = host_part.strip()
            port_str = port_part.strip()

    if not validate_domain(sni):
        raise ValueError("SNI / serverName 格式不正确，例如 visa.cn。")
    panel_domain = get_setting("panel_domain", PANEL_DOMAIN)
    if panel_domain and sni == panel_domain and actor.get("role") != "admin":
        raise ValueError("该域名是面板域名，不能作为子账号转发规则。")
    existing = query_one("SELECT id FROM rules WHERE sni=?", (sni,))
    if existing and (not existing_rule or existing["id"] != existing_rule["id"]):
        raise ValueError("这个 SNI 已经存在，不能重复添加。")
    if not validate_host(target):
        raise ValueError("目标域名或 IP 格式不正确，例如 a.example.com 或 127.0.0.1。")
    try:
        port = int(port_str)
    except ValueError as exc:
        raise ValueError("目标端口必须是数字。") from exc
    if not (1 <= port <= 65535):
        raise ValueError("目标端口范围必须是 1-65535。")

    listen_port_raw = (form.get("listen_port") or "").strip()
    if listen_port_raw == "":
        listen_port = default_listen_port()
    else:
        try:
            listen_port = int(listen_port_raw)
        except ValueError as exc:
            raise ValueError("入站端口必须是数字。") from exc
    validate_listen_port(listen_port)
    keep_port = None
    if existing_rule and existing_rule.get("listen_port"):
        keep_port = int(existing_rule["listen_port"])
    validate_listen_port_available(listen_port, keep=keep_port)

    if actor["role"] == "admin":
        speed = parse_float_or_none(form.get("speed_limit_mbps", ""), "限速")
        traffic = parse_float_or_none(form.get("traffic_limit_gb", ""), "规则流量")
        if owner["role"] != "admin":
            speed = cap_limit(speed, owner.get("rule_speed_limit_mbps"))
            traffic = cap_limit(traffic, owner.get("rule_traffic_limit_gb"))
    else:
        speed = none_if_zero(owner.get("rule_speed_limit_mbps"))
        traffic = none_if_zero(owner.get("rule_traffic_limit_gb"))

    return {
        "sni": sni,
        "target": target,
        "port": port,
        "listen_port": listen_port,
        "note": note,
        "enabled": enabled,
        "speed_limit_mbps": speed,
        "traffic_limit_gb": traffic,
    }


def cap_limit(value: Optional[float], cap: Any) -> Optional[float]:
    cap_v = none_if_zero(cap)
    if cap_v is None:
        return value
    if value is None:
        return cap_v
    return min(float(value), float(cap_v))


def none_if_zero(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except Exception:
        return None
    return v if v > 0 else None


def insert_rule(rule: Dict[str, Any]) -> None:
    execute(
        """INSERT INTO rules
        (id, owner_id, sni, target, port, listen_port, note, enabled, relay_port, speed_limit_mbps, traffic_limit_gb, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            rule["id"],
            rule["owner_id"],
            rule["sni"],
            rule["target"],
            rule["port"],
            int(rule.get("listen_port") or default_listen_port()),
            rule["note"],
            rule["enabled"],
            rule["relay_port"],
            rule["speed_limit_mbps"],
            rule["traffic_limit_gb"],
            utc_now(),
            utc_now(),
        ),
    )


def update_rule_row(rule_id: str, rule: Dict[str, Any]) -> None:
    execute(
        """UPDATE rules SET sni=?, target=?, port=?, listen_port=?, note=?, enabled=?, speed_limit_mbps=?, traffic_limit_gb=?, updated_at=? WHERE id=?""",
        (
            rule["sni"],
            rule["target"],
            rule["port"],
            int(rule.get("listen_port") or default_listen_port()),
            rule["note"],
            rule["enabled"],
            rule["speed_limit_mbps"],
            rule["traffic_limit_gb"],
            utc_now(),
            rule_id,
        ),
    )


def parse_account_form(form, new: bool) -> Dict[str, Any]:
    username = form.get("username", "").strip()
    password = form.get("password", "")
    enabled = 1 if form.get("enabled") == "on" or form.get("enabled") == "true" or form.get("enabled") == "1" else 0
    note = form.get("note", "").strip()[:120]
    if new:
        if not re.match(r"^[A-Za-z0-9_.-]{3,32}$", username):
            raise ValueError("用户名只能包含字母、数字、下划线、点和横线，长度 3-32。")
        if len(password) < 10:
            raise ValueError("子账号密码至少 10 位。")
    try:
        max_rules = int(form.get("max_rules", "3"))
    except ValueError as exc:
        raise ValueError("规则数量上限必须是整数。") from exc
    if not (0 <= max_rules <= 500):
        raise ValueError("规则数量上限范围为 0-500。")
    speed = parse_float_or_none(form.get("rule_speed_limit_mbps", ""), "每条规则限速")
    rule_traffic = parse_float_or_none(form.get("rule_traffic_limit_gb", ""), "每条规则流量")
    account_traffic = parse_float_or_none(form.get("account_traffic_limit_gb", ""), "账号总流量")
    expires_at = form.get("expires_at", "").strip()
    if expires_at:
        if "T" not in expires_at and " " in expires_at:
            expires_at = expires_at.replace(" ", "T", 1)
        if parse_dt(expires_at) is None:
            raise ValueError("到期时间格式不正确。")
    else:
        expires_at = None
    data = {
        "username": username,
        "password": password,
        "enabled": enabled,
        "max_rules": max_rules,
        "rule_speed_limit_mbps": speed,
        "rule_traffic_limit_gb": rule_traffic,
        "account_traffic_limit_gb": account_traffic,
        "expires_at": expires_at,
        "note": note,
    }
    return data


def clamp_user_rule_limits(user_id: str) -> None:
    user = get_user(user_id)
    if not user:
        return
    speed_cap = none_if_zero(user.get("rule_speed_limit_mbps"))
    traffic_cap = none_if_zero(user.get("rule_traffic_limit_gb"))
    rows = query_all("SELECT id, speed_limit_mbps, traffic_limit_gb FROM rules WHERE owner_id=?", (user_id,))
    for r in rows:
        speed = cap_limit(none_if_zero(r.get("speed_limit_mbps")), speed_cap)
        traffic = cap_limit(none_if_zero(r.get("traffic_limit_gb")), traffic_cap)
        execute("UPDATE rules SET speed_limit_mbps=?, traffic_limit_gb=?, updated_at=? WHERE id=?", (speed, traffic, utc_now(), r["id"]))


def validate_domain(domain: str) -> bool:
    if "`" in domain or "\n" in domain or "\r" in domain:
        return False
    return bool(DOMAIN_RE.match(domain))


def validate_host(host: str) -> bool:
    if not host or "`" in host or "\n" in host or "\r" in host or "/" in host:
        return False
    return bool(HOST_RE.match(host)) and len(host) <= 253


def validate_backend_addr(addr: str) -> bool:
    if not addr or "`" in addr or "\n" in addr or "\r" in addr or "/" in addr:
        return False
    if addr.startswith("["):
        m = re.match(r"^\[[0-9A-Fa-f:.]+\]:(\d{1,5})$", addr)
        return bool(m and 1 <= int(m.group(1)) <= 65535)
    if ":" not in addr:
        return False
    host, port_s = addr.rsplit(":", 1)
    if not validate_host(host):
        return False
    try:
        port = int(port_s)
    except ValueError:
        return False
    return 1 <= port <= 65535


def validate_listen(listen: str) -> bool:
    if not LISTEN_RE.match(listen):
        return False
    try:
        port = int(listen.rsplit(":", 1)[1])
    except ValueError:
        return False
    return 1 <= port <= 65535


def allocate_relay_port() -> int:
    used = {int(r["relay_port"]) for r in query_all("SELECT relay_port FROM rules")}
    for _ in range(300):
        port = random.randint(RELAY_PORT_MIN, RELAY_PORT_MAX)
        if port not in used:
            return port
    for port in range(RELAY_PORT_MIN, RELAY_PORT_MAX + 1):
        if port not in used:
            return port
    raise RuntimeError("没有可用的本地中继端口。")


def sanitize_node_name(sni: str, fallback: bool = False) -> str:
    if fallback:
        return "caddy-fallback"
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", sni).strip("-")[:32]
    suffix = hashlib.sha1(sni.encode("utf-8")).hexdigest()[:8]
    return f"sni-{cleaned}-{suffix}"


def get_setting(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def load_settings() -> Dict[str, str]:
    mode = get_setting("forwarding_mode", "direct").strip().lower()
    if mode not in {"direct", "carpool"}:
        mode = "direct"
    return {
        "listen": get_setting("listen", DEFAULT_LISTEN),
        "fallback_addr": get_setting("fallback_addr", DEFAULT_FALLBACK),
        "panel_domain": get_setting("panel_domain", PANEL_DOMAIN),
        "forwarding_mode": mode,
    }


def forwarding_mode() -> str:
    return load_settings().get("forwarding_mode", "direct")


def is_carpool_mode() -> bool:
    return forwarding_mode() == "carpool"


def rule_is_active(rule: Dict[str, Any], owner: Optional[Dict[str, Any]] = None) -> bool:
    if int(rule.get("enabled", 1)) != 1:
        return False
    owner = owner or get_user(rule.get("owner_id", ""))
    if not owner or not user_is_active(owner):
        return False
    if not is_carpool_mode():
        return True
    rule_limit = gb_to_bytes(rule.get("traffic_limit_gb"))
    used = int(rule.get("bytes_up", 0)) + int(rule.get("bytes_down", 0))
    if rule_limit is not None and used >= rule_limit:
        return False
    acc = account_usage(owner)
    if acc.get("limit_bytes") is not None and int(acc.get("used_bytes") or 0) >= int(acc.get("limit_bytes") or 0):
        return False
    return True


def active_rules_for_config() -> List[Dict[str, Any]]:
    rows = query_all("SELECT r.*, u.enabled owner_enabled, u.expires_at owner_expires_at FROM rules r JOIN users u ON u.id=r.owner_id ORDER BY r.created_at")
    active = []
    for r in rows:
        owner = get_user(r["owner_id"])
        if rule_is_active(r, owner):
            active.append(r)
    return active


def group_rules_by_listen_port(rules: List[Dict[str, Any]], default_port: int) -> Dict[int, List[Dict[str, Any]]]:
    groups: Dict[int, List[Dict[str, Any]]] = {default_port: []}
    for rule in rules:
        try:
            port = int(rule.get("listen_port") or default_port)
        except (TypeError, ValueError):
            port = default_port
        groups.setdefault(port, []).append(rule)
    return groups


def generate_gost_config() -> str:
    settings = load_settings()
    listen = settings.get("listen") or DEFAULT_LISTEN
    fallback = settings.get("fallback_addr") or DEFAULT_FALLBACK
    if not validate_listen(listen):
        raise RuntimeError(f"监听地址无效：{listen}")
    if not validate_backend_addr(fallback):
        raise RuntimeError(f"默认后端无效：{fallback}")
    default_port = listen_port_of(listen) or 443
    mode = settings.get("forwarding_mode", "direct")
    groups = group_rules_by_listen_port(active_rules_for_config(), default_port)

    lines = ["services:"]
    # Default port first, then the extra inbound ports in ascending order.
    for port in sorted(groups, key=lambda p: (p != default_port, p)):
        rules = groups[port]
        is_default = port == default_port
        addr = listen if is_default else f":{port}"
        # A lone rule on a dedicated port is a plain TCP port forward; several
        # rules sharing one port need SNI dispatch to tell them apart.
        plain_forward = (not is_default) and len(rules) == 1
        lines.extend([
            f"- name: reality-{port}",
            f'  addr: "{addr}"',
            "  handler:",
            "    type: tcp",
        ])
        if not plain_forward:
            lines.extend(["    metadata:", "      sniffing: true"])
        lines.extend(["  listener:", "    type: tcp", "  forwarder:", "    nodes:"])
        for rule in rules:
            sni = str(rule["sni"]).lower()
            if mode == "carpool":
                backend = f"{RELAY_HOST}:{int(rule['relay_port'])}"
            else:
                backend = f"{rule['target']}:{int(rule['port'])}"
            lines.extend([f"    - name: {sanitize_node_name(sni)}", f"      addr: {backend}"])
            if not plain_forward:
                lines.extend(["      matcher:", f"        rule: Host(`{sni}`)"])
        if is_default:
            lines.extend([
                f"    - name: {sanitize_node_name('', fallback=True)}",
                f"      addr: {fallback}",
            ])
        lines.append("")
    return "\n".join(lines)


_APPLY_LOCK = threading.Lock()
_APPLY_TIMER: Optional[threading.Timer] = None
_PREVIOUS_CONFIG = ""
APPLY_STATE: Dict[str, Any] = {"pending": False, "last_at": None, "error": None}


def write_gost_config() -> str:
    """Render the config and validate it before it can reach the live service."""
    yaml_text = generate_gost_config()
    validate_gost_config(yaml_text)
    atomic_write_text(GOST_CONFIG, yaml_text, mode=0o644)
    return yaml_text


def validate_gost_config(yaml_text: str) -> None:
    """Structural check of the rendered config before it reaches the live service.

    GOST has no dry-run flag (`-D` is debug mode and binds ports), and a reload
    with an empty or truncated `services:` list silently drops every listener.
    These checks catch that class of mistake; the real safety net is the
    post-reload listener verification in `_apply_worker`.
    """
    lines = yaml_text.splitlines()
    if not lines or lines[0].strip() != "services:":
        raise RuntimeError("生成的 GOST 配置无效：缺少 services 段。")
    service_names = [l for l in lines if l.startswith("- name: ")]
    if not service_names:
        raise RuntimeError("生成的 GOST 配置无效：没有任何转发服务。")
    for required, label in (("  addr: ", "监听地址"), ("  handler:", "handler"), ("  forwarder:", "forwarder")):
        if sum(1 for l in lines if l.startswith(required)) < len(service_names):
            raise RuntimeError(f"生成的 GOST 配置无效：有服务缺少{label}。")


def schedule_apply(delay: Optional[float] = None) -> None:
    """Apply the GOST config *after* the HTTP response has been flushed.

    The reload happens in a background thread so the redirect reaches the
    browser first, and debouncing collapses a burst of edits into one reload.
    GOST's SIGHUP reload keeps established sessions alive, so the panel's own
    request path through GOST is not interrupted.
    """
    global _APPLY_TIMER
    delay = APPLY_DELAY if delay is None else delay
    with _APPLY_LOCK:
        if _APPLY_TIMER is not None:
            _APPLY_TIMER.cancel()
        timer = threading.Timer(max(float(delay), 0.5), _apply_worker)
        timer.daemon = True
        _APPLY_TIMER = timer
        APPLY_STATE["pending"] = True
        timer.start()


def _apply_worker() -> None:
    global _APPLY_TIMER
    previous = _PREVIOUS_CONFIG
    with _APPLY_LOCK:
        _APPLY_TIMER = None
        APPLY_STATE["pending"] = False
    try:
        reload_gost_service()
        time.sleep(float(os.environ.get("GOST_APPLY_VERIFY_DELAY", "2")))
        verify_gost_running()
        sync_relay_service()
        APPLY_STATE["error"] = None
    except Exception as exc:  # background thread: record instead of raising
        APPLY_STATE["error"] = str(exc)
        if previous:
            try:
                atomic_write_text(GOST_CONFIG, previous, mode=0o644)
                reload_gost_service()
                APPLY_STATE["error"] = f"{exc}（已回滚到上一份可用配置）"
            except Exception as rollback_exc:
                APPLY_STATE["error"] = f"{exc}；回滚也失败：{rollback_exc}"
    APPLY_STATE["last_at"] = utc_now()


def verify_gost_running() -> None:
    """Confirm GOST survived the reload and still listens on every expected port.

    A bad reload (empty `services:`, unknown listener type) leaves the process
    alive but the listeners closed, so `is-active` alone is not enough: a new
    client would get connection refused while the panel looks healthy.
    """
    code, out = run_cmd(["systemctl", "is-active", GOST_SERVICE], allow_fail=True)
    if code != 0 or out.strip() != "active":
        raise RuntimeError(out.strip() or "GOST 重启后未处于运行状态")
    expected = expected_listen_ports()
    missing = [port for port in expected if not port_in_use(port)]
    if missing:
        ports = "、".join(str(p) for p in missing)
        raise RuntimeError(f"GOST 没有监听预期端口 {ports}，配置可能已被拒绝")


def expected_listen_ports() -> List[int]:
    """Ports the freshly written config is supposed to bind."""
    ports: List[int] = []
    groups = group_rules_by_listen_port(active_rules_for_config(), default_listen_port())
    for port in groups:
        ports.append(int(port))
    return sorted(set(ports))


def reload_gost_service() -> None:
    """Reload GOST in place so in-flight connections (including this panel) survive.

    `systemctl restart` tears down every proxied connection, which is why a
    submission made through the panel used to end in a browser connection
    error instead of the redirect. GOST supports SIGHUP config reload while
    keeping listeners and established sessions alive, so prefer that and only
    fall back to a full restart when the process is not running.
    """
    code, out = run_cmd(["systemctl", "is-active", GOST_SERVICE], allow_fail=True)
    if code != 0 or out.strip() != "active":
        code, out = run_cmd(["systemctl", "restart", "--no-block", GOST_SERVICE], allow_fail=True)
        if code != 0:
            raise RuntimeError(out.strip() or f"systemctl restart {GOST_SERVICE} failed")
        return
    code, out = run_cmd(["systemctl", "reload", "--no-block", GOST_SERVICE], allow_fail=True)
    if code == 0:
        return
    # Older unit files have no ExecReload; a direct SIGHUP still performs the
    # same in-place reload without dropping established connections.
    code, out = run_cmd(
        ["systemctl", "kill", "--signal=SIGHUP", "--kill-whom=main", GOST_SERVICE],
        allow_fail=True,
    )
    if code == 0:
        return
    code, out = run_cmd(["systemctl", "restart", "--no-block", GOST_SERVICE], allow_fail=True)
    if code != 0:
        raise RuntimeError(out.strip() or f"systemctl reload/restart {GOST_SERVICE} failed")


def apply_config(restart: bool) -> None:
    global _PREVIOUS_CONFIG
    previous = ""
    try:
        previous = GOST_CONFIG.read_text(encoding="utf-8")
    except OSError:
        previous = ""
    write_gost_config()
    if restart:
        _PREVIOUS_CONFIG = previous
        schedule_apply()


def sync_relay_service() -> None:
    if is_carpool_mode():
        code, out = run_cmd(["systemctl", "is-active", RELAY_SERVICE], allow_fail=True)
        if code != 0 or out.strip() != "active":
            run_cmd(["systemctl", "enable", "--now", RELAY_SERVICE], allow_fail=True)
            run_cmd(["systemctl", "restart", RELAY_SERVICE], allow_fail=True)
    else:
        run_cmd(["systemctl", "disable", "--now", RELAY_SERVICE], allow_fail=True)


def dashboard_stats(user: Dict[str, Any]) -> Dict[str, Any]:
    if user["role"] == "admin":
        total_rules = query_one("SELECT COUNT(*) c FROM rules")["c"]
        active_rules = len(active_rules_for_config())
        total_users = query_one("SELECT COUNT(*) c FROM users WHERE role='user'")["c"]
        used = query_one("SELECT COALESCE(SUM(bytes_up + bytes_down),0) used FROM rules")["used"]
        return {"total_rules": total_rules, "active_rules": active_rules, "total_users": total_users, "used_bytes": int(used or 0)}
    usage = account_usage(user)
    total_rules = query_one("SELECT COUNT(*) c FROM rules WHERE owner_id=?", (user["id"],))["c"]
    active_rules = len([r for r in list_rules_for_user(user) if r.get("active")])
    return {"total_rules": total_rules, "active_rules": active_rules, "total_users": 0, "used_bytes": usage["used_bytes"], **usage}


def collect_status() -> Dict[str, str]:
    status: Dict[str, str] = {"forwarding_mode": forwarding_mode()}
    for svc in (GOST_SERVICE, RELAY_SERVICE, CADDY_SERVICE):
        code, out = run_cmd(["systemctl", "is-active", svc], allow_fail=True)
        status[svc] = out.strip() if out.strip() else ("active" if code == 0 else "unknown")
    gost_bin = shutil.which("gost") or "/usr/local/bin/gost"
    if os.path.exists(gost_bin):
        status["gost_version"] = run_cmd([gost_bin, "-V"], allow_fail=True)[1].strip()
    else:
        status["gost_version"] = ""
    listen_dump = run_cmd(["bash", "-lc", "ss -tulnp 2>/dev/null | grep -E 'gost' || true"], allow_fail=True)[1].strip()
    status["listen_ports"] = listen_dump
    return status


def run_cmd(cmd: List[str], allow_fail: bool = False) -> Tuple[int, str]:
    try:
        completed = subprocess.run(cmd, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
        if completed.returncode != 0 and not allow_fail:
            raise RuntimeError(completed.stdout)
        return completed.returncode, completed.stdout
    except Exception as exc:
        if allow_fail:
            return 1, str(exc)
        raise


def generate_password() -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@#%+=_.:-"
    return "".join(secrets.choice(alphabet) for _ in range(28))


def parse_init_rules(raw: str, owner_id: str) -> List[Dict[str, Any]]:
    rules: List[Dict[str, Any]] = []
    if not raw.strip():
        return rules
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item or ":" not in item:
            raise ValueError("INIT_RULES 格式应为 sni=target:port,sni2=target2:port")
        sni, backend = item.split("=", 1)
        listen_port = None
        if "@" in backend:
            backend, listen_s = backend.rsplit("@", 1)
            if not listen_s.strip().isdigit():
                raise ValueError(f"初始化规则的入站端口无效：{item}")
            listen_port = int(listen_s.strip())
            validate_listen_port(listen_port)
        target, port_s = backend.rsplit(":", 1)
        port = int(port_s)
        sni = sni.strip().lower()
        target = target.strip()
        if not validate_domain(sni) or not validate_host(target) or not (1 <= port <= 65535):
            raise ValueError(f"初始化规则无效：{item}")
        rules.append({
            "id": uuid.uuid4().hex[:12],
            "owner_id": owner_id,
            "sni": sni,
            "target": target,
            "port": port,
            "listen_port": listen_port or default_listen_port(),
            "note": "初始化规则",
            "enabled": 1,
            "relay_port": allocate_relay_port(),
            "speed_limit_mbps": None,
            "traffic_limit_gb": None,
        })
    return rules


def cli() -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run web panel")
    serve.add_argument("--host", default=os.environ.get("GOST_PANEL_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("GOST_PANEL_PORT", "7080")))

    init = sub.add_parser("init", help="initialize admin account and state")
    init.add_argument("--username", required=True)
    init.add_argument("--password", required=True)
    init.add_argument("--listen", default=DEFAULT_LISTEN)
    init.add_argument("--fallback", default=DEFAULT_FALLBACK)
    init.add_argument("--panel-domain", default=PANEL_DOMAIN)
    init.add_argument("--init-rules", default=os.environ.get("INIT_RULES", ""))
    init.add_argument("--mode", default=os.environ.get("GOST_FORWARD_MODE", "direct"), choices=["direct", "carpool"])
    init.add_argument("--force-auth", action="store_true")

    sub.add_parser("render-config", help="write gost config from database")
    sub.add_parser("show-config", help="print gost config")

    args = parser.parse_args()
    init_db()
    if args.command == "serve":
        app = create_app()
        app.run(host=args.host, port=args.port)
        return 0
    if args.command == "init":
        read_or_create_secret()
        admin = find_user_by_username(args.username)
        if args.force_auth or not admin:
            if admin:
                execute("UPDATE users SET password_hash=?, role='admin', enabled=1, updated_at=? WHERE id=?", (generate_password_hash(args.password), utc_now(), admin["id"]))
                admin_id = admin["id"]
            else:
                admin_id = uuid.uuid4().hex[:12]
                execute(
                    """INSERT INTO users
                    (id, username, password_hash, role, enabled, max_rules, created_at, updated_at)
                    VALUES (?, ?, ?, 'admin', 1, 999, ?, ?)""",
                    (admin_id, args.username, generate_password_hash(args.password), utc_now(), utc_now()),
                )
        else:
            admin_id = admin["id"]
        set_setting("listen", args.listen)
        set_setting("fallback_addr", args.fallback)
        set_setting("panel_domain", (args.panel_domain or "").strip().lower())
        set_setting("forwarding_mode", args.mode)
        if query_one("SELECT COUNT(*) c FROM rules")["c"] == 0:
            for rule in parse_init_rules(args.init_rules, admin_id):
                insert_rule(rule)
        apply_config(restart=False)
        return 0
    if args.command == "render-config":
        apply_config(restart=False)
        return 0
    if args.command == "show-config":
        print(generate_gost_config())
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(cli())
