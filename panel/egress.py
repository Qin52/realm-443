#!/usr/bin/env python3
"""GSM 出口访问守卫（egress guard）。

职责（TEAM_CONTRACT.md 第 4/5/6 节）：

1. 策略存在 SQLite 的 egress_policy 表（单行 id=1），访问流水存在 egress_log 表；
2. 数据面用 nftables 的 inet/ip6 两张表，开关和白名单都通过“重建表”热更新；
3. 常驻守护进程负责下发、每 2 秒轮询策略哈希、每 30 秒自愈、DNS 缓存和内核日志流水；
4. 面板（app.py）与便携命令（tools/gsm）都只走本模块的公共 API，判定逻辑只有这一套。

安全底线：任何一次下发失败都必须回落到“全拒绝”形态，绝不允许出现
“表不存在 / 链为空 = 放行”的状态（契约第 10 节第 1、3 条）。
本模块不 import app.py、不依赖 Flask，保持可独立运行与可单元测试。
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

try:
    import fcntl  # Linux 上用文件锁串行化“面板 + 守护进程”的下发
except ImportError:  # pragma: no cover - Windows 测试环境没有 fcntl
    fcntl = None  # type: ignore[assignment]


def _env_flag(name: str, default: bool = False) -> bool:
    """读取布尔环境变量；0/false/no/off 视为假。"""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


EGRESS_ENABLED = _env_flag("GSM_EGRESS", True)
EGRESS_MODES = ("allowlist", "all")
RUNTIME_DIR = Path(os.environ.get("GSM_EGRESS_RUNDIR") or "/run/gsm-egress")
DATA_DIR = Path(os.environ.get("GOST_PANEL_DATA", "/etc/gost-panel"))
DB_FILE = Path(os.environ.get("GOST_PANEL_DB", str(DATA_DIR / "gsm.db")))
RESOLV_CONF = Path(os.environ.get("GSM_EGRESS_RESOLV_CONF", "/etc/resolv.conf"))
SERVICE_FILE = Path(
    os.environ.get("GSM_EGRESS_SERVICE_FILE", "/etc/systemd/system/gsm-egress.service")
)

# nft 规则里必须永远放行的内网/本机地址（契约第 4 节第 2 条）。
PRIVATE4 = (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "100.64.0.0/10",
    "224.0.0.0/4",
    "255.255.255.255",
)
PRIVATE6 = ("::1", "fc00::/7", "fe80::/10", "ff00::/8")
LOG_LIMIT = "limit rate 12/minute burst 6"
ALLOW_PREFIX = "gsm-egress-allow:"
BLOCK_PREFIX = "gsm-egress-block:"
DQ = chr(34)  # nft 规则里的引号，集中一处避免转义债
STATE_FILE = "state.json"
LOCK_FILE = "apply.lock"
RESOLVE_TTL = 60.0

# 域名标签：小写字母/数字/横线，不以横线开头结尾。
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_TLD_RE = re.compile(r"^[a-z]{2,63}$")
_DST_RE = re.compile(r"\bDST=([0-9A-Fa-f:.]+)")
_DPT_RE = re.compile(r"\bDPT=(\d{1,5})")
_PROTO_RE = re.compile(r"\bPROTO=([A-Za-z0-9]+)")

_RESOLVE_CACHE: Dict[str, Tuple[float, Tuple[str, ...]]] = {}
_RESOLVE_LOCK = threading.Lock()
_APPLY_THREAD_LOCK = threading.Lock()
_LOG_INSERT_COUNT = 0
_LOG_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 路径与数据库
# ---------------------------------------------------------------------------


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _db_path() -> Path:
    """数据库位置：GOST_PANEL_DB 优先，其次 GOST_PANEL_DATA/gsm.db（与 app.py 一致）。"""
    raw = os.environ.get("GOST_PANEL_DB")
    if raw:
        return Path(raw)
    data = os.environ.get("GOST_PANEL_DATA")
    if data:
        return Path(data) / "gsm.db"
    return DB_FILE


def _runtime_dir_path() -> Path:
    raw = os.environ.get("GSM_EGRESS_RUNDIR")
    return Path(raw) if raw else RUNTIME_DIR


def egress_runtime_dir() -> Path:
    """确保运行时目录存在；失败时退回临时目录（契约第 6 节）。"""
    path = _runtime_dir_path()
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "gsm-egress"
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return fallback


def get_db() -> sqlite3.Connection:
    """连接策略库。与 app.py 共用同一个 SQLite 文件，busy_timeout 避免互相写失败。"""
    path = _db_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    conn = sqlite3.connect(str(path), timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


# egress_policy 的列定义：既用于建表，也用于给“旧库”补列（幂等升级）。
_POLICY_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("mode", "TEXT NOT NULL DEFAULT 'allowlist'"),
    ("domains", "TEXT NOT NULL DEFAULT ''"),
    ("block_quic", "INTEGER NOT NULL DEFAULT 1"),
    ("ipv6", "INTEGER NOT NULL DEFAULT 1"),
    ("updated_at", "TEXT"),
    ("updated_by", "TEXT"),
)

_LOG_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("at", "TEXT NOT NULL DEFAULT ''"),
    ("domain", "TEXT DEFAULT ''"),
    ("ip", "TEXT DEFAULT ''"),
    ("port", "INTEGER DEFAULT 0"),
    ("action", "TEXT NOT NULL DEFAULT ''"),
    ("reason", "TEXT DEFAULT ''"),
)


def _ensure_columns(db: sqlite3.Connection, table: str, columns: Tuple[Tuple[str, str], ...]) -> None:
    """给已存在的表补齐缺失列，保证旧数据库可以原地升级。"""
    existing = {row["name"] for row in db.execute("PRAGMA table_info(%s)" % table).fetchall()}
    for name, ddl in columns:
        if name not in existing:
            db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl))


def ensure_egress_schema() -> None:
    """建表/补列/写入默认策略行，必须幂等（契约第 11 节）。"""
    with closing(get_db()) as db, db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS egress_policy (
                id INTEGER PRIMARY KEY CHECK(id=1),
                enabled INTEGER NOT NULL DEFAULT 0,
                mode TEXT NOT NULL DEFAULT 'allowlist',
                domains TEXT NOT NULL DEFAULT '',
                block_quic INTEGER NOT NULL DEFAULT 1,
                ipv6 INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT,
                updated_by TEXT
            );
            CREATE TABLE IF NOT EXISTS egress_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                domain TEXT DEFAULT '',
                ip TEXT DEFAULT '',
                port INTEGER DEFAULT 0,
                action TEXT NOT NULL,
                reason TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS egress_log_id_idx ON egress_log(id);
            """
        )
        _ensure_columns(db, "egress_policy", _POLICY_COLUMNS)
        _ensure_columns(db, "egress_log", _LOG_COLUMNS)
        db.execute("INSERT OR IGNORE INTO egress_policy(id) VALUES(1)")
        db.commit()


# ---------------------------------------------------------------------------
# 域名与策略
# ---------------------------------------------------------------------------


def normalize_domain(raw: str) -> str:
    """规范化单个域名/IP 字面量；非法时 raise ValueError（契约第 6 节）。

    - 小写、去掉末尾的点；容忍用户写成 "*.example.com"（存成 example.com，
      因为 example.com 的规则本身已经覆盖所有子域）；
    - 允许 IPv4/IPv6 字面量，返回其规范形式（直接进 nft allow 集合）；
    - 单层主机名（没有点）视为笔误直接拒绝。
    """
    text = (raw or "").strip().lower()
    if not text:
        raise ValueError("域名不能为空。")
    if text.startswith("*."):
        text = text[2:]
    if text.endswith("."):
        text = text.rstrip(".")
    if not text:
        raise ValueError("域名不能为空。")
    if len(text) > 253:
        raise ValueError("域名过长（最多 253 个字符）。")
    if any(ch in text for ch in (" ", "\t", "/", "\\", "`", "\n", "\r", ",", ";")):
        raise ValueError("域名包含非法字符：%s" % text)
    # IP 字面量优先判断，IPv6 里的冒号不能被当成域名。
    try:
        return str(ipaddress.ip_address(text.split("%", 1)[0]))
    except ValueError:
        pass
    labels = text.split(".")
    if len(labels) < 2:
        raise ValueError("域名格式不正确：%s" % text)
    for label in labels:
        if not _LABEL_RE.match(label):
            raise ValueError("域名格式不正确：%s" % text)
    if not _TLD_RE.match(labels[-1]):
        raise ValueError("域名后缀不正确：%s" % text)
    return text


