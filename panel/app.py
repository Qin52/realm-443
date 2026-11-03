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
import html
import ipaddress
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
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import ssl
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

try:
    # 出口访问守卫 egress.py 与面板同目录；缺少该模块时降级，
    # 用 EGRESS_IMPORT_ERROR 记录原因，保证面板仍然能启动。
    import egress
except ImportError as exc:
    egress = None  # type: ignore[assignment]
    EGRESS_IMPORT_ERROR = str(exc)
else:
    EGRESS_IMPORT_ERROR = ""

APP_NAME = "GOST SNI Manager"
APP_VERSION = "2.3.0"
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
CERT_DIR = Path(os.environ.get("GOST_CERT_DIR", "/root/cert"))

# 面板开关与来源限制（契约 v3 第 2/3/4 节）。
PANEL_CONF = Path(os.environ.get("GOST_PANEL_CONF", str(DATA_DIR / "panel.conf")))
PANEL_SERVICE = os.environ.get("GSM_PANEL_SERVICE", "gost-panel")
PANEL_DEFAULT_BIND = os.environ.get("GOST_PANEL_BIND", "127.0.0.1")
PANEL_WILDCARD_BIND = "0.0.0.0"
PANEL_REMOTE_DENIED_TEXT = "面板仅允许通过 SSH 隧道访问"
PANEL_DISABLED_TEXT = "面板已关闭"
# 本机网段：SSH 端口转发（含 -L 隧道、VNC 隧道）到达面板时 remote_addr 就是本机。
LOCAL_TRUSTED_NETS = tuple(
    ipaddress.ip_network(item)
    for item in ("127.0.0.0/8", "::1/128")
)
_PANEL_ENABLED_LOCK = threading.Lock()

# 出口访问控制（契约第 6/7/8 节）：面板侧提示与日志页上限。
EGRESS_UNAVAILABLE_TEXT = "本机未启用出口访问守卫（未安装 gsm-egress 或 GSM_EGRESS 未开启），该功能不可用。"
EGRESS_LOG_LIMIT_DEFAULT = 50
EGRESS_LOG_LIMIT_MAX = 200
EGRESS_CLI_COMMANDS = (
    "egress-show",
    "egress-set",
    "egress-apply",
    "egress-log",
    "egress-probe",
    "egress-domains",
    "egress-doctor",
)

DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$|^\[[0-9A-Fa-f:.]+\]$")
LISTEN_RE = re.compile(r"^(:\d{1,5}|[A-Za-z0-9.:-]+:\d{1,5}|\[[0-9A-Fa-f:.]+\]:\d{1,5})$")


# ---- 面板开关 / 来源闸门（契约 v3 第 3/4 节） ----


def request_is_local() -> bool:
    """请求是否来自本机（SSH 端口转发最终就是从本机进来的）。"""
    candidates = [str(request.remote_addr or "")]
    # 契约 v3 第 4 节：还要看 X-Forwarded-For（只有在本地反代/隧道下才有意义）。
    forwarded = str(request.headers.get("X-Forwarded-For", "") or "")
    if forwarded:
        candidates.extend(part.strip() for part in forwarded.split(",") if part.strip())
    for raw in candidates:
        host = raw.strip()
        if not host:
            continue
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        # IPv6 的 zone id（fe80::1%eth0）不属于地址本身。
        host = host.split("%", 1)[0]
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            continue
        if any(address in net for net in LOCAL_TRUSTED_NETS):
            return True
    return False


def panel_remote_allowed() -> bool:
    """settings.panel_remote=1 时才额外放行公网来源（默认 0）。"""
    return get_settings_flag("panel_remote", False)


def panel_enabled() -> bool:
    """面板开关状态（settings.panel_enabled，默认 1）。"""
    return get_settings_flag("panel_enabled", True)


def panel_remote_denied() -> bool:
    """非本机且不允许远程时的统一判定。"""
    return not request_is_local() and not panel_remote_allowed()


def require_local_or_admin_tunnel() -> Optional[Response]:
    """契约 v3 第 4 节来源闸门：非本机一律拒绝，返回 Response 或 None。

    不满足时返回 403，并把提示放进 session 的 flash 队列
    （403 本身不重定向，直接渲染 message.html 时也能读到这条提示）。
    """
    if not panel_remote_denied():
        return None
    flash(PANEL_REMOTE_DENIED_TEXT, "error")
    return render_message_page(
        "仅允许通过 SSH 隧道访问",
        f"{PANEL_REMOTE_DENIED_TEXT}。请在本地电脑上执行 "
        f"ssh -L {PANEL_PORT}:127.0.0.1:{PANEL_PORT} root@服务器地址，"
        f"然后用浏览器打开 http://127.0.0.1:{PANEL_PORT}/ 。",
        status=403,
    )


def render_message_page(title: str, message: str, status: int = 200) -> Response:
    """极简提示页（不依赖模板文件，避免远程/关闭状态还要渲染完整布局）。"""
    body = (
        "<!doctype html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title>"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<style>body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;"
        "background:#0f172a;color:#e2e8f0;display:flex;min-height:100vh;margin:0;"
        "align-items:center;justify-content:center}"
        ".card{background:#1e293b;border:1px solid #334155;border-radius:12px;"
        "padding:32px 40px;max-width:560px;line-height:1.7}"
        "h1{font-size:20px;margin:0 0 12px}p{margin:0;color:#cbd5e1;word-break:break-all}"
        "</style></head><body><div class=\"card\">"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>"
        "</div></body></html>"
    )
    return Response(body, status=status, mimetype="text/html; charset=utf-8")


def require_local(func):
    """来源闸门装饰器：登录页与登录后的敏感路由统一生效（契约 v3 第 4 节）。"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        denied = require_local_or_admin_tunnel()
        if denied is not None:
            return denied
        return func(*args, **kwargs)
    return wrapper


def panel_disabled_notice() -> Optional[str]:
    """面板已关闭时的页面提示（契约 v3 第 3 节：仍能启动，但只允许本机访问）。"""
    if panel_enabled():
        return None
    return (
        f"{PANEL_DISABLED_TEXT}：前端页面已通过指令关闭，端口映射等操作不可用（转发能力不受影响）。"
        "在服务器上执行 python app.py panel-enable 或 gsm panel on 可重新打开。"
    )


def get_settings_flag(key: str, default: bool) -> bool:
    """读 settings 里的布尔开关（0/1 文本）。"""
    raw = str(get_setting(key, "1" if default else "0") or "").strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def set_settings_flag(key: str, value: bool) -> None:
    set_setting(key, "1" if value else "0")


def read_panel_conf() -> Dict[str, str]:
    """读 panel.conf（BIND/PORT/ALLOW_REMOTE/ENABLED 键值文件，允许 # 注释）。"""
    values: Dict[str, str] = {}
    try:
        text = PANEL_CONF.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip().upper()] = val.strip()
    return values