def parse_domain_text(raw: str) -> List[str]:
    """把多行/逗号分隔的域名清单解析成去重保序的列表；任意一项非法即报错。"""
    if raw is None:
        return []
    text = str(raw)
    items: List[str] = []
    seen: Set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for token in re.split(r"[\s,;]+", line):
            token = token.strip()
            if not token or token.startswith("#"):
                continue
            domain = normalize_domain(token)
            if domain not in seen:
                seen.add(domain)
                items.append(domain)
    return items


def _split_domain_text(raw: str) -> List[str]:
    """读取侧使用的宽松解析：跳过坏行，避免手改过的数据库把面板打挂。"""
    try:
        return parse_domain_text(raw)
    except ValueError:
        pass
    items: List[str] = []
    seen: Set[str] = set()
    for line in str(raw or "").splitlines():
        try:
            domain = normalize_domain(line)
        except ValueError:
            continue
        if domain not in seen:
            seen.add(domain)
            items.append(domain)
    return items


def domain_matches(domain: str, rules: List[str]) -> bool:
    """大小写不敏感、忽略末尾点：精确匹配，或规则域名是它的父域（覆盖子域）。"""
    try:
        target = (domain or "").strip().lower().rstrip(".")
    except AttributeError:
        return False
    if not target:
        return False
    for rule in rules or []:
        try:
            rule_text = normalize_domain(rule)
        except ValueError:
            continue
        if not rule_text:
            continue
        if target == rule_text:
            return True
        # IP 字面量只做精确匹配，不允许“后缀匹配”。
        try:
            ipaddress.ip_address(rule_text)
            continue
        except ValueError:
            pass
        try:
            ipaddress.ip_address(target)
            continue
        except ValueError:
            pass
        if target.endswith("." + rule_text):
            return True
    return False


def _ip_key(value: str) -> Tuple[int, int]:
    """IP 排序键：先版本后数值，保证 nft 脚本与哈希稳定。"""
    try:
        addr = ipaddress.ip_address(str(value))
        return (addr.version, int(addr))
    except ValueError:
        return (9, 0)


def _dedupe_sorted(values: List[str], version: Optional[int] = None) -> List[str]:
    out: Dict[str, None] = {}
    for value in values:
        try:
            addr = ipaddress.ip_address(str(value).split("%", 1)[0])
        except ValueError:
            continue
        if version is not None and addr.version != version:
            continue
        out[str(addr)] = None
    return sorted(out, key=_ip_key)


def _policy_row() -> Optional[sqlite3.Row]:
    try:
        with closing(get_db()) as db:
            return db.execute("SELECT * FROM egress_policy WHERE id=1").fetchone()
    except sqlite3.OperationalError:
        # 还没有建表：先补 schema 再读一次。
        ensure_egress_schema()
        with closing(get_db()) as db:
            return db.execute("SELECT * FROM egress_policy WHERE id=1").fetchone()


def _policy_summary(enabled: bool, mode: str, domains: List[str]) -> str:
    if not enabled:
        return "关闭（所有出站访问被拒绝）"
    if mode == "all":
        return "打开（全部放行）"
    return "打开（白名单，%d 个域名/地址）" % len(domains)


def get_egress_policy() -> Dict[str, Any]:
    """读取单行策略；缺行时返回默认（关闭）策略。"""
    row = _policy_row()
    if row is None:
        return {
            "enabled": False,
            "mode": "allowlist",
            "domains": [],
            "block_quic": True,
            "ipv6": True,
            "updated_at": "",
            "updated_by": "",
            "summary": _policy_summary(False, "allowlist", []),
        }
    enabled = bool(int(row["enabled"] or 0))
    mode = str(row["mode"] or "allowlist").strip().lower()
    if mode not in EGRESS_MODES:
        mode = "allowlist"
    domains = _split_domain_text(row["domains"] or "")
    return {
        "enabled": enabled,
        "mode": mode,
        "domains": domains,
        "block_quic": bool(int(row["block_quic"] or 0)),
        "ipv6": bool(int(row["ipv6"] or 0)),
        "updated_at": str(row["updated_at"] or ""),
        "updated_by": str(row["updated_by"] or ""),
        "summary": _policy_summary(enabled, mode, domains),
    }


def save_egress_policy(
    *,
    enabled: Optional[bool] = None,
    mode: Optional[str] = None,
    domains: Any = None,
    block_quic: Optional[bool] = None,
    ipv6: Optional[bool] = None,
    actor: str = "",
) -> Dict[str, Any]:
    """保存策略；只更新传入的字段。domains 接受字符串或列表。"""
    ensure_egress_schema()
    current = get_egress_policy()
    updates: Dict[str, Any] = {}
    if enabled is not None:
        updates["enabled"] = 1 if enabled else 0
    if mode is not None:
        mode_text = str(mode).strip().lower()
        if mode_text not in EGRESS_MODES:
            raise ValueError("出口模式只能是 allowlist 或 all。")
        updates["mode"] = mode_text
    if domains is not None:
        if isinstance(domains, (list, tuple, set)):
            parsed: List[str] = []
            seen: Set[str] = set()
            for item in domains:
                domain = normalize_domain(str(item))
                if domain not in seen:
                    seen.add(domain)
                    parsed.append(domain)
        else:
            parsed = parse_domain_text(str(domains))
        updates["domains"] = "\n".join(parsed)
    if block_quic is not None:
        updates["block_quic"] = 1 if block_quic else 0
    if ipv6 is not None:
        updates["ipv6"] = 1 if ipv6 else 0
    if not updates:
        return current
    updates["updated_at"] = utc_now()
    updates["updated_by"] = str(actor or current.get("updated_by") or "")
    columns = ", ".join("%s=?" % key for key in updates)
    params = list(updates.values()) + [1]
    with closing(get_db()) as db, db:
        db.execute("UPDATE egress_policy SET %s WHERE id=?" % columns, tuple(params))
        db.commit()
    return get_egress_policy()


def guard_installed() -> bool:
    """是否安装了出口守卫（契约第 6 节）。"""
    if _env_flag("GSM_EGRESS_HOST", False):
        return True
    try:
        if SERVICE_FILE.is_file():
            return True
    except OSError:
        pass
    try:
        return _runtime_dir_path().is_dir()
    except OSError:
        return False


def egress_available() -> bool:
    return bool(EGRESS_ENABLED and guard_installed())


# ---------------------------------------------------------------------------
# DNS 解析与放行地址
# ---------------------------------------------------------------------------


def _resolve_timeout() -> float:
    try:
        return max(0.5, float(os.environ.get("GSM_EGRESS_RESOLVE_TIMEOUT", "3") or 3))
    except ValueError:
        return 3.0