def write_panel_conf(bind: str, port: int, allow_remote: bool, enabled: bool) -> None:
    """写 panel.conf；先读后写，保留文件里其他未知键，保证幂等。"""
    known = {
        "BIND": bind,
        "PORT": str(int(port)),
        "ALLOW_REMOTE": "1" if allow_remote else "0",
        "ENABLED": "1" if enabled else "0",
    }
    extra: Dict[str, str] = {}
    try:
        for line in PANEL_CONF.read_text(encoding="utf-8").splitlines():
            item = line.strip()
            if not item or item.startswith("#") or "=" not in item:
                continue
            key, _, val = item.partition("=")
            if key.strip().upper() not in known:
                extra[key.strip().upper()] = val.strip()
    except (OSError, UnicodeDecodeError):
        pass
    lines = ["# GOST SNI Manager 面板运行参数（由 app.py panel-* 与 gsm panel 维护）"]
    for key in ("BIND", "PORT", "ALLOW_REMOTE", "ENABLED"):
        lines.append(f"{key}={known[key]}")
    for key, val in sorted(extra.items()):
        lines.append(f"{key}={val}")
    PANEL_CONF.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(PANEL_CONF, "\n".join(lines) + "\n", mode=0o644)


def panel_conf_values() -> Dict[str, Any]:
    """合并 panel.conf 与 settings 的当前值（settings 是面板内可改的那份）。"""
    conf = read_panel_conf()
    bind = conf.get("BIND") or PANEL_DEFAULT_BIND
    try:
        port = int(conf.get("PORT") or PANEL_PORT)
    except ValueError:
        port = PANEL_PORT
    if "ALLOW_REMOTE" in conf:
        allow_remote = conf["ALLOW_REMOTE"].strip().lower() not in {"0", "false", "no", "off", ""}
    else:
        allow_remote = get_settings_flag("panel_remote", False)
    if "ENABLED" in conf:
        enabled = conf["ENABLED"].strip().lower() not in {"0", "false", "no", "off", ""}
    else:
        enabled = get_settings_flag("panel_enabled", True)
    return {
        "bind": bind or PANEL_DEFAULT_BIND,
        "port": port,
        "allow_remote": allow_remote,
        "enabled": enabled,
        "updated_at": get_setting("panel_updated_at", ""),
        "updated_by": get_setting("panel_updated_by", ""),
    }


def save_panel_state(actor: str = "", enabled: Optional[bool] = None,
                     allow_remote: Optional[bool] = None,
                     bind: Optional[str] = None, port: Optional[int] = None) -> Dict[str, Any]:
    """把开关写进 settings 与 panel.conf（两者同步），返回写后的状态。幂等。"""
    with _PANEL_ENABLED_LOCK:
        current = panel_conf_values()
        new_enabled = current["enabled"] if enabled is None else bool(enabled)
        new_remote = current["allow_remote"] if allow_remote is None else bool(allow_remote)
        new_bind = (bind or current["bind"] or PANEL_DEFAULT_BIND).strip()
        if new_remote and new_bind in {"127.0.0.1", "localhost", "::1"}:
            # 允许远程时必须真的监听出去，否则 SSH 隧道以外永远连不上。
            new_bind = PANEL_WILDCARD_BIND
        if not new_remote and new_bind in {PANEL_WILDCARD_BIND, "*"}:
            new_bind = PANEL_DEFAULT_BIND
        new_port = int(port or current["port"])
        set_settings_flag("panel_enabled", new_enabled)
        set_settings_flag("panel_remote", new_remote)
        set_setting("panel_updated_at", utc_now())
        set_setting("panel_updated_by", actor or "panel")
        write_panel_conf(new_bind, new_port, new_remote, new_enabled)
        state = panel_conf_values()
        state.update({"enabled": new_enabled, "allow_remote": new_remote,
                      "bind": new_bind, "port": new_port,
                      "updated_by": actor or "panel", "updated_at": get_setting("panel_updated_at", "")})
        return state


def panel_status_text(state: Dict[str, Any]) -> str:
    """panel-status 的纯文本输出（不跑 systemctl，那是 gsm 的职责）。"""
    running = "应运行" if state["enabled"] else "已关闭（不该运行）"
    remote = "允许远程" if state["allow_remote"] else "仅本机（SSH 隧道）"
    lines = [
        f"面板状态：{running}",
        f"监听地址：{state['bind']}:{state['port']}",
        f"来源限制：{remote}",
        f"最近开关：{state.get('updated_at') or '（无记录）'}  操作者：{state.get('updated_by') or '（无记录）'}",
        f"运行参数：{PANEL_CONF}",
    ]
    if not state["allow_remote"]:
        lines.append(f"连接方式：ssh -L {state['port']}:127.0.0.1:{state['port']} root@服务器地址"
                     f" 然后访问 http://127.0.0.1:{state['port']}/")
    return "\n".join(lines)


def egress_available() -> bool:
    """出口守卫是否可用（模块缺失或本机未启用时一律为 False）。"""
    if egress is None:
        return False
    try:
        return bool(egress.egress_available())
    except Exception as exc:  # 守卫探测失败不能把面板带崩
        print(f"[warn] 出口守卫可用性判断失败：{exc}", file=sys.stderr)
        return False


def egress_notes() -> list[str]:
    """面板启动时关于出口模块的诊断信息（仅提示，不影响运行）。"""
    if egress is None:
        return [f"出口访问守卫模块未加载：{EGRESS_IMPORT_ERROR or 'panel/egress.py 不存在'}"]
    return []


def egress_guard_response() -> Optional[Response]:
    """契约第 7 节：守卫不可用时统一 flash 并回控制台。"""
    if egress_available():
        return None
    flash(EGRESS_UNAVAILABLE_TEXT, "error")
    return redirect(url_for("dashboard"))


def egress_next_url(raw: Any = "") -> str:
    """只接受以 "/" 开头的站内路径，其余（含 // 与反斜杠）一律回控制台。"""
    candidate = str(raw or "").strip()
    if re.fullmatch(r"/(?!/)[^\s\\]*", candidate):
        return candidate
    return url_for("dashboard")


def egress_form_bool(name: str, default: bool = False) -> bool:
    """解析表单布尔字段，兼容 checkbox、隐藏 0/1 与 select 三种写法。"""
    values = [str(item).strip().lower() for item in request.form.getlist(name)]
    if not values:
        return default
    truthy = {"1", "on", "true", "yes", "enabled"}
    falsy = {"0", "off", "false", "no", "disabled"}
    if any(item in truthy for item in values):
        return True
    if any(item in falsy for item in values):
        return False
    return default


def egress_toggle_value(raw: Any) -> Optional[bool]:
    """开关表单只认明确的开/关值，其余返回 None 交给调用方报错。"""
    value = str(raw or "").strip().lower()
    if value in {"1", "on", "true", "yes"}:
        return True
    if value in {"0", "off", "false", "no"}:
        return False
    return None


def egress_log_action() -> str:
    """日志页 action 查询参数，只允许 allow/block，其余视为不筛选。"""
    action = str(request.args.get("action", "")).strip().lower()
    return action if action in {"allow", "block"} else ""


def egress_log_limit() -> int:
    """日志页条数，默认 50，上限 200（契约第 7 节）。"""
    raw = str(request.args.get("limit", "")).strip()
    if not raw:
        return EGRESS_LOG_LIMIT_DEFAULT
    try:
        limit = int(raw)
    except ValueError:
        return EGRESS_LOG_LIMIT_DEFAULT
    return max(1, min(limit, EGRESS_LOG_LIMIT_MAX))


def egress_summary_data() -> Dict[str, Any]:
    """契约第 8 节 egress_summary()，模块缺失时给出可优雅降级的默认值。"""
    fallback: Dict[str, Any] = {
        "enabled": False,
        "mode": "allowlist",
        "mode_label": "白名单",
        "domains": 0,
        "backend": "none",
        "ok": False,
        "detail": EGRESS_IMPORT_ERROR or "出口访问守卫未加载",
        "updated_at": "",
        "updated_by": "",
        "available": False,
        "installed": False,
        "block_quic": True,
        "ipv6": True,
        "domain_list": [],
        "dns_servers": [],
        "allow4_count": 0,
        "allow6_count": 0,
    }
    if egress is None:
        return fallback
    try:
        data = dict(egress.egress_summary())
    except Exception as exc:
        fallback["detail"] = f"读取出口状态失败：{exc}"
        return fallback
    for key, value in fallback.items():
        data.setdefault(key, value)
    return data


def egress_state_data() -> Dict[str, Any]:
    """契约第 8 节 egress_apply_state()，取不到时按"未下发"处理。"""
    fallback: Dict[str, Any] = {
        "ok": False,
        "detail": EGRESS_IMPORT_ERROR or "出口访问守卫未加载",
        "backend": "none",
        "last_at": None,
        "allow4_count": 0,
        "allow6_count": 0,
        "needs_apply": True,
        "policy_hash": "",
        "applied_hash": "",
    }
    if egress is None:
        return fallback
    try:
        data = dict(egress.egress_apply_state())
    except Exception as exc:
        fallback["detail"] = f"读取下发状态失败：{exc}"
        return fallback
    for key, value in fallback.items():
        data.setdefault(key, value)
    return data


def egress_stats_data() -> Dict[str, Any]:
    """契约第 8 节 egress_log_stats()。"""
    fallback: Dict[str, Any] = {"total": 0, "allow": 0, "block": 0, "last": None}
    if egress is None:
        return fallback
    try:
        data = dict(egress.egress_log_stats())
    except Exception:
        return fallback
    for key, value in fallback.items():
        data.setdefault(key, value)
    return data


def egress_logs_data(limit: int = 20, action: str = "") -> List[Dict[str, Any]]:
    """契约第 8 节 egress_log_tail()，字段固定 at/domain/ip/port/action/reason。"""
    if egress is None:
        return []
    try:
        rows = egress.egress_log_tail(limit=limit, action=action)
    except Exception:
        return []
    result: List[Dict[str, Any]] = []
    for row in rows or []:
        item = dict(row)
        result.append({
            "at": item.get("at", ""),
            "domain": item.get("domain", ""),
            "ip": item.get("ip", ""),
            "port": item.get("port", 0),
            "action": item.get("action", ""),
            "reason": item.get("reason", ""),
        })
    return result


def egress_actor() -> str:
    """当前操作者名字，写进策略的 updated_by；取不到时留空。"""
    user = get_current_user() or {}
    return str(user.get("username", "") or "")


def egress_apply_after_save(saved_message: str) -> None:
    """保存策略后立即下发；失败时说明策略已保存、数据面仍全拒绝（契约第 7 节）。"""
    try:
        state = dict(egress.egress_apply_now(actor=egress_actor()))
    except Exception as exc:
        flash(f"{saved_message}但数据面下发失败：{exc}。策略已保存，当前数据面仍保持全拒绝。", "error")
        return
    if bool(state.get("ok")):
        flash(saved_message, "success")
        return
    detail = str(state.get("detail", "") or "").strip() or "未知原因"
    flash(f"{saved_message}但数据面下发失败：{detail}。策略已保存，当前数据面仍保持全拒绝。", "error")