def resolve_host(host: str) -> List[str]:
    """解析域名为地址列表；失败或超时返回空。测试可替换本函数（契约第 6 节）。

    getaddrinfo 自身没有超时参数，解析器卡住时会一直阻塞；这里放到工作线程里
    等待有限时长（GSM_EGRESS_RESOLVE_TIMEOUT，默认 3 秒），保证面板首屏和守护
    进程的轮询不会被 DNS 拖死。超时结果同样进缓存，避免反复卡住。
    """
    host = (host or "").strip()
    if not host:
        return []
    resolved: List[str] = []

    def _work() -> None:
        try:
            infos = socket.getaddrinfo(host, None)
        except (socket.gaierror, OSError):
            return
        for info in infos:
            try:
                addr = str(ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]))
            except (IndexError, ValueError):
                continue
            if addr not in resolved:
                resolved.append(addr)

    worker = threading.Thread(target=_work, name="gsm-egress-resolve", daemon=True)
    worker.start()
    worker.join(_resolve_timeout())
    return list(resolved) if not worker.is_alive() else []


def resolve_cached(host: str, ttl: float = RESOLVE_TTL) -> List[str]:
    """带 TTL（默认 60 秒）的解析缓存；解析失败也缓存，避免打爆解析器。"""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return []
    now = time.time()
    with _RESOLVE_LOCK:
        hit = _RESOLVE_CACHE.get(host)
        if hit is not None and now - hit[0] < ttl:
            return list(hit[1])
    addrs = resolve_host(host)
    with _RESOLVE_LOCK:
        _RESOLVE_CACHE[host] = (now, tuple(addrs))
    return list(addrs)


def clear_resolve_cache() -> None:
    """清空解析缓存（测试与手工重下发用）。"""
    with _RESOLVE_LOCK:
        _RESOLVE_CACHE.clear()


def resolver_addresses() -> Tuple[List[str], List[str]]:
    """读取 resolv.conf 里的 nameserver，返回 (IPv4 列表, IPv6 列表)。"""
    path = Path(os.environ.get("GSM_EGRESS_RESOLV_CONF") or RESOLV_CONF)
    v4: List[str] = []
    v6: List[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return v4, v6
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] != "nameserver":
            continue
        try:
            addr = ipaddress.ip_address(parts[1].split("%", 1)[0])
        except ValueError:
            continue
        (v4 if addr.version == 4 else v6).append(str(addr))
    return _dedupe_sorted(v4, 4), _dedupe_sorted(v6, 6)


def resolve_allow_addresses(policy: Dict[str, Any]) -> Tuple[List[str], List[str], List[str]]:
    """把策略里的域名清单展开成 nft 放行集合，返回 (allow4, allow6, 解析失败域名)。"""
    if not policy.get("enabled") or policy.get("mode") == "all":
        return [], [], []
    allow4: List[str] = []
    allow6: List[str] = []
    unresolved: List[str] = []
    for rule in policy.get("domains") or []:
        rule_text = str(rule)
        try:
            addr = ipaddress.ip_address(rule_text)
        except ValueError:
            addrs = resolve_cached(rule_text)
            if not addrs:
                unresolved.append(rule_text)
                continue
            for item in addrs:
                try:
                    ip = ipaddress.ip_address(str(item).split("%", 1)[0])
                except ValueError:
                    continue
                (allow4 if ip.version == 4 else allow6).append(str(ip))
            continue
        (allow4 if addr.version == 4 else allow6).append(str(addr))
    return _dedupe_sorted(allow4, 4), _dedupe_sorted(allow6, 6), unresolved


# ---------------------------------------------------------------------------
# nft 脚本生成（纯函数）
# ---------------------------------------------------------------------------


def _elements_lines(indent: str, elements: List[str]) -> List[str]:
    """把集合元素按每行 6 个换行输出，避免单行过长。"""
    lines: List[str] = []
    chunk: List[str] = []
    for element in elements:
        chunk.append(element)
        if len(chunk) >= 6:
            lines.append(indent + ", ".join(chunk) + ",")
            chunk = []
    if chunk:
        text = indent + ", ".join(chunk)
        if lines:
            lines[-1] = lines[-1].rstrip(",") + ","
        lines.append(text)
    return lines


def _nft_set_block(name: str, set_type: str, elements: List[str]) -> List[str]:
    """生成集合块；关闭状态/无解析结果时集合保持为空。"""
    lines = ["\tset %s {" % name, "\t\ttype %s" % set_type]
    values = [element for element in elements if element]
    if values:
        lines.append("\t\telements = {")
        lines.extend(_elements_lines("\t\t\t", values))
        lines.append("\t\t}")
    lines.append("\t}")
    return lines


def _inet_table(policy: Dict[str, Any], allow4: List[str], dns4: List[str]) -> List[str]:
    """IPv4 表：规则顺序严格按契约第 4 节，逐条生成。"""
    enabled = bool(policy.get("enabled"))
    mode = str(policy.get("mode") or "allowlist").strip().lower()
    lines: List[str] = ["table inet gsm_egress {"]
    lines.extend(_nft_set_block("allow4", "ipv4_addr", allow4))
    lines.append("\tchain gsm_out {")
    lines.append("\t\ttype filter hook output priority 0; policy accept;")
    lines.append("\t\toifname " + DQ + "lo" + DQ + " accept")
    lines.append("\t\tip daddr { %s } accept" % ", ".join(PRIVATE4))
    lines.append("\t\tct state established,related accept")
    if enabled and bool(policy.get("block_quic", True)):
        lines.append("\t\tudp dport 443 drop")
    if not enabled:
        # 关闭：集合为空，打拒绝日志并 drop（不引用 @allow4）。
        lines.append("\t\tlog prefix " + DQ + BLOCK_PREFIX + " " + DQ + " " + LOG_LIMIT + " drop")
    elif mode == "all":
        lines.append("\t\taccept")
    else:
        if dns4:
            target = "ip daddr { %s }" % ", ".join(dns4)
            lines.append("\t\t%s udp dport 53 accept" % target)
            lines.append("\t\t%s tcp dport 53 accept" % target)
        lines.append(
            "\t\tip daddr @allow4 log prefix " + DQ + ALLOW_PREFIX + " " + DQ
            + " " + LOG_LIMIT + " accept"
        )
        lines.append("\t\tlog prefix " + DQ + BLOCK_PREFIX + " " + DQ + " " + LOG_LIMIT + " drop")
    lines.append("\t}")
    lines.append("}")
    return lines


def _ip6_table(policy: Dict[str, Any], allow6: List[str], dns6: List[str]) -> List[str]:
    """IPv6 表：与 IPv4 同构，仅 ipv6=1 时生成。"""
    enabled = bool(policy.get("enabled"))
    mode = str(policy.get("mode") or "allowlist").strip().lower()
    lines: List[str] = ["table ip6 gsm_egress {"]
    lines.extend(_nft_set_block("allow6", "ipv6_addr", allow6))
    lines.append("\tchain gsm_out6 {")
    lines.append("\t\ttype filter hook output priority 0; policy accept;")
    lines.append("\t\toifname " + DQ + "lo" + DQ + " accept")
    lines.append("\t\tip6 daddr { %s } accept" % ", ".join(PRIVATE6))
    lines.append("\t\tct state established,related accept")
    if enabled and bool(policy.get("block_quic", True)):
        lines.append("\t\tudp dport 443 drop")
    if not enabled:
        lines.append("\t\tlog prefix " + DQ + BLOCK_PREFIX + " " + DQ + " " + LOG_LIMIT + " drop")
    elif mode == "all":
        lines.append("\t\taccept")
    else:
        if dns6:
            target = "ip6 daddr { %s }" % ", ".join(dns6)
            lines.append("\t\t%s udp dport 53 accept" % target)
            lines.append("\t\t%s tcp dport 53 accept" % target)
        lines.append(
            "\t\tip6 daddr @allow6 log prefix " + DQ + ALLOW_PREFIX + " " + DQ
            + " " + LOG_LIMIT + " accept"
        )
        lines.append("\t\tlog prefix " + DQ + BLOCK_PREFIX + " " + DQ + " " + LOG_LIMIT + " drop")
    lines.append("\t}")
    lines.append("}")
    return lines


def nft_script(
    policy: Dict[str, Any],
    allow4: List[str],
    allow6: List[str],
    dns4: List[str],
    dns6: List[str],
) -> str:
    """生成完整 nft -f 脚本（纯函数，便于单元测试）。

    - inet gsm_egress 永远生成；ipv6=1 时再生成 ip6 gsm_egress，ipv6=0 时不生成
      （删除旧 ip6 表的逻辑由 apply 负责，见 _install_guard）；
    - 关闭时集合保持为空、以 drop 结束且完全不引用 @allow4；
    - 打开且 allowlist 时：先放行 resolv.conf 里的 DNS（UDP/TCP 53），再按 allow
      集合打日志放行（limit rate 12/minute burst 6），未命中打日志并 drop；
    - 打开且 all 时直接 accept（不打放行日志）。
    """
    enabled = bool(policy.get("enabled"))
    values4 = _dedupe_sorted([str(item) for item in (allow4 or []) if str(item or "").strip()], 4) if enabled else []
    values6 = _dedupe_sorted([str(item) for item in (allow6 or []) if str(item or "").strip()], 6) if enabled else []
    dns4_values = _dedupe_sorted([str(item) for item in (dns4 or [])], 4)
    dns6_values = _dedupe_sorted([str(item) for item in (dns6 or [])], 6)

    lines: List[str] = [
        "# 由 gsm-egress 生成，重新下发会整体替换，请勿手工修改。",
    ]
    lines.extend(_inet_table(policy, values4, dns4_values))
    if policy.get("ipv6", True):
        lines.append("")
        lines.extend(_ip6_table(policy, values6, dns6_values))
    return "\n".join(lines) + "\n"


def _deny_policy() -> Dict[str, Any]:
    """回退用的全拒绝策略：同时守护 IPv6，宁可多拦不可漏放。"""
    return {"enabled": False, "mode": "allowlist", "domains": [], "block_quic": False, "ipv6": True}


def _policy_fingerprint(
    policy: Dict[str, Any],
    allow4: List[str],
    allow6: List[str],
    dns4: List[str],
    dns6: List[str],
) -> str:
    """数据面指纹：策略开关 + 解析后的集合 + DNS 目标。

    包含域名解析结果，所以 DNS 记录变化也会触发重下发（契约第 4 节）。
    """
    payload = {
        "enabled": bool(policy.get("enabled")),
        "mode": str(policy.get("mode") or "allowlist"),
        "block_quic": bool(policy.get("block_quic", True)),
        "ipv6": bool(policy.get("ipv6", True)),
        "allow4": _dedupe_sorted([str(item) for item in allow4 or []], 4),
        "allow6": _dedupe_sorted([str(item) for item in allow6 or []], 6),
        "dns4": _dedupe_sorted([str(item) for item in dns4 or []], 4),
        "dns6": _dedupe_sorted([str(item) for item in dns6 or []], 6),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 下发（nft 执行、校验、回退、状态文件）
# ---------------------------------------------------------------------------


NFT_MISSING = "nft 不可用：未找到 nft 命令（请安装 nftables 并以 root 运行）"
NFT_TIMEOUT = "nft 不可用：执行 nft 超时（内核或 nftables 无响应）"


def _nft_binary() -> str:
    """nft 可执行文件路径；GSM_EGRESS_NFT 可覆盖（测试/非标准路径）。"""
    return os.environ.get("GSM_EGRESS_NFT") or shutil.which("nft") or ""


def run_nft(script: str) -> Tuple[int, str, str]:
    """执行一条 nft 脚本（走 stdin），返回 (rc, stdout, stderr)。

    约定（契约第 6 节）：所有 nft 调用都集中到这里，测试直接替换本函数。
    "list tables" / "list table ..." 这类查询脚本也走同一入口。
    失败信息统一成人话（不暴露 WinError/ENOENT 之类的原生文本），
    因为它们会直接显示在面板和 gsm 的输出里。
    """
    nft = _nft_binary()
    if not nft:
        return 1, "", NFT_MISSING
    try:
        completed = subprocess.run(
            [nft, "-f", "-"],
            input=script,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return 1, "", NFT_TIMEOUT
    except OSError:
        # 命令不存在/无执行权限/被沙箱拒绝：统一成契约措辞。
        return 1, "", NFT_MISSING
    except subprocess.SubprocessError:
        return 1, "", NFT_TIMEOUT
    return completed.returncode, completed.stdout, completed.stderr


def _nft_backend() -> str:
    """数据面后端：找得到 nft 才是 "nft"，否则 "none"（面板/gsm 据此提示）。"""
    return "nft" if _nft_binary() else "none"


def _existing_tables() -> Set[str]:
    """枚举当前 nft 表；无法枚举时返回空集（下发后的校验仍会兜底）。"""
    rc, out, _err = run_nft("list tables\n")
    if rc != 0:
        return set()
    tables: Set[str] = set()
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("table "):
            tables.add(line[len("table "):].strip())
    return tables


def _delete_prefix(tables: Set[str]) -> str:
    return "".join("delete table %s\n" % table for table in sorted(tables))


def _install_guard(body: str, existing: Set[str], ipv6_enabled: bool) -> Tuple[bool, str, str]:
    """下发规则体，返回 (ok, 实际脚本, 错误信息)。

    优先在同一个 nft -f 事务里"删旧表 + 建新表"：表存在时脚本以 delete table 开头，
    失败整体回滚，不留空窗。事务失败时退化为逐表删除后单独建表，此时调用方必须
    紧接着装拒绝形态（见 egress_apply_now），避免留下"表不存在"的放行窗口。
    """
    to_delete = set(existing)
    if not ipv6_enabled:
        # 关闭 IPv6 守护时必须清掉旧的 ip6 表（契约第 4 节）。
        to_delete.add("ip6 gsm_egress")
    script = _delete_prefix(to_delete) + body
    rc, out, err = run_nft(script)
    if rc == 0:
        return True, script, ""
    for table in sorted(to_delete):
        run_nft("delete table %s\n" % table)
    rc2, out2, err2 = run_nft(body)
    if rc2 == 0:
        return True, body, ""
    message = (err or err2 or out2 or out or "").strip()
    return False, script, message


def _expects_drop(policy: Dict[str, Any]) -> bool:
    """all 模式且不丢 QUIC 时链里本来就没有 drop，校验要按预期判断。"""
    enabled = bool(policy.get("enabled"))
    if enabled and policy.get("mode") == "all":
        return bool(policy.get("block_quic", True))
    return True


def _verify(policy: Dict[str, Any]) -> Tuple[bool, str]:
    """下发后校验：表能列出、链规则完整（契约第 4 节"下发后必须校验"）。"""
    expect_drop = _expects_drop(policy)
    rc, out, err = run_nft("list table inet gsm_egress\n")
    if rc != 0:
        return False, "校验失败：inet gsm_egress 表不存在或无法读取（%s）" % ((err or out or "").strip())
    if expect_drop:
        if "drop" not in out:
            return False, "校验失败：inet gsm_egress 中找不到 drop 规则"
    elif "chain gsm_out" not in out or "accept" not in out:
        return False, "校验失败：inet gsm_egress 链规则不完整"
    if policy.get("ipv6", True):
        rc6, out6, err6 = run_nft("list table ip6 gsm_egress\n")
        if rc6 != 0:
            return False, "校验失败：ip6 gsm_egress 表不存在或无法读取（%s）" % ((err6 or out6 or "").strip())
        if expect_drop and "drop" not in out6:
            return False, "校验失败：ip6 gsm_egress 中找不到 drop 规则"
    return True, ""


def _state_file() -> Path:
    return egress_runtime_dir() / STATE_FILE


def _read_state() -> Dict[str, Any]:
    try:
        raw = _state_file().read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state: Dict[str, Any]) -> None:
    """原子写 state.json（面板与 CLI 会同时读）。"""
    path = _state_file()
    directory = path.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=path.name + ".", dir=str(directory))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=1, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_path, path)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except OSError as exc:
        print("gsm-egress: 写入状态文件失败：%s" % exc, file=sys.stderr)