def egress_cli_argv(raw_args: Iterable[str]) -> Optional[List[str]]:
    """把 app.py 的 egress-* 命令转成 egress.py 独立进程等价的参数列表。

    例如 `python app.py egress-set --enable` <-> `python egress.py set --enable`。
    """
    args = [str(item) for item in raw_args]
    if not args or args[0] not in EGRESS_CLI_COMMANDS:
        return None
    return [args[0][len("egress-"):]] + args[1:]


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
            "cert_status": cert_status,
            # 契约第 8 节：base.html 导航据此决定是否显示出口入口。
            "egress_available": egress_available(),
            # 契约 v3：面板开关与来源状态（base.html 据此显示"面板已关闭"）。
            "panel_enabled": panel_enabled(),
            "panel_remote": panel_remote_allowed(),
            "panel_notice": panel_disabled_notice(),
            # 前端（dashboard.html）用这三个变量给出 SSH 隧道访问提示。
            "panel_local": request_is_local(),
            "panel_remote_allowed": panel_remote_allowed(),
            "panel_port": PANEL_PORT,
        }

    @app.get("/login")
    @require_local
    def login_page() -> str | Response:
        if is_logged_in():
            return redirect(url_for("dashboard"))
        return render_template("login.html", panel_enabled=panel_enabled(),
                               panel_notice=panel_disabled_notice())

    @app.post("/login")
    @require_local
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
    @require_local
    @require_login
    @require_csrf
    @require_panel_open
    def logout() -> Response:
        session.clear()
        flash("已退出登录。", "success")
        return redirect(url_for("login_page"))

    @app.get("/")
    @require_local
    @require_login
    def dashboard() -> str:
        user = require_current_user()
        stats = dashboard_stats(user)
        rules = list_rules_for_user(user)
        users = list_users() if user["role"] == "admin" else []
        status = collect_status()
        settings = load_settings()
        for rule in rules:
            rule["cert"] = cert_status(rule["sni"])
        panel_cert = cert_status(settings.get("panel_domain", ""))
        return render_template(
            "dashboard.html",
            stats=stats,
            rules=rules,
            users=users,
            status=status,
            settings=settings,
            apply_state=dict(APPLY_STATE),
            default_port=default_listen_port(),
            panel_cert=panel_cert,
            cert_dir=str(CERT_DIR),
            # 契约第 8 节：出口访问卡片所需变量，一定存在。
            egress=egress_summary_data(),
            egress_state=egress_state_data(),
            egress_stats=egress_stats_data(),
            egress_logs=egress_logs_data(limit=20),
            # 契约 v3：关闭时页面显示提示，前端据此隐藏操作入口。
            panel_enabled=panel_enabled(),
            panel_remote=panel_remote_allowed(),
            panel_notice=panel_disabled_notice(),
        )

    @app.post("/rules")
    @require_local
    @require_login
    @require_csrf
    @require_panel_open
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
    @require_local
    @require_login
    @require_csrf
    @require_panel_open
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
    @require_local
    @require_login
    @require_csrf
    @require_panel_open
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
    @require_local
    @require_login
    @require_csrf
    @require_panel_open
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
    @require_local
    @require_admin
    @require_csrf
    @require_panel_open
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
    @require_local
    @require_admin
    @require_csrf
    @require_panel_open
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
    @require_local
    @require_admin
    @require_csrf
    @require_panel_open
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
    @require_local
    @require_admin
    @require_csrf
    @require_panel_open
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
    @require_local
    @require_admin
    @require_csrf
    @require_panel_open
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
            # 契约 v3：表单出现 panel_* 字段时才改面板开关，避免漏字段把状态清空。
            panel_changed = ""
            if "panel_enabled" in request.form or "panel_remote" in request.form:
                actor = get_current_user() or {}
                state = save_panel_state(
                    actor=str(actor.get("username", "") or "panel"),
                    enabled=egress_form_bool("panel_enabled", panel_enabled())
                    if "panel_enabled" in request.form else None,
                    allow_remote=egress_form_bool("panel_remote", panel_remote_allowed())
                    if "panel_remote" in request.form else None,
                )
                panel_changed = (
                    f" 面板状态：{'开启' if state['enabled'] else '关闭'}，"
                    f"{'允许远程' if state['allow_remote'] else '仅本机（SSH 隧道）'}。"
                )
            flash("基础设置已保存，配置将在几秒内自动生效。" + panel_changed, "success")
        except (ValueError, RuntimeError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("dashboard"))

    @app.post("/apply")
    @require_local
    @require_login
    @require_csrf
    @require_panel_open
    def apply_now() -> Response:
        try:
            apply_config(restart=True)
            flash("配置已重新生成，GOST 将在几秒内自动重启。", "success")
        except RuntimeError as exc:
            flash(f"应用失败：{exc}", "error")
        return redirect(url_for("dashboard"))

    @app.get("/config")
    @require_local
    @require_admin
    def view_config() -> Response:
        return Response(generate_gost_config(), mimetype="text/plain; charset=utf-8")

    @app.get("/logs")
    @require_local
    @require_login
    def logs() -> str:
        gost_logs = run_cmd(["journalctl", "-u", GOST_SERVICE, "-n", "120", "--no-pager"], allow_fail=True)[1]
        relay_logs = run_cmd(["journalctl", "-u", RELAY_SERVICE, "-n", "120", "--no-pager"], allow_fail=True)[1]
        caddy_logs = run_cmd(["journalctl", "-u", CADDY_SERVICE, "-n", "60", "--no-pager"], allow_fail=True)[1]
        return render_template("logs.html", gost_logs=gost_logs, relay_logs=relay_logs, caddy_logs=caddy_logs)

    # ---- 出口访问控制（契约第 7 节）：全部主账号 + CSRF，失败都 flash 后重定向 ----

    @app.post("/egress/toggle")
    @require_local
    @require_admin
    @require_csrf
    @require_panel_open
    def toggle_egress() -> Response:
        guard = egress_guard_response()
        if guard is not None:
            return guard
        next_url = egress_next_url(request.form.get("next", ""))
        enabled = egress_toggle_value(request.form.get("enabled"))
        if enabled is None:
            flash("出口开关参数不正确，已忽略本次操作。", "error")
            return redirect(next_url)
        try:
            egress.save_egress_policy(enabled=enabled, actor=egress_actor())
        except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
            flash(f"出口开关保存失败：{exc}", "error")
            return redirect(next_url)
        egress_apply_after_save("出口访问已打开。" if enabled else "出口访问已关闭。")
        return redirect(next_url)

    @app.post("/egress/settings")
    @require_local
    @require_admin
    @require_csrf
    @require_panel_open
    def update_egress() -> Response:
        guard = egress_guard_response()
        if guard is not None:
            return guard
        next_url = egress_next_url(request.form.get("next", ""))
        changes: Dict[str, Any] = {}
        mode = str(request.form.get("mode", "") or "").strip().lower()
        if mode:
            modes = tuple(getattr(egress, "EGRESS_MODES", ("allowlist", "all")))
            if mode not in modes:
                flash(f"出口模式不正确：{mode}。", "error")
                return redirect(next_url)
            changes["mode"] = mode
        # 表单没出现的字段保持原值，避免前端漏字段时把策略清空。
        for field in ("enabled", "block_quic", "ipv6"):
            if field in request.form:
                changes[field] = egress_form_bool(field, default=False)
        if "domains" in request.form:
            try:
                changes["domains"] = egress.parse_domain_text(request.form.get("domains", ""))
            except ValueError as exc:
                flash(f"域名清单有误：{exc}", "error")
                return redirect(next_url)
        try:
            egress.save_egress_policy(actor=egress_actor(), **changes)
        except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
            flash(f"出口设置保存失败：{exc}", "error")
            return redirect(next_url)
        egress_apply_after_save("出口访问设置已保存。")
        return redirect(next_url)

    @app.post("/egress/probe")
    @require_local
    @require_admin
    @require_csrf
    @require_panel_open
    def probe_egress() -> Response:
        guard = egress_guard_response()
        if guard is not None:
            return guard
        next_url = egress_next_url(request.form.get("next", ""))
        host = str(request.form.get("host", "") or "").strip()
        port_raw = str(request.form.get("port", "") or "").strip() or "443"
        if not host:
            flash("请填写要探测的主机名或 IP。", "error")
            return redirect(next_url)
        try:
            port = int(port_raw)
        except ValueError:
            flash("探测端口必须是 1-65535 的整数。", "error")
            return redirect(next_url)
        if not 1 <= port <= 65535:
            flash("探测端口必须是 1-65535 的整数。", "error")
            return redirect(next_url)
        try:
            result = dict(egress.egress_probe(host, port))
        except (ValueError, RuntimeError, OSError) as exc:
            flash(f"出口探测失败：{exc}", "error")
            return redirect(next_url)
        elapsed = result.get("elapsed_ms", 0)
        detail = str(result.get("detail", "") or "").strip()
        if result.get("ok"):
            flash(f"出口探测 {host}:{port} 成功（{elapsed} ms）{detail}", "success")
        else:
            flash(f"出口探测 {host}:{port} 失败（{elapsed} ms）：{detail or '连接不成功'}", "error")
        return redirect(next_url)

    @app.get("/egress/log")
    @require_local
    @require_admin
    def egress_log_page() -> str | Response:
        guard = egress_guard_response()
        if guard is not None:
            return guard
        action = egress_log_action()
        limit = egress_log_limit()
        return render_template(
            "egress_log.html",
            egress=egress_summary_data(),
            egress_state=egress_state_data(),
            egress_stats=egress_stats_data(),
            logs=egress_logs_data(limit=limit, action=action),
            action=action,
            limit=limit,
        )

    @app.get("/egress/log.json")
    @require_local
    @require_admin
    def egress_log_json() -> Response:
        guard = egress_guard_response()
        if guard is not None:
            return guard
        action = egress_log_action()
        limit = egress_log_limit()
        payload = {
            "ok": True,
            "action": action,
            "limit": limit,
            "stats": egress_stats_data(),
            "items": egress_logs_data(limit=limit, action=action),
        }
        return Response(json.dumps(payload, ensure_ascii=False), mimetype="application/json")

    @app.post("/egress/reapply")
    @require_local
    @require_admin
    @require_csrf
    @require_panel_open
    def reapply_egress() -> Response:
        guard = egress_guard_response()
        if guard is not None:
            return guard
        next_url = egress_next_url(request.form.get("next", ""))
        try:
            state = dict(egress.egress_apply_now(actor=egress_actor()))
        except (ValueError, RuntimeError, OSError) as exc:
            flash(f"重新下发出口策略失败：{exc}。数据面仍保持全拒绝。", "error")
            return redirect(next_url)
        if bool(state.get("ok")):
            flash("出口策略已重新下发。", "success")
        else:
            detail = str(state.get("detail", "") or "").strip() or "未知原因"
            flash(f"重新下发出口策略失败：{detail}。数据面仍保持全拒绝。", "error")
        return redirect(next_url)

    return app


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GOST_CONFIG.parent.mkdir(parents=True, exist_ok=True)