@contextmanager
def _apply_lock() -> Iterator[None]:
    """串行化下发：进程内锁 + apply.lock 上的 fcntl 排它锁（面板与守护进程都会下发）。"""
    with _APPLY_THREAD_LOCK:
        handle = None
        try:
            handle = open(str(egress_runtime_dir() / LOCK_FILE), "a+")
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError:
            handle = None
        try:
            yield
        finally:
            if handle is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    handle.close()
                except OSError:
                    pass


def _default_state() -> Dict[str, Any]:
    return {
        "ok": False,
        "detail": "尚未下发",
        "backend": _nft_backend(),
        "last_at": None,
        "allow4_count": 0,
        "allow6_count": 0,
        "policy_hash": "",
        "applied_hash": "",
    }


def _success_detail(
    policy: Dict[str, Any],
    allow4: List[str],
    allow6: List[str],
    dns4: List[str],
    dns6: List[str],
    unresolved: List[str],
) -> str:
    enabled = bool(policy.get("enabled"))
    mode = str(policy.get("mode") or "allowlist")
    if not enabled:
        detail = "出口已关闭：所有出站访问被拒绝（DNS 也拒绝）"
    elif mode == "all":
        detail = "出口已打开：全部放行"
        if policy.get("block_quic", True):
            detail += "（UDP/443 已丢弃）"
    else:
        detail = "出口已打开：白名单放行（IPv4 %d 个 / IPv6 %d 个地址）" % (len(allow4), len(allow6))
        if not (dns4 or dns6):
            detail += "；未发现 DNS 解析器，域名可能无法解析"
        if unresolved:
            detail += "；%d 个域名解析失败" % len(unresolved)
    if not policy.get("ipv6", True):
        detail += "；IPv6 未守护（已删除 ip6 表）"
    return detail


def egress_apply_now(actor: str = "", *, detail_suffix: str = "") -> Dict[str, Any]:
    """同步下发当前策略，返回 egress_apply_state()。任何失败都回落到全拒绝形态。

    detail_suffix 是给守护进程用的补充说明（例如"流水不可用"），会和下发结果
    一起原子写进 state.json，避免面板/CLI 读到只写了一半的状态。
    """
    at = utc_now()
    backend = _nft_backend()

    def with_suffix(text: str) -> str:
        if detail_suffix and detail_suffix not in text:
            return (text + "；" + detail_suffix) if text else detail_suffix
        return text
    try:
        ensure_egress_schema()
        policy = get_egress_policy()
    except Exception as exc:
        # 数据库坏了也必须记录失败状态，而不是把异常抛给 Web 请求。
        state = _default_state()
        state.update({
            "detail": with_suffix("读取出口策略失败：%s" % exc),
            "backend": backend,
            "last_at": at,
        })
        _write_state(state)
        return egress_apply_state()

    dns4, dns6 = resolver_addresses()
    allow4, allow6, unresolved = resolve_allow_addresses(policy)
    desired_hash = _policy_fingerprint(policy, allow4, allow6, dns4, dns6)
    ipv6_enabled = bool(policy.get("ipv6", True))

    with _apply_lock():
        ok, _script, error = _install_guard(
            nft_script(policy, allow4, allow6, dns4, dns6), _existing_tables(), ipv6_enabled
        )
        if ok:
            ok, verify_detail = _verify(policy)
            if not ok:
                error = verify_detail
        if ok:
            detail = _success_detail(policy, allow4, allow6, dns4, dns6, unresolved)
            applied_hash = desired_hash
        else:
            # 回落到全拒绝形态（契约第 4 节"失败安全"）。
            deny_policy = _deny_policy()
            deny_ok, _deny_script, deny_error = _install_guard(
                nft_script(deny_policy, [], [], dns4, dns6), _existing_tables(), True
            )
            deny_detail = ""
            if deny_ok:
                deny_ok, deny_detail = _verify(deny_policy)
            if deny_ok:
                detail = "下发失败：%s；已回退到全拒绝形态" % (error or "未知错误")
            else:
                detail = "下发失败：%s；回退到全拒绝也失败：%s" % (
                    error or "未知错误",
                    deny_error or deny_detail or "未知错误",
                )
            applied_hash = _policy_fingerprint(deny_policy, [], [], dns4, dns6)
            allow4 = []
            allow6 = []
            ok = False
        state = {
            "ok": ok,
            "detail": with_suffix(detail),
            "backend": backend,
            "last_at": at,
            "allow4_count": len(allow4),
            "allow6_count": len(allow6),
            "policy_hash": desired_hash,
            "applied_hash": applied_hash,
        }
        if actor:
            state["actor"] = actor
        _write_state(state)
    return egress_apply_state()


def egress_apply_state() -> Dict[str, Any]:
    """读取最近一次下发结果，并与当前策略指纹比较得到 needs_apply。"""
    stored = _read_state()
    desired = ""
    try:
        policy = get_egress_policy()
        allow4, allow6, _unresolved = resolve_allow_addresses(policy)
        dns4, dns6 = resolver_addresses()
        desired = _policy_fingerprint(policy, allow4, allow6, dns4, dns6)
    except Exception:
        desired = ""
    applied = str(stored.get("applied_hash") or "")
    if stored:
        needs_apply = bool(desired) and desired != applied
    else:
        needs_apply = True
    return {
        "ok": bool(stored.get("ok", False)),
        "detail": str(stored.get("detail") or "尚未下发"),
        "backend": str(stored.get("backend") or _nft_backend()),
        "last_at": stored.get("last_at"),
        "allow4_count": int(stored.get("allow4_count") or 0),
        "allow6_count": int(stored.get("allow6_count") or 0),
        "needs_apply": needs_apply,
        "policy_hash": desired or str(stored.get("policy_hash") or ""),
        "applied_hash": applied,
    }


def egress_summary() -> Dict[str, Any]:
    """面板与 CLI 共用的摘要（契约第 6 节字段一个都不能少）。"""
    try:
        policy = get_egress_policy()
    except Exception:
        policy = {
            "enabled": False,
            "mode": "allowlist",
            "domains": [],
            "block_quic": True,
            "ipv6": True,
            "updated_at": "",
            "updated_by": "",
        }
    state = egress_apply_state()
    dns4, dns6 = resolver_addresses()
    mode = str(policy.get("mode") or "allowlist")
    installed = guard_installed()
    return {
        "enabled": bool(policy.get("enabled")),
        "mode": mode,
        "mode_label": "全部放行" if mode == "all" else "仅白名单",
        "domains": len(policy.get("domains") or []),
        "backend": state.get("backend") or "none",
        "ok": bool(state.get("ok")),
        "detail": state.get("detail") or "",
        "updated_at": str(policy.get("updated_at") or ""),
        "updated_by": str(policy.get("updated_by") or ""),
        "available": egress_available(),
        "installed": installed,
        "block_quic": bool(policy.get("block_quic")),
        "ipv6": bool(policy.get("ipv6")),
        "domain_list": list(policy.get("domains") or []),
        "dns_servers": list(dns4) + list(dns6),
        "allow4_count": int(state.get("allow4_count") or 0),
        "allow6_count": int(state.get("allow6_count") or 0),
    }


# ---------------------------------------------------------------------------
# 访问流水
# ---------------------------------------------------------------------------


def _log_max() -> int:
    try:
        return max(1, int(os.environ.get("GSM_EGRESS_LOG_MAX", "2000") or 2000))
    except ValueError:
        return 2000


def trim_egress_log(keep: Optional[int] = None) -> None:
    """只保留最新 keep 行（默认 GSM_EGRESS_LOG_MAX，2000）。"""
    limit = _log_max() if keep is None else max(1, int(keep))
    try:
        with closing(get_db()) as db, db:
            db.execute(
                "DELETE FROM egress_log WHERE id NOT IN "
                "(SELECT id FROM egress_log ORDER BY id DESC LIMIT ?)",
                (limit,),
            )
            db.commit()
    except sqlite3.Error as exc:
        print("gsm-egress: 裁剪访问流水失败：%s" % exc, file=sys.stderr)


def egress_log_append(at: str, domain: str, ip: str, port: Any, action: str, reason: str) -> None:
    """追加一条访问流水；写库失败只记日志，绝不让守护进程退出。"""
    global _LOG_INSERT_COUNT
    try:
        with closing(get_db()) as db, db:
            db.execute(
                "INSERT INTO egress_log (at, domain, ip, port, action, reason) VALUES (?,?,?,?,?,?)",
                (
                    str(at or utc_now()),
                    str(domain or ""),
                    str(ip or ""),
                    int(port or 0),
                    str(action or ""),
                    str(reason or ""),
                ),
            )
            db.commit()
    except (sqlite3.Error, ValueError, TypeError) as exc:
        print("gsm-egress: 写入访问流水失败：%s" % exc, file=sys.stderr)
        return
    with _LOG_LOCK:
        _LOG_INSERT_COUNT += 1
        should_trim = _LOG_INSERT_COUNT >= 50
        if should_trim:
            _LOG_INSERT_COUNT = 0
    if should_trim:
        trim_egress_log()


def egress_log_tail(limit: int = 50, action: str = "") -> List[Dict[str, Any]]:
    """最近 limit 条流水（新的在前）；action 取 allow/block 时过滤（契约第 6 节）。"""
    try:
        limit_value = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit_value = 50
    sql = "SELECT at, domain, ip, port, action, reason FROM egress_log"
    params: List[Any] = []
    if action in ("allow", "block"):
        sql += " WHERE action=?"
        params.append(action)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit_value)
    try:
        with closing(get_db()) as db:
            rows = db.execute(sql, tuple(params)).fetchall()
    except sqlite3.Error:
        return []
    return [
        {
            "at": str(row["at"] or ""),
            "domain": str(row["domain"] or ""),
            "ip": str(row["ip"] or ""),
            "port": int(row["port"] or 0),
            "action": str(row["action"] or ""),
            "reason": str(row["reason"] or ""),
        }
        for row in rows
    ]


def egress_log_stats() -> Dict[str, Any]:
    """流水统计：{"total","allow","block","last"}（契约第 6 节）。"""
    try:
        with closing(get_db()) as db:
            row = db.execute(
                "SELECT COUNT(*) AS total,"
                " COALESCE(SUM(action='allow'),0) AS allow_count,"
                " COALESCE(SUM(action='block'),0) AS block_count,"
                " MAX(at) AS last FROM egress_log"
            ).fetchone()
    except sqlite3.Error:
        return {"total": 0, "allow": 0, "block": 0, "last": None}
    if row is None:
        return {"total": 0, "allow": 0, "block": 0, "last": None}
    return {
        "total": int(row["total"] or 0),
        "allow": int(row["allow_count"] or 0),
        "block": int(row["block_count"] or 0),
        "last": row["last"] or None,
    }


def parse_kernel_log_line(line: str) -> Optional[Dict[str, Any]]:
    """解析一条 nft 日志：返回 {action, ip, port, proto}；不是本模块的日志返回 None。"""
    action = ""
    index = line.find(BLOCK_PREFIX)
    if index >= 0:
        action = "block"
    else:
        index = line.find(ALLOW_PREFIX)
        if index < 0:
            return None
        action = "allow"
    tail = line[index:]
    dst = _DST_RE.search(tail)
    if not dst:
        return None
    dpt = _DPT_RE.search(tail)
    proto = _PROTO_RE.search(tail)
    return {
        "action": action,
        "ip": dst.group(1).strip(),
        "port": int(dpt.group(1)) if dpt else 0,
        "proto": proto.group(1).lower() if proto else "",
    }


def _domain_for_ip(ip: str) -> str:
    """按当前解析缓存反查域名；查不到返回空串。"""
    try:
        policy = get_egress_policy()
    except Exception:
        return ""
    for rule in policy.get("domains") or []:
        rule_text = str(rule)
        try:
            ipaddress.ip_address(rule_text)
            continue  # IP 字面量本身就是地址，没有域名可反查
        except ValueError:
            pass
        if ip in resolve_cached(rule_text):
            return rule_text
    return ""


def _consume_kernel_line(line: str) -> bool:
    """把一条内核日志落成流水；返回是否命中本模块的日志。"""
    parsed = parse_kernel_log_line(line)
    if not parsed:
        return False
    try:
        policy = get_egress_policy()
    except Exception:
        policy = {"enabled": False, "mode": "allowlist"}
    domain = ""
    if parsed["action"] == "allow":
        domain = _domain_for_ip(parsed["ip"])
        reason = "allowlist"
    else:
        reason = "egress-disabled" if not policy.get("enabled") else "not-in-allowlist"
    egress_log_append(utc_now(), domain, parsed["ip"], parsed["port"], parsed["action"], reason)
    return True


def _kernel_log_lines(stop_event: threading.Event) -> Iterator[str]:
    """读内核日志：优先 /dev/kmsg（非阻塞 + select），退回 journalctl -k -f -o cat。"""
    kmsg = Path(os.environ.get("GSM_EGRESS_KMSG") or "/dev/kmsg")
    try:
        if kmsg.is_file():
            with open(str(kmsg), "r", encoding="utf-8", errors="replace") as handle:
                try:
                    os.set_blocking(handle.fileno(), False)
                except (OSError, AttributeError):
                    pass
                while not stop_event.is_set():
                    try:
                        ready, _, _ = select.select([handle], [], [], 1.0)
                    except (OSError, ValueError):
                        time.sleep(0.5)
                        continue
                    if not ready:
                        continue
                    chunk = handle.read()
                    if not chunk:
                        time.sleep(0.2)
                        continue
                    for line in chunk.splitlines():
                        yield line
            return
    except OSError:
        pass
    command = ["journalctl", "-k", "-f", "-o", "cat", "--no-pager"]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except (OSError, ValueError):
        return
    try:
        stream = process.stdout
        if stream is None:
            return
        for line in stream:
            if stop_event.is_set():
                break
            yield line.rstrip("\n")
    finally:
        try:
            process.terminate()
        except OSError:
            pass


def _log_loop(stop_event: threading.Event) -> None:
    """流水线程：任何异常都不允许把守护进程带走（契约第 5 节）。"""
    try:
        for line in _kernel_log_lines(stop_event):
            if stop_event.is_set():
                break
            try:
                _consume_kernel_line(line)
            except Exception as exc:
                print("gsm-egress: 处理内核日志失败：%s" % exc, file=sys.stderr)
    except Exception as exc:
        print("gsm-egress: 访问流水线程退出：%s" % exc, file=sys.stderr)


def _kernel_log_available() -> bool:
    try:
        if Path(os.environ.get("GSM_EGRESS_KMSG") or "/dev/kmsg").is_file():
            return True
    except OSError:
        pass
    return bool(shutil.which("journalctl"))


# ---------------------------------------------------------------------------
# 守护进程
# ---------------------------------------------------------------------------