def get_db() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_FILE)
    # Wait instead of failing when the relay is writing counters at the same time.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with closing(get_db()) as db, db:
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
        # 出口访问控制表（契约第 6 节），旧数据库可直接升级；
        # 没有守卫模块时跳过，且建表失败不应阻塞面板启动。
        if egress is not None:
            try:
                egress.ensure_egress_schema()
            except Exception as exc:
                print(f"[warn] 出口访问表初始化失败：{exc}", file=sys.stderr)
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('listen',?)", (DEFAULT_LISTEN,))
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('fallback_addr',?)", (DEFAULT_FALLBACK,))
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('panel_domain',?)", (PANEL_DOMAIN,))
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('forwarding_mode', 'direct')")
        # 契约 v3 第 3 节：面板默认开启、默认只允许本机（SSH 隧道）访问。
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('panel_enabled','1')")
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('panel_remote','0')")
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


def validate_listen_port(port: int, actor: Optional[Dict[str, Any]] = None) -> None:
    """Reject inbound ports that would fight with the panel, Caddy or the relay range.

    A dedicated inbound port is also an extra public listener on the transit
    server, so only the main account may open one (see `enforce_dedicated_port_policy`).
    """
    if not (1 <= port <= 65535):
        raise ValueError("入站端口范围必须是 1-65535。")
    if port == PANEL_PORT:
        raise ValueError(f"入站端口 {port} 是面板自身端口，请换一个。")
    if port == CADDY_HTTPS_PORT:
        raise ValueError(f"入站端口 {port} 是 Caddy fallback 端口，请换一个。")
    if RELAY_PORT_MIN <= port <= RELAY_PORT_MAX:
        raise ValueError(f"入站端口不能落在本地中继保留段 {RELAY_PORT_MIN}-{RELAY_PORT_MAX} 内。")
    # Anything below the default listen port is a well-known service port
    # (80/443/8080-ish ranges used by mail, DNS, SSH, databases). Opening one
    # of those on the transit VPS shadows a real service and widens the attack
    # surface for no gain, because clients can always use the shared port.
    default_port = default_listen_port()
    if port < default_port and port != PANEL_PORT:
        raise ValueError(
            f"入站端口 {port} 低于默认监听端口 {default_port}，"
            "属于系统/常见服务端口段，请改用默认端口或更高的端口。"
        )


def enforce_dedicated_port_policy(port: int, actor: Optional[Dict[str, Any]],
                                  existing_rule: Optional[Dict[str, Any]] = None) -> None:
    """Only the main account may open a dedicated inbound port.

    A dedicated port is a second public listener on the transit host. Letting a
    sub-account choose one hands it an arbitrary public port on somebody else's
    server; the shared port plus SNI dispatch already serves every rule.
    """
    if port == default_listen_port():
        return
    if existing_rule and int(existing_rule.get("listen_port") or 0) == port:
        return
    if actor is None or actor.get("role") == "admin":
        return
    raise ValueError("只有主账号可以设置独立的入站端口，子账号请使用默认端口。")


def port_owner(port: int) -> Optional[str]:
    """Process name holding `port`, "" when free, None when unknown.

    Checking with `ss` rather than a probe bind avoids briefly occupying the
    port and avoids a false negative on servers where SO_REUSEADDR lets a
    second bind succeed.
    """
    code, out = run_cmd(["ss", "-ltnpH", f"sport = :{port}"], allow_fail=True)
    if code != 0:
        return None
    lines = [l for l in out.splitlines() if l.strip().startswith("LISTEN")]
    if not lines:
        return ""
    match = re.search(r'users:\(\("([^"]+)"', out)
    return match.group(1) if match else ""


def port_in_use(port: int, exclude: Tuple[str, ...] = ()) -> bool:
    """True when a program other than `exclude` holds the port."""
    owner = port_owner(port)
    if owner is None:
        # `ss` unavailable: fall back to a plain listen check rather than
        # silently letting a conflicting port through.
        code, out = run_cmd(["bash", "-lc", f"ss -ltn 2>/dev/null | grep -E ':{port}\\b' || true"], allow_fail=True)
        return code == 0 and bool(out.strip())
    if owner == "":
        # Listener exists but its process is not readable (non-root panel).
        return port_reserved_by_panel(port)
    return owner not in exclude


def port_reserved_by_panel(port: int) -> bool:
    """A listener without a readable owner still counts when the panel claims it."""
    if port == default_listen_port():
        return True
    return any(int(r.get("listen_port") or 0) == port for r in query_all("SELECT listen_port FROM rules"))


def validate_listen_port_available(port: int, keep: Optional[int] = None) -> None:
    """Reject a new inbound port that another program already occupies.

    GOST binds these ports itself, so a port held by anything else (a service,
    a stray process) can never serve the rule. Ports the panel already manages
    are fine: GOST (or the relay) owns them and reloads in place, and the
    `keep` port is the one this rule currently uses.
    """
    if keep is not None and port == keep:
        return
    if port == default_listen_port():
        return
    if not port_in_use(port, exclude=(os.path.basename(shutil.which("gost") or "gost"),)):
        return
    raise ValueError(f"入站端口 {port} 已被其他程序占用，请换一个。")