def egress_run() -> int:
    """守护进程入口（systemd 执行 python egress.py run）。

    启动即下发一次（默认关闭 → 全拒绝）；此后每 2 秒比对策略指纹（含域名解析
    结果，TTL 60 秒）变化则重下发；每 30 秒无条件校验表与链规则，缺失则重下发。
    判定逻辑全在这里，因此面板/CLI 都不在场时数据面依然拒绝一切出站。
    """
    stop_event = threading.Event()

    def _handle_signal(_signum, _frame) -> None:
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass

    ensure_egress_schema()
    trim_egress_log()
    log_available = _kernel_log_available()
    if log_available:
        threading.Thread(target=_log_loop, args=(stop_event,), daemon=True).start()

    try:
        poll_interval = float(os.environ.get("GSM_EGRESS_POLL_INTERVAL", "2") or 2)
    except ValueError:
        poll_interval = 2.0
    try:
        heal_interval = float(os.environ.get("GSM_EGRESS_HEAL_INTERVAL", "30") or 30)
    except ValueError:
        heal_interval = 30.0
    # 测试钩子：大于 0 时跑够这么多秒自动退出（Windows 上发 SIGTERM 会直接杀进程）。
    try:
        run_seconds = float(os.environ.get("GSM_EGRESS_RUN_SECONDS", "0") or 0)
    except ValueError:
        run_seconds = 0.0
    deadline = time.monotonic() + run_seconds if run_seconds > 0 else None

    # 流水不可用不影响拦截功能，只在下发结果里标注（契约第 5 节）。
    state = egress_apply_now(
        actor="daemon",
        detail_suffix="" if log_available else "流水不可用：既没有 /dev/kmsg 也没有 journalctl",
    )
    print("gsm-egress: 已启动，ok=%s，%s" % (state.get("ok"), state.get("detail")), flush=True)
    last_heal = time.monotonic()
    while not stop_event.is_set():
        if deadline is not None and time.monotonic() >= deadline:
            break
        stop_event.wait(poll_interval)
        if stop_event.is_set():
            break
        if deadline is not None and time.monotonic() >= deadline:
            break
        try:
            if egress_apply_state().get("needs_apply"):
                egress_apply_now(actor="daemon")
        except Exception as exc:
            print("gsm-egress: 轮询策略失败：%s" % exc, file=sys.stderr)
        if time.monotonic() - last_heal >= heal_interval:
            last_heal = time.monotonic()
            try:
                ok, detail = _verify(get_egress_policy())
                if not ok:
                    print("gsm-egress: 自愈校验失败（%s），准备重下发" % detail, flush=True)
                    egress_apply_now(actor="daemon-heal")
            except Exception as exc:
                print("gsm-egress: 自愈失败：%s" % exc, file=sys.stderr)
    print("gsm-egress: 已退出", flush=True)
    return 0


# ---------------------------------------------------------------------------
# 探测、诊断、命令行
# ---------------------------------------------------------------------------


def tcp_probe(host: str, port: int = 443, timeout: float = 4.0) -> Dict[str, Any]:
    """TCP 连通性探测；不抛异常，失败信息放在 detail。"""
    host_text = (host or "").strip()
    try:
        port_value = int(port)
    except (TypeError, ValueError):
        return {"host": host_text, "port": 0, "ok": False, "detail": "参数错误：端口必须是数字", "elapsed_ms": 0}
    if not host_text:
        return {"host": host_text, "port": port_value, "ok": False, "detail": "参数错误：探测目标不能为空", "elapsed_ms": 0}
    if not (1 <= port_value <= 65535):
        return {"host": host_text, "port": port_value, "ok": False, "detail": "参数错误：端口范围必须是 1-65535", "elapsed_ms": 0}
    started = time.monotonic()
    sock = None
    ok = False
    detail = ""
    try:
        sock = socket.create_connection((host_text, port_value), timeout=max(0.2, float(timeout)))
        ok = True
        detail = "TCP 连接成功"
    except (OSError, ValueError) as exc:
        detail = "连接失败：%s" % exc
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    return {
        "host": host_text,
        "port": port_value,
        "ok": ok,
        "detail": detail,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def egress_probe(host: str = "1.1.1.1", port: int = 443, timeout: float = 4.0) -> Dict[str, Any]:
    """探测出口连通性，并结合当前策略解释结果（契约第 6 节）。"""
    result = tcp_probe(host, port, timeout)
    try:
        policy = get_egress_policy()
    except Exception:
        policy = {"enabled": False, "mode": "allowlist"}
    enabled = bool(policy.get("enabled"))
    mode = str(policy.get("mode") or "allowlist")
    if result["ok"]:
        if not enabled:
            result["detail"] = "TCP 连接成功；但出口处于关闭状态，说明数据面可能没生效，请执行 doctor 检查"
        elif mode == "allowlist":
            result["detail"] = "TCP 连接成功（白名单已放行该目标）"
        else:
            result["detail"] = "TCP 连接成功（全部放行模式）"
    else:
        if not enabled:
            result["detail"] = "连接失败：出口处于关闭状态，被拒绝属预期结果"
        elif mode == "allowlist":
            result["detail"] = "连接失败：目标不在白名单或目标不可达（%s）" % result["detail"]
        else:
            result["detail"] = "连接失败：全部放行模式下仍不可达，请检查目标或本机出网（%s）" % result["detail"]
    return result


def egress_diag_text() -> str:
    """便携命令使用的纯文本摘要（契约第 6 节）。"""
    summary = egress_summary()
    state = egress_apply_state()
    stats = egress_log_stats()
    lines = [
        "出口访问：%s" % ("已打开" if summary["enabled"] else "已关闭"),
        "模式：%s" % summary["mode_label"],
        "白名单：%d 个域名/地址" % summary["domains"],
        "数据面：%s（%s）" % ("正常" if state["ok"] else "异常", state["detail"]),
        "放行地址：IPv4 %d 个 / IPv6 %d 个" % (state["allow4_count"], state["allow6_count"]),
        "最近下发：%s" % (state["last_at"] or "从未"),
        "DNS 解析器：%s" % ("、".join(summary["dns_servers"]) or "未发现"),
        "流水：共 %d 条（放行 %d / 拦截 %d）" % (stats["total"], stats["allow"], stats["block"]),
        "运行环境：available=%s installed=%s" % (summary["available"], summary["installed"]),
        "说明：流水是内核日志采样记录（nft limit 限速），不是全量审计。",
    ]
    if state["needs_apply"]:
        lines.append("提示：策略有变更尚未下发，请执行 apply。")
    return "\n".join(lines)


def egress_doctor() -> Tuple[int, str]:
    """自检：返回 (退出码 0/1, 文本)。"""
    lines: List[str] = ["出口守卫自检："]
    counters = {"error": 0, "warn": 0}

    def report(ok: bool, message: str, warn: bool = False) -> None:
        if ok:
            lines.append("  [OK]   %s" % message)
        elif warn:
            counters["warn"] += 1
            lines.append("  [WARN] %s" % message)
        else:
            counters["error"] += 1
            lines.append("  [ERR]  %s" % message)

    report(EGRESS_ENABLED, "GSM_EGRESS 已启用（本机被当作出口机）", warn=True)
    report(guard_installed(), "守卫已安装（systemd 服务或运行时目录存在）", warn=True)
    backend = _nft_backend()
    report(backend == "nft", "nft 命令可用（%s）" % (_nft_binary() or "未找到"))
    try:
        policy = get_egress_policy()
        report(True, "策略库可读：%s" % policy["summary"])
    except Exception as exc:
        policy = {"enabled": False, "mode": "allowlist", "ipv6": True}
        report(False, "策略库不可读：%s" % exc)
    rc, _out, err = run_nft("list table inet gsm_egress\n")
    report(rc == 0, "inet gsm_egress 表存在（%s）" % ((err or "正常").strip()))
    try:
        state = egress_apply_state()
        report(not state["needs_apply"], "策略与数据面一致（applied=%s）" % (state["applied_hash"] or "无"), warn=True)
    except Exception as exc:
        report(False, "读取下发状态失败：%s" % exc)
    dns4, dns6 = resolver_addresses()
    report(bool(dns4 or dns6), "解析器可用：%s" % ("、".join(dns4 + dns6) or "未发现"), warn=True)
    report(_kernel_log_available(), "内核日志可读（/dev/kmsg 或 journalctl）", warn=True)
    stats = egress_log_stats()
    lines.append("  [INFO] 访问流水：共 %d 条（放行 %d / 拦截 %d）" % (stats["total"], stats["allow"], stats["block"]))
    lines.append("")
    if counters["error"]:
        lines.append("结论：发现 %d 个错误、%d 个警告。" % (counters["error"], counters["warn"]))
        return 1, "\n".join(lines)
    lines.append("结论：未发现错误（%d 个警告）。" % counters["warn"])
    return 0, "\n".join(lines)


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True))


def _cli_set(args: argparse.Namespace) -> int:
    """set 子命令：未安装守卫一律拒绝（退出码 1），保存成功但下发失败返回 3。"""
    if args.enable and args.disable:
        print("参数错误：--enable 与 --disable 不能同时使用。", file=sys.stderr)
        return 1
    if args.block_quic and args.no_block_quic:
        print("参数错误：--block-quic 与 --no-block-quic 不能同时使用。", file=sys.stderr)
        return 1
    if args.ipv6 and args.no_ipv6:
        print("参数错误：--ipv6 与 --no-ipv6 不能同时使用。", file=sys.stderr)
        return 1
    kwargs: Dict[str, Any] = {}
    if args.enable:
        kwargs["enabled"] = True
    if args.disable:
        kwargs["enabled"] = False
    if args.mode:
        kwargs["mode"] = args.mode
    if args.domains_file:
        try:
            text = Path(args.domains_file).read_text(encoding="utf-8")
        except OSError as exc:
            print("参数错误：读取域名文件失败（%s）。" % exc, file=sys.stderr)
            return 1
        kwargs["domains"] = text
    if args.domains is not None:
        kwargs["domains"] = args.domains
    if args.block_quic:
        kwargs["block_quic"] = True
    if args.no_block_quic:
        kwargs["block_quic"] = False
    if args.ipv6:
        kwargs["ipv6"] = True
    if args.no_ipv6:
        kwargs["ipv6"] = False
    if not egress_available():
        print("本机未启用出口守卫（GSM_EGRESS=0 或未安装 gsm-egress.service），拒绝修改策略。", file=sys.stderr)
        return 1
    try:
        policy = save_egress_policy(**kwargs, actor=args.actor)
    except ValueError as exc:
        print("参数错误：%s" % exc, file=sys.stderr)
        return 1
    state = egress_apply_now(actor=args.actor)
    if args.json:
        _print_json({"ok": state["ok"], "policy": policy, "state": state})
    else:
        print("策略已保存：%s" % policy["summary"])
        print("数据面：%s（%s）" % ("正常" if state["ok"] else "异常", state["detail"]))
    return 0 if state["ok"] else 3


def egress_cli(argv: List[str]) -> int:
    """命令行入口：run/apply/show/set/log/probe/doctor/domains（契约第 6 节）。

    退出码：0 成功；1 参数/校验错误或本机未启用守卫；3 保存成功但数据面下发失败。
    app.py 的 egress-* 子命令只做参数转发，全部委托到这里。
    """
    parser = argparse.ArgumentParser(prog="gsm-egress", description="GSM 出口访问控制")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="常驻守护进程（systemd 调用）")

    domains_parser = sub.add_parser("domains", help="列出当前放行域名（每行一个，供 gsm allow/deny 合并）")
    domains_parser.add_argument("--json", action="store_true")

    apply_parser = sub.add_parser("apply", help="立即下发当前策略")
    apply_parser.add_argument("--actor", default="cli")
    apply_parser.add_argument("--json", action="store_true")

    show_parser = sub.add_parser("show", help="查看当前策略与数据面状态")
    show_parser.add_argument("--json", action="store_true")

    set_parser = sub.add_parser("set", help="修改策略并立即下发")
    set_parser.add_argument("--enable", action="store_true", help="打开出口")
    set_parser.add_argument("--disable", action="store_true", help="关闭出口")
    set_parser.add_argument("--mode", choices=list(EGRESS_MODES), default=None)
    set_parser.add_argument("--domains", default=None, help="逗号分隔的域名/IP")
    set_parser.add_argument("--domains-file", default=None, help="每行一个域名的文件")
    set_parser.add_argument("--block-quic", action="store_true", default=False)
    set_parser.add_argument("--no-block-quic", action="store_true", default=False)
    set_parser.add_argument("--ipv6", action="store_true", default=False)
    set_parser.add_argument("--no-ipv6", action="store_true", default=False)
    set_parser.add_argument("--actor", default="cli")
    set_parser.add_argument("--json", action="store_true")

    log_parser = sub.add_parser("log", help="查看访问流水")
    log_parser.add_argument("--limit", type=int, default=30)
    log_parser.add_argument("--action", choices=["allow", "block", ""], default="")
    log_parser.add_argument("--json", action="store_true")

    probe_parser = sub.add_parser("probe", help="探测出口 TCP 连通性")
    probe_parser.add_argument("--host", default="1.1.1.1")
    probe_parser.add_argument("--port", type=int, default=443)
    probe_parser.add_argument("--timeout", type=float, default=4.0)
    probe_parser.add_argument("--json", action="store_true")

    doctor_parser = sub.add_parser("doctor", help="自检")
    doctor_parser.add_argument("--json", action="store_true")

    try:
        args = parser.parse_args(list(argv or []))
    except SystemExit:
        # argparse 的参数错误退出码是 2，契约要求统一成 1。
        return 1

    command = args.command
    if command == "run":
        return egress_run()
    if command == "show":
        if args.json:
            _print_json({"summary": egress_summary(), "state": egress_apply_state()})
        else:
            print(egress_diag_text())
        return 0
    if command == "domains":
        if not egress_available():
            print("本机未启用出口守卫（GSM_EGRESS=0 或未安装 gsm-egress.service），拒绝读取出口域名。", file=sys.stderr)
            return 1
        domains = list(egress_summary().get("domain_list") or [])
        if args.json:
            _print_json({"domains": domains})
        else:
            for item in domains:
                print(item)
        return 0
    if command == "apply":
        if not egress_available():
            print("本机未启用出口守卫（GSM_EGRESS=0 或未安装 gsm-egress.service），拒绝下发。", file=sys.stderr)
            return 1
        state = egress_apply_now(actor=args.actor)
        if args.json:
            _print_json(state)
        else:
            print("数据面：%s（%s）" % ("正常" if state["ok"] else "异常", state["detail"]))
        return 0 if state["ok"] else 3
    if command == "set":
        return _cli_set(args)
    if command == "log":
        rows = egress_log_tail(args.limit, args.action)
        if args.json:
            _print_json({"stats": egress_log_stats(), "items": rows})
        else:
            if not rows:
                print("暂无访问流水。")
            for row in rows:
                print("%s %-5s %s:%s %s %s" % (
                    row["at"], row["action"], row["ip"] or "-", row["port"],
                    row["domain"] or "-", row["reason"],
                ))
        return 0
    if command == "probe":
        result = egress_probe(args.host, args.port, args.timeout)
        if args.json:
            _print_json(result)
        else:
            print("%s:%s %s（%d ms）%s" % (
                result["host"], result["port"],
                "可达" if result["ok"] else "不可达",
                result["elapsed_ms"], result["detail"],
            ))
        return 0 if result["ok"] else 1
    if command == "doctor":
        code, text = egress_doctor()
        if args.json:
            _print_json({"code": code, "text": text})
        else:
            print(text)
        return code
    parser.print_help()
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    return egress_cli(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