def row_to_dict(row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def query_one(sql: str, params: Tuple[Any, ...] = ()) -> Optional[Dict[str, Any]]:
    with closing(get_db()) as db:
        return row_to_dict(db.execute(sql, params).fetchone())


def query_all(sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    with closing(get_db()) as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def execute(sql: str, params: Tuple[Any, ...] = ()) -> None:
    with closing(get_db()) as db, db:
        db.execute(sql, params)
        db.commit()


def executemany(sql: str, params: Iterable[Tuple[Any, ...]]) -> None:
    with closing(get_db()) as db, db:
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


_cert_cache: Dict[str, Dict[str, Any]] = {}
_cert_cache_lock = threading.Lock()
_CERT_CACHE_TTL = 300.0


def cert_status(domain: str) -> Dict[str, Any]:
    """Look for a certificate matching `domain` under CERT_DIR.

    Accepts `<domain>.cer`, `<domain>.crt`, `<domain>.pem`, or the generic
    `fullchain.cer`. Returns a dict with keys: exists (bool), path (str|None),
    not_after (datetime|None), issuer (str|None), days_left (int|None),
    state ("ok"|"expiring"|"expired"|"none"|"error").
    Result is cached for 5 minutes so dashboard loads stay cheap.
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return {"exists": False, "path": None, "not_after": None, "issuer": None, "days_left": None, "state": "none"}
    now = time.time()
    with _cert_cache_lock:
        cached = _cert_cache.get(domain)
        if cached and now - cached["_ts"] < _CERT_CACHE_TTL:
            return {k: v for k, v in cached.items() if k != "_ts"}
    result = _cert_status_uncached(domain)
    result["_ts"] = now
    with _cert_cache_lock:
        _cert_cache[domain] = result
    return {k: v for k, v in result.items() if k != "_ts"}


def _cert_status_uncached(domain: str) -> Dict[str, Any]:
    base = {"exists": False, "path": None, "not_after": None, "issuer": None, "days_left": None, "state": "none"}
    if not CERT_DIR.is_dir():
        return base
    candidates = [
        CERT_DIR / f"{domain}.cer",
        CERT_DIR / f"{domain}.crt",
        CERT_DIR / f"{domain}.pem",
        CERT_DIR / "fullchain.cer",
        CERT_DIR / "fullchain.pem",
    ]
    cert_path = next((c for c in candidates if c.is_file()), None)
    if cert_path is None:
        # Also match the x-ui convention: <dir>/<domain>/fullchain.cer
        nested = CERT_DIR / domain / "fullchain.cer"
        if nested.is_file():
            cert_path = nested
    if cert_path is None:
        return base
    try:
        cert = ssl._ssl._test_decode_cert(str(cert_path))
    except Exception:
        return {**base, "exists": True, "path": str(cert_path), "state": "error"}
    # Domain-specific files are trusted by filename; generic names (fullchain.*)
    # must actually cover the requested domain via CN or SAN.
    if cert_path.name in ("fullchain.cer", "fullchain.pem"):
        subject_names = set()
        for group in cert.get("subject", []):
            for pair in group:
                if pair[0] in ("commonName",):
                    subject_names.add(pair[1].lower())
        for san_group in cert.get("subjectAltName", []):
            if isinstance(san_group, tuple) and len(san_group) == 2:
                subject_names.add(str(san_group[1]).lower())
        if domain not in subject_names:
            return base
    not_after_raw = cert.get("notAfter")
    not_after: Optional[datetime] = None
    if not_after_raw:
        for fmt in ("%b %d %H:%M:%S %Y %Z", "%b %d %H:%M:%S %Y GMT"):
            try:
                not_after = datetime.strptime(not_after_raw, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
    issuer_parts = cert.get("issuer", [])
    issuer = ""
    for group in issuer_parts:
        for pair in group:
            if pair[0] == "organizationName":
                issuer = pair[1]
    days_left = None
    state = "ok"
    if not_after:
        delta = (not_after - datetime.now(timezone.utc)).days
        days_left = delta
        if delta < 0:
            state = "expired"
        elif delta <= 14:
            state = "expiring"
    return {
        "exists": True,
        "path": str(cert_path),
        "not_after": not_after,
        "issuer": issuer or None,
        "days_left": days_left,
        "state": state,
    }


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


def require_panel_open(func):
    """契约 v3 第 3 节：面板被指令关闭后，写操作一律拒绝（连本机也不行）。

    只看不写的页面（控制台、日志、访问流水）仍可打开，方便从 SSH 隧道里查状态。
    POST 才是改变端口映射/账号/出口的入口，所以闸门加在 POST 侧。
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not panel_enabled():
            flash("面板已关闭，请先在服务器上执行 gsm panel on 打开后再操作。", "error")
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
    validate_listen_port(listen_port, actor)
    enforce_dedicated_port_policy(listen_port, actor, existing_rule)
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


def render_service_lines(port: int, rules: List[Dict[str, Any]], is_default: bool,
                         listen: str, fallback: str, mode: str) -> List[str]:
    """Render one GOST service, keyed on the inbound port.

    Every service sniffs the ClientHello and matches on the SNI, including a
    port that carries only a single rule. The old generator let a lone rule on
    a dedicated port become a plain TCP forward (no sniffing, no matcher),
    which sends *every* client that reaches the port to that rule's target:
    any SNI, any unrelated TLS session, and any client that was never given the
    SNI all came out of the landing server's exit, on any port the user typed
    in. That is what turned the inbound port into a free exit channel.

    Two properties keep the dispatch honest:

    * every client node is matcher-gated on its own SNI;
    * the forwarder uses strategy ``fifo``, because GOST's default round-robin
      picks a node *before* the ClientHello is sniffed, so a client could be
      sent to a node whose SNI does not match. FIFO walks the nodes in order
      and takes the first one whose matcher passes.
    """
    addr = listen if is_default else f":{port}"
    lines = [
        f"- name: reality-{port}",
        f'  addr: "{addr}"',
        "  handler:",
        "    type: tcp",
        "    metadata:",
        "      sniffing: true",
        "  listener:",
        "    type: tcp",
        "  forwarder:",
        "    nodes:",
    ]
    for rule in rules:
        sni = str(rule["sni"]).lower()
        if mode == "carpool":
            backend = f"{RELAY_HOST}:{int(rule['relay_port'])}"
        else:
            backend = f"{rule['target']}:{int(rule['port'])}"
        lines.extend([
            f"    - name: {sanitize_node_name(sni)}",
            f"      addr: {backend}",
            "      matcher:",
            f"        rule: Host(`{sni}`)",
        ])
    # The fallback is the only catch-all and it exists on the default port
    # only. A dedicated inbound port has no catch-all, so a client whose SNI
    # does not match is closed instead of being handed to some landing target.
    if is_default:
        lines.extend([
            f"    - name: {sanitize_node_name('', fallback=True)}",
            f"      addr: {fallback}",
        ])
    lines.extend(["  selector:", "    strategy: fifo"])
    return lines


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
        is_default = port == default_port
        lines.extend(render_service_lines(port, groups[port], is_default, listen, fallback, mode))
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


def validate_sni_gating(lines: List[str], service_names: List[str]) -> None:
    """Refuse any config where a client-bearing node is not SNI-gated.

    This is the regression guard for the open-relay bug: a node without a
    `matcher` accepts every client that reaches the port, so the inbound port
    turns into a free exit through the landing server. The only node allowed
    to be matchless is the last one of a service, and only when it is the
    configured Caddy fallback (that node must stay catch-all by design).
    """
    services: List[List[str]] = []
    current: Optional[List[str]] = None
    for line in lines:
        if line.startswith("- name: "):
            current = []
            services.append(current)
        elif current is not None:
            current.append(line)
    if len(services) != len(service_names):
        raise RuntimeError("生成的 GOST 配置无效：服务段解析失败。")
    for svc_index, block in enumerate(services):
        # Lines are grouped *after* the "- name:" line, so the service name
        # has to be tracked separately instead of read from the block.
        name = service_names[svc_index].strip()
        if "      sniffing: true" not in block:
            raise RuntimeError(f"生成的 GOST 配置无效：服务 {name} 没有开启 SNI 嗅探。")
        if "    strategy: fifo" not in block:
            raise RuntimeError(f"生成的 GOST 配置无效：服务 {name} 没有配置 fifo 选择策略。")
        # Walk the node list; group every node by its position so a matchless
        # node can be checked against the fallback rule.
        node_starts = [i for i, l in enumerate(block) if l.startswith("    - name:")]
        # The node list ends where the first sibling key of the service
        # (selector / chain) starts again at the 2-space indent level.
        list_end = len(block)
        for i in range(node_starts[-1] + 1 if node_starts else 0, len(block)):
            if block[i].startswith("  ") and not block[i].startswith("   "):
                list_end = i
                break
        for idx, start in enumerate(node_starts):
            end = node_starts[idx + 1] if idx + 1 < len(node_starts) else list_end
            node = block[start:end]
            if any(l.strip() == "matcher:" for l in node):
                continue
            is_last = idx == len(node_starts) - 1
            is_fallback = any(l.strip() == "- name: caddy-fallback" for l in node)
            if not (is_last and is_fallback):
                raise RuntimeError(
                    f"生成的 GOST 配置无效：服务 {name} 的节点 {node[0].strip()} 没有 SNI 匹配规则，"
                    "会把任意客户端转发到落地，等于开放出口。"
                )


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
    validate_sni_gating(lines, service_names)


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
            # INIT_RULES runs from the installer as the main account.
            validate_listen_port(listen_port, {"role": "admin"})
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


def reconcile_panel_state() -> Dict[str, Any]:
    """确保 panel.conf 与 settings 一致：缺文件按 settings 生成，有文件则回写 settings。"""
    state = panel_conf_values()
    if not PANEL_CONF.is_file():
        write_panel_conf(state["bind"], state["port"], state["allow_remote"], state["enabled"])
    else:
        set_settings_flag("panel_enabled", state["enabled"])
        set_settings_flag("panel_remote", state["allow_remote"])
    return state


def panel_cli(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """panel-enable / panel-disable / panel-status / panel-remote（契约 v3 第 3 节）。

    只维护状态与 panel.conf，不执行 systemctl（服务启停是 gsm 的职责）。
    退出码：0 表示应运行/操作成功，1 表示面板已关闭（仅 panel-status 会返回 1）。
    """
    command = args.command
    want_json = bool(getattr(args, "json", False))
    actor = str(getattr(args, "actor", "") or "gsm-cli")
    reconcile_panel_state()
    if command == "panel-enable":
        state = save_panel_state(actor=actor, enabled=True)
        code = 0
        headline = "面板已开启（页面可操作，端口映射恢复网页管理）。"
    elif command == "panel-disable":
        state = save_panel_state(actor=actor, enabled=False)
        code = 0
        headline = ("面板已关闭（仅本机可访问且页面只读提示；GOST/出口/转发能力不受影响）。")
    elif command == "panel-remote":
        allow = bool(getattr(args, "on", False))
        state = save_panel_state(actor=actor, allow_remote=allow)
        code = 0
        if allow:
            headline = (f"警告：已允许公网直接访问面板（绑定 {state['bind']}）。"
                        "强烈建议改回 panel-remote --off，只用 SSH 隧道。")
            print(headline, file=sys.stderr)
            headline = "已允许远程访问（绑定 0.0.0.0）。"
        else:
            headline = "已恢复仅本机访问（绑定 127.0.0.1），请使用 SSH 隧道。"
            print(f"如需绑定立即生效：systemctl restart {PANEL_SERVICE}"
                  "（本命令不代为执行 systemd 操作）。", file=sys.stderr)
    elif command == "panel-status":
        state = panel_conf_values()
        code = 0 if state["enabled"] else 1
        headline = ""
    else:
        parser.print_help()
        return 1
    if want_json:
        payload = {
            "ok": True,
            "enabled": bool(state["enabled"]),
            "should_run": bool(state["enabled"]),
            "bind": state["bind"],
            "port": int(state["port"]),
            "allow_remote": bool(state["allow_remote"]),
            "updated_at": state.get("updated_at") or "",
            "updated_by": state.get("updated_by") or "",
            "conf": str(PANEL_CONF),
        }
        print(json.dumps(payload, ensure_ascii=False))
        return code
    if headline:
        print(headline)
    print(panel_status_text(state))
    return code


def cli(argv: Optional[List[str]] = None) -> int:
    # 契约第 6 节：app.py 的 egress-* 只做参数转发，交给 egress.egress_cli()，
    # 转发前把命令名换成独立进程的写法（egress-show -> show）。
    # 先于 init_db() 处理，避免在未启用守卫的机器上顺带建面板数据库。
    raw_args = list(sys.argv[1:] if argv is None else argv)
    forwarded = egress_cli_argv(raw_args)
    if forwarded is not None:
        if egress is None:
            print(f"本机未启用出口访问守卫：{EGRESS_IMPORT_ERROR or 'panel/egress.py 不存在'}", file=sys.stderr)
            return 1
        return int(egress.egress_cli(forwarded))

    parser = argparse.ArgumentParser(description=APP_NAME)
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run web panel")
    # 契约 v3 第 4 节：绑定地址由 panel.conf 决定，显式 --host/--port 仍然优先。
    _conf = read_panel_conf() if PANEL_CONF.is_file() else {}
    serve.add_argument("--host", default=os.environ.get("GOST_PANEL_HOST")
                       or _conf.get("BIND") or PANEL_DEFAULT_BIND)
    serve.add_argument("--port", type=int, default=int(os.environ.get("GOST_PANEL_PORT")
                                                      or _conf.get("PORT") or PANEL_PORT))

    init = sub.add_parser("init", help="initialize admin account and state")
    init.add_argument("--username", required=True)
    init.add_argument("--password", required=True)
    init.add_argument("--listen", default=DEFAULT_LISTEN)
    init.add_argument("--fallback", default=DEFAULT_FALLBACK)
    init.add_argument("--panel-domain", default=PANEL_DOMAIN)
    init.add_argument("--init-rules", default=os.environ.get("INIT_RULES", ""))
    init.add_argument("--mode", default=os.environ.get("GOST_FORWARD_MODE", "direct"), choices=["direct", "carpool"])
    init.add_argument("--force-auth", action="store_true")

    panel_enable_cmd = sub.add_parser("panel-enable", help="enable the web panel (state + panel.conf)")
    panel_enable_cmd.add_argument("--actor", default=os.environ.get("GSM_ACTOR", "gsm-cli"))
    panel_enable_cmd.add_argument("--json", action="store_true")

    panel_disable_cmd = sub.add_parser("panel-disable", help="disable the web panel (state + panel.conf)")
    panel_disable_cmd.add_argument("--actor", default=os.environ.get("GSM_ACTOR", "gsm-cli"))
    panel_disable_cmd.add_argument("--json", action="store_true")

    panel_status_cmd = sub.add_parser("panel-status", help="print panel state (exit 0=running, 1=off)")
    panel_status_cmd.add_argument("--json", action="store_true")

    panel_remote_cmd = sub.add_parser("panel-remote", help="allow/deny non-local (public) panel access")
    remote_group = panel_remote_cmd.add_mutually_exclusive_group(required=True)
    remote_group.add_argument("--on", action="store_true", help="allow remote access (bind 0.0.0.0)")
    remote_group.add_argument("--off", action="store_true", help="local only (bind 127.0.0.1)")
    panel_remote_cmd.add_argument("--actor", default=os.environ.get("GSM_ACTOR", "gsm-cli"))
    panel_remote_cmd.add_argument("--json", action="store_true")

    sub.add_parser("render-config", help="write gost config from database")
    sub.add_parser("show-config", help="print gost config")

    args = parser.parse_args(raw_args)
    init_db()
    # 契约 v3 第 3 节：面板开关只改状态与 panel.conf，进程/服务启停归 gsm 负责。
    if args.command in {"panel-enable", "panel-disable", "panel-status", "panel-remote"}:
        return panel_cli(args, parser)
    if args.command == "serve":
        if not panel_enabled():
            # 契约 v3 第 3 节：关闭后进程仍可启动，页面只提示"面板已关闭"且仅本机可访问。
            print(f"[warn] {PANEL_DISABLED_TEXT}：仅本机（SSH 隧道）可访问，"
                  f"执行 python app.py panel-enable 重新打开。", file=sys.stderr)
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
