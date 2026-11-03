#!/usr/bin/env python3
"""自测：出口守卫（panel/egress.py）的策略、nft 脚本、下发回退、流水与 CLI。

只用标准库，不需要真实 nft / systemd / 网络：

    python panel/tests/test_egress.py

覆盖 TEAM_CONTRACT.md 第 11 节要求的全部条目。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

SANDBOX = Path(tempfile.mkdtemp(prefix="gsm-egress-tests-"))
os.environ["GOST_PANEL_DATA"] = str(SANDBOX)
os.environ.pop("GOST_PANEL_DB", None)
os.environ["GSM_EGRESS_RUNDIR"] = str(SANDBOX / "run")
os.environ["GSM_EGRESS"] = "1"
os.environ["GSM_EGRESS_HOST"] = "1"
os.environ["GSM_EGRESS_RESOLV_CONF"] = str(SANDBOX / "resolv.conf")
os.environ["GSM_EGRESS_LOG_MAX"] = "50"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import egress  # noqa: E402

# 解析结果固定下来，测试不依赖真实 DNS。
FAKE_DNS = {
    "a.example.com": ["203.0.113.10", "2001:db8::10"],
    "b.example.com": ["203.0.113.11"],
    "dead.example.com": [],
}

(
    SANDBOX / "resolv.conf"
).write_text("nameserver 1.1.1.1\nnameserver 2001:4860:4860::8888\nnameserver 9.9.9.9\n", encoding="utf-8")


def fake_resolve_host(host: str) -> list:
    return list(FAKE_DNS.get(host, []))


egress.resolve_host = fake_resolve_host


class FakeNft:
    """替换 run_nft：按脚本类型返回结果，并可强制某一次下发失败。"""

    def __init__(self) -> None:
        self.scripts: list = []
        self.queries: list = []
        self.fail_bodies: bool = False
        self.fail_queries: bool = False
        self.tables = {"inet gsm_egress", "ip6 gsm_egress"}

    def __call__(self, script: str):
        if script.startswith("list tables"):
            self.queries.append(script)
            if self.fail_queries:
                return (1, "", "nft: 未安装")
            out = "".join("table %s\n" % name for name in sorted(self.tables))
            return (0, out, "")
        if script.startswith("list table "):
            self.queries.append(script)
            if self.fail_queries:
                return (1, "", "nft: 未安装")
            name = script.strip()[len("list table "):]
            if name not in self.tables:
                return (1, "", "No such file or directory")
            policy = last_script_policy
            lines = ["table %s {" % name, "\tchain gsm_out {" if name.startswith("inet") else "\tchain gsm_out6 {"]
            if policy is not None and not egress._expects_drop(policy):
                lines.append("\t\taccept")
            else:
                lines.append('\t\tlog prefix "gsm-egress-block: " limit rate 12/minute burst 6 drop')
            lines.append("\t}")
            lines.append("}")
            return (0, "\n".join(lines) + "\n", "")
        self.scripts.append(script)
        for line in script.splitlines():
            if line.startswith("delete table "):
                self.tables.discard(line[len("delete table "):])
            elif line.startswith("table "):
                self.tables.add(line[len("table "):].rstrip(" {"))
        if self.fail_bodies and "table inet gsm_egress" in script:
            return (1, "", "nft: syntax error")
        return (0, "", "")


last_script_policy: dict | None = None

# 保存模块自带的真实 run_nft：install_fake() 会替换 egress.run_nft，
# 需要测真实实现的用例从这里取。
ORIGINAL_RUN_NFT = egress.run_nft
ORIGINAL_RESOLVE_HOST = egress.resolve_host


def install_fake() -> FakeNft:
    fake = FakeNft()
    egress.run_nft = fake
    return fake


def reset(policy: dict | None = None, allow_resolve: bool = True) -> dict:
    """重置策略与状态文件，返回默认（关闭）策略。"""
    egress.ensure_egress_schema()
    with egress.closing(egress.get_db()) as db, db:
        db.execute("DELETE FROM egress_policy")
        db.execute("DELETE FROM egress_log")
    egress.ensure_egress_schema()
    egress.clear_resolve_cache()
    egress.resolve_host = fake_resolve_host if allow_resolve else (lambda host: [])
    state_file = egress.egress_runtime_dir() / egress.STATE_FILE
    if state_file.exists():
        state_file.unlink()
    return egress.get_egress_policy()


def apply_policy(policy: dict) -> str:
    """把策略写进 DB 后调用纯函数生成脚本。"""
    egress.save_egress_policy(
        enabled=policy.get("enabled"),
        mode=policy.get("mode"),
        domains="\n".join(policy.get("domains") or []),
        block_quic=policy.get("block_quic"),
        ipv6=policy.get("ipv6"),
        actor="test",
    )
    return egress.nft_script(
        egress.get_egress_policy(),
        policy.get("allow4") or [],
        policy.get("allow6") or [],
        policy.get("dns4") or [],
        policy.get("dns6") or [],
    )


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def body_lines(script: str) -> list:
    """去掉注释和空行后的脚本行。"""
    return [line for line in script.splitlines() if line.strip() and not line.strip().startswith("#")]


def test_default_policy_and_schema_idempotent() -> None:
    reset()
    policy = egress.get_egress_policy()
    check(policy["enabled"] is False, "默认必须是关闭")
    check(policy["mode"] == "allowlist", "默认模式必须是 allowlist")
    check(policy["block_quic"] is True, "默认必须丢弃 QUIC")
    check(policy["ipv6"] is True, "默认必须守护 IPv6")
    check(policy["domains"] == [], "默认域名清单必须为空")

    # 幂等：连续三次建表不报错，且默认行仍然只有一条。
    for _ in range(3):
        egress.ensure_egress_schema()
    with egress.closing(egress.get_db()) as db:
        rows = db.execute("SELECT COUNT(*) AS c FROM egress_policy").fetchone()["c"]
        check(int(rows) == 1, "egress_policy 必须只有一行：%s" % rows)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(egress_policy)").fetchall()}
    check(
        columns == {"id", "enabled", "mode", "domains", "block_quic", "ipv6", "updated_at", "updated_by"},
        "egress_policy 列不完整：%s" % sorted(columns),
    )
    print("ok 默认策略与 schema 幂等")


def test_old_database_upgrade() -> None:
    """旧库（只有 users/rules/settings）必须能直接升级。"""
    db_path = Path(os.environ["GOST_PANEL_DATA"]) / "gsm.db"
    legacy = Path(os.environ["GOST_PANEL_DATA"]) / "legacy.db"
    import sqlite3

    with egress.closing(sqlite3.connect(str(legacy))) as db, db:
        db.execute("CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT)")
        db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO settings VALUES ('listen', ':443')")
    old_db_env = os.environ.get("GOST_PANEL_DB")
    os.environ["GOST_PANEL_DB"] = str(legacy)
    try:
        egress.ensure_egress_schema()
        policy = egress.get_egress_policy()
        check(policy["enabled"] is False, "升级后的旧库默认必须是关闭")
        with egress.closing(egress.get_db()) as db:
            keep = db.execute("SELECT value FROM settings WHERE key='listen'").fetchone()["value"]
        check(keep == ":443", "升级不能动旧表数据")
    finally:
        if old_db_env is None:
            os.environ.pop("GOST_PANEL_DB", None)
        else:
            os.environ["GOST_PANEL_DB"] = old_db_env
    check(db_path.exists(), "主库仍然可用")
    print("ok 旧库原地升级")


def test_truth_table() -> None:
    """真值表四种组合 + 关闭时不得引用 @allow4。"""
    # 关闭：任意模式、任意域名都拒绝。
    script = apply_policy({
        "enabled": False, "mode": "allowlist", "domains": ["a.example.com"],
        "block_quic": True, "ipv6": True, "allow4": ["203.0.113.10"],
    })
    check("drop" in script, "关闭时必须 drop")
    check("@allow4" not in script, "关闭时不得引用 @allow4")
    check("set allow4 {" in script, "关闭时集合仍要声明（保持为空）")
    check("elements" not in script, "关闭时集合必须为空")
    check("gsm-egress-allow:" not in script, "关闭时不得有放行日志规则")
    check(body_lines(script)[-1].strip() == "}", "关闭时脚本必须以表结尾")

    # 打开 + all：直接放行。
    script = apply_policy({
        "enabled": True, "mode": "all", "domains": [], "block_quic": True, "ipv6": True,
    })
    check("@allow4" not in script, "all 模式不引用 allow 集合")
    check("gsm-egress-allow:" not in script, "all 模式不打放行日志（契约第 4 节）")
    check("udp dport 443 drop" in script, "all 模式仍应丢弃 QUIC")
    accept_lines = [line for line in body_lines(script) if line.strip() == "accept"]
    check(len(accept_lines) == 2, "all 模式两张表各有一条 accept：%s" % accept_lines)

    # 打开 + allowlist：命中放行、未命中 drop。
    script = apply_policy({
        "enabled": True, "mode": "allowlist", "domains": ["a.example.com", "203.0.113.10"],
        "block_quic": True, "ipv6": True, "allow4": ["203.0.113.10"], "dns4": ["1.1.1.1"],
    })
    check("ip daddr @allow4 log" in script, "allowlist 模式必须引用 @allow4")
    check("gsm-egress-allow:" in script and "gsm-egress-block:" in script, "两个日志前缀都要有")
    check("limit rate 12/minute burst 6" in script, "日志必须限速")
    check("drop" in script, "未命中必须 drop")
    print("ok 真值表四种组合")


def test_normalize_and_parse_domains() -> None:
    check(egress.normalize_domain("Example.COM.") == "example.com", "小写 + 去末尾点")
    check(egress.normalize_domain("*.Example.com") == "example.com", "容忍 *. 前缀")
    check(egress.normalize_domain(" 203.0.113.10 ") == "203.0.113.10", "IPv4 字面量")
    check(egress.normalize_domain("2001:DB8::1") == "2001:db8::1", "IPv6 字面量规范形式")
    for bad in ("", "   ", "http://a.example.com", "a b", "localhost", "a..b", "a.-b.com",
                "a.example.com/", "a.example.com\\", "bad_domain.example.com", "x" * 300):
        try:
            egress.normalize_domain(bad)
        except ValueError:
            continue
        raise AssertionError("非法域名被接受：%r" % bad)

    parsed = egress.parse_domain_text("b.example.com\nA.example.com.\nb.example.com\n\n# 注释\n203.0.113.10, a.example.com\n")
    check(parsed == ["b.example.com", "a.example.com", "203.0.113.10"], "去重保序结果不对：%s" % parsed)
    try:
        egress.parse_domain_text("a.example.com\noops\n")
    except ValueError:
        pass
    else:
        raise AssertionError("parse_domain_text 必须逐行校验")

    rules = ["example.com", "203.0.113.10"]
    check(egress.domain_matches("EXAMPLE.com", rules), "大小写不敏感")
    check(egress.domain_matches("a.example.com.", rules), "子域 + 末尾点")
    check(egress.domain_matches("203.0.113.10", rules), "IP 精确匹配")
    check(not egress.domain_matches("notexample.com", rules), "不得后缀误匹配")
    check(not egress.domain_matches("203.0.113.100", rules), "IP 不得前缀匹配")
    check(not egress.domain_matches("", rules), "空值不匹配")
    check(not egress.domain_matches("a.other.com", rules), "无关域名不匹配")
    print("ok normalize/parse/domain_matches")


def test_nft_script_order_and_content() -> None:
    """规则顺序：private → established → quic → DNS → allow4 → drop（契约第 4 节）。"""
    script = apply_policy({
        "enabled": True, "mode": "allowlist", "domains": ["a.example.com"],
        "block_quic": True, "ipv6": True,
        "allow4": ["203.0.113.10", "198.51.100.7"], "dns4": ["1.1.1.1"],
    })
    lines = [line.strip() for line in body_lines(script)]
    check(lines[0] == "table inet gsm_egress {", "IPv4 表必须最先出现：%s" % lines[0])
    check("table ip6 gsm_egress {" in lines, "ipv6=1 时必须生成 ip6 表")
    check("set allow4 {" in lines and "set allow6 {" in lines, "两张表都要有集合")

    def index_of(predicate) -> int:
        for index, line in enumerate(lines):
            if predicate(line):
                return index
        raise AssertionError("找不到规则：%s" % lines)

    lo = index_of(lambda l: l.startswith('oifname "lo" accept'))
    private = index_of(lambda l: l.startswith("ip daddr { 127.0.0.0/8"))
    established = index_of(lambda l: l.startswith("ct state established,related accept"))
    quic = index_of(lambda l: l == "udp dport 443 drop")
    dns = index_of(lambda l: l.startswith("ip daddr { 1.1.1.1 } udp dport 53 accept"))
    allow = index_of(lambda l: l.startswith("ip daddr @allow4 log"))
    deny = index_of(lambda l: l.startswith('log prefix "gsm-egress-block:'))
    check(lo < private < established < quic < dns < allow < deny, "IPv4 规则顺序不对：%s" % lines)
    for cidr in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                 "169.254.0.0/16", "100.64.0.0/10", "224.0.0.0/4", "255.255.255.255"):
        check(cidr in lines[private], "private4 缺 %s" % cidr)
    for line in lines:
        if line.startswith("ip6 daddr { "):
            for cidr in ("::1", "fc00::/7", "fe80::/10", "ff00::/8"):
                check(cidr in line, "private6 缺 %s" % cidr)
            break
    else:
        raise AssertionError("找不到 IPv6 内网放行规则")
    check(lines[allow] == 'ip daddr @allow4 log prefix "gsm-egress-allow: " limit rate 12/minute burst 6 accept',
          "allow 日志规则内容不对：%s" % lines[allow])
    check(lines[deny] == 'log prefix "gsm-egress-block: " limit rate 12/minute burst 6 drop',
          "拒绝规则内容不对：%s" % lines[deny])
    check("ip6 daddr @allow6 log" in script, "IPv6 表必须有 allow6 规则")

    # block_quic=0 时不得出现 QUIC drop；ipv6=0 时不得有 ip6 表。
    script = apply_policy({
        "enabled": True, "mode": "allowlist", "domains": ["a.example.com"],
        "block_quic": False, "ipv6": False, "allow4": ["203.0.113.10"], "dns4": ["1.1.1.1"],
    })
    check("udp dport 443 drop" not in script, "block_quic=0 时不得丢 QUIC")
    check("table ip6 gsm_egress" not in script, "ipv6=0 时不得生成 ip6 表")

    # 解析到 IPv6 的域名要进 allow6。
    check(egress.nft_script({"enabled": True, "mode": "allowlist", "ipv6": True},
                            [], ["2001:db8::10"], [], []) .count("2001:db8::10") == 1,
          "IPv6 放行地址必须进 allow6 集合")
    print("ok nft_script 顺序与内容")


def test_apply_success_writes_state() -> None:
    global last_script_policy
    reset()
    egress.save_egress_policy(enabled=True, mode="allowlist",
                              domains="a.example.com\n203.0.113.10", actor="tester")
    fake = install_fake()
    last_script_policy = egress.get_egress_policy()
    state = egress.egress_apply_now(actor="tester")
    check(state["ok"] is True, "下发应当成功：%s" % state)
    # a.example.com 解析出的 203.0.113.10 与清单里的 IP 字面量去重后只剩 1 个。
    check(state["allow4_count"] == 1, "allow4_count 应为 1（去重后的 IPv4 地址数）：%s" % state)
    check(state["allow6_count"] == 1, "allow6_count 应为 1（解析出的 IPv6）：%s" % state)
    check(state["detail"].startswith("出口已打开"), "detail 不对：%s" % state["detail"])
    check(state["needs_apply"] is False, "刚下发完不应需要重下发：%s" % state)
    state_file = egress.egress_runtime_dir() / egress.STATE_FILE
    check(state_file.is_file(), "state.json 必须落盘")
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    check(saved["ok"] is True and saved["allow4_count"] == 1, "state.json 内容不对：%s" % saved)
    check(saved["policy_hash"] == state["policy_hash"], "policy_hash 必须一致")
    check(fake.scripts, "必须真的调用过 nft")
    check("@allow4" in fake.scripts[0], "下发的脚本必须引用 @allow4")

    # 策略变化 → needs_apply 变真。
    egress.save_egress_policy(domains="b.example.com", actor="tester")
    check(egress.egress_apply_state()["needs_apply"] is True, "策略变化后必须提示需要重下发")

    # 关闭后集合必须清空。
    egress.save_egress_policy(enabled=False, actor="tester")
    last_script_policy = egress.get_egress_policy()
    state = egress.egress_apply_now(actor="tester")
    check(state["allow4_count"] == 0 and state["allow6_count"] == 0, "关闭后放行集合必须为空：%s" % state)
    last_script = fake.scripts[-1]
    check("@allow4" not in last_script, "关闭后的脚本不得引用 @allow4")
    check("elements" not in last_script, "关闭后集合必须为空")
    print("ok 下发成功 + state.json + 集合计数")


def test_apply_failure_falls_back_to_deny() -> None:
    global last_script_policy
    reset()
    egress.save_egress_policy(enabled=True, mode="allowlist", domains="a.example.com", actor="tester")
    fake = install_fake()
    fake.fail_bodies = True          # 所有下发都失败
    fake.fail_queries = True         # 连校验也失败（模拟 nft 完全不可用）
    last_script_policy = egress.get_egress_policy()
    state = egress.egress_apply_now(actor="tester")
    check(state["ok"] is False, "下发失败时 ok 必须为 False")
    check("回退到全拒绝" in state["detail"], "detail 必须说明回退：%s" % state["detail"])
    check(state["allow4_count"] == 0 and state["allow6_count"] == 0, "回退后不得有放行地址")
    check(state["needs_apply"] is True, "失败后必须保持需要重下发（守护进程会自愈）")
    saved = json.loads((egress.egress_runtime_dir() / egress.STATE_FILE).read_text(encoding="utf-8"))
    check(saved["ok"] is False, "state.json 必须记录 ok=False")
    # 最后一条下发的一定是回退用的“全拒绝”规则体。
    deny_script = fake.scripts[-1]
    check("gsm-egress-block:" in deny_script, "回退脚本必须包含拒绝规则")
    check("@allow4" not in deny_script, "回退脚本不得引用 @allow4")
    check("elements" not in deny_script, "回退脚本的集合必须为空")

    # 主脚本语法错但回退脚本能装：ok=False，detail 仍含"回退"。
    fake = install_fake()
    calls = {"n": 0}

    def flaky(script: str):
        if "table inet gsm_egress" in script and "@allow4" in script:
            fake.scripts.append(script)
            return (1, "", "nft: syntax error")
        return fake(script)

    egress.run_nft = flaky
    state = egress.egress_apply_now(actor="tester")
    check(state["ok"] is False, "主脚本失败必须报 ok=False")
    check("回退到全拒绝形态" in state["detail"], "detail 不对：%s" % state["detail"])
    check("也失败" not in state["detail"], "回退成功时不应说回退也失败：%s" % state["detail"])
    check(egress.egress_apply_state()["applied_hash"] != "", "回退后也要记录已下发的指纹")
    print("ok 下发失败回落到全拒绝")


def test_verify_rejects_missing_drop() -> None:
    reset()
    egress.save_egress_policy(enabled=False, actor="tester")
    fake = install_fake()

    def no_drop(script: str):
        if script.startswith("list table"):
            return (0, "table inet gsm_egress {\n\tchain gsm_out {\n\t\taccept\n\t}\n}\n", "")
        return fake(script)

    egress.run_nft = no_drop
    state = egress.egress_apply_now(actor="tester")
    check(state["ok"] is False, "校验发现没有 drop 时必须判失败")
    check("回退到全拒绝" in state["detail"] or "校验失败" in state["detail"], state["detail"])
    print("ok 校验发现缺 drop 判失败")


def test_resolver_and_fingerprint() -> None:
    reset()
    dns4, dns6 = egress.resolver_addresses()
    check(dns4 == ["1.1.1.1", "9.9.9.9"], "resolv.conf IPv4 解析不对：%s" % dns4)
    check(dns6 == ["2001:4860:4860::8888"], "resolv.conf IPv6 解析不对：%s" % dns6)

    egress.clear_resolve_cache()
    check(egress.resolve_cached("a.example.com") == ["203.0.113.10", "2001:db8::10"], "解析缓存内容不对")
    cached = egress._RESOLVE_CACHE["a.example.com"]
    check(egress.resolve_cached("a.example.com") == list(cached[1]), "第二次必须命中缓存")

    policy = {"enabled": True, "mode": "allowlist", "domains": ["a.example.com", "203.0.113.10", "dead.example.com"]}
    allow4, allow6, unresolved = egress.resolve_allow_addresses(policy)
    check(allow4 == ["203.0.113.10"], "allow4 不对：%s" % allow4)
    check(allow6 == ["2001:db8::10"], "allow6 不对：%s" % allow6)
    check(unresolved == ["dead.example.com"], "解析失败清单不对：%s" % unresolved)

    disabled = {"enabled": False, "mode": "allowlist", "domains": ["a.example.com"]}
    check(egress.resolve_allow_addresses(disabled) == ([], [], []), "关闭时不得解析任何域名")

    hash_a = egress._policy_fingerprint(policy, allow4, allow6, dns4, dns6)
    hash_b = egress._policy_fingerprint(policy, allow4, allow6, dns4, dns6)
    check(hash_a == hash_b, "同一状态指纹必须稳定")
    hash_c = egress._policy_fingerprint(policy, ["198.51.100.1"], allow6, dns4, dns6)
    check(hash_a != hash_c, "解析结果变化必须改变指纹（触发重下发）")
    print("ok DNS 缓存与策略指纹")


def test_egress_log_flow() -> None:
    reset()
    egress.egress_log_append("2026-01-01T00:00:00+00:00", "a.example.com", "203.0.113.10", 443, "allow", "allowlist")
    egress.egress_log_append("2026-01-01T00:00:01+00:00", "", "1.2.3.4", 80, "block", "egress-disabled")
    egress.egress_log_append("2026-01-01T00:00:02+00:00", "", "5.6.7.8", 0, "block", "not-in-allowlist")
    tail = egress.egress_log_tail(limit=10)
    check(len(tail) == 3, "流水条数不对：%s" % tail)
    check(set(tail[0]) == {"at", "domain", "ip", "port", "action", "reason"}, "字段名必须固定：%s" % tail[0])
    check(tail[0]["ip"] == "5.6.7.8", "最新的在前")
    check(len(egress.egress_log_tail(limit=10, action="allow")) == 1, "action=allow 过滤不对")
    check(len(egress.egress_log_tail(limit=10, action="block")) == 2, "action=block 过滤不对")
    stats = egress.egress_log_stats()
    check(stats["total"] == 3 and stats["allow"] == 1 and stats["block"] == 2, "统计不对：%s" % stats)
    check(stats["last"] == "2026-01-01T00:00:02+00:00", "最后时间不对：%s" % stats["last"])

    # 裁剪：GSM_EGRESS_LOG_MAX=10，每 50 次插入触发一次自动裁剪。
    reset()
    old_max = os.environ.get("GSM_EGRESS_LOG_MAX")
    os.environ["GSM_EGRESS_LOG_MAX"] = "10"
    try:
        for index in range(60):
            egress.egress_log_append("2026-01-01T00:00:%02d+00:00" % (index % 60), "", "10.0.0.%d" % index,
                                     index, "block", "not-in-allowlist")
        total = egress.egress_log_stats()["total"]
        check(total < 60, "插入 50 条后必须自动裁剪，实际 %s" % total)
        check(total > 10, "自动裁剪应当只删旧行，实际 %s" % total)
        egress.trim_egress_log(10)
        check(egress.egress_log_stats()["total"] == 10, "显式裁剪到 10 行失败")
        newest = egress.egress_log_tail(limit=1)[0]
        check(newest["ip"] == "10.0.0.59", "裁剪必须保留最新行：%s" % newest)
    finally:
        if old_max is None:
            os.environ.pop("GSM_EGRESS_LOG_MAX", None)
        else:
            os.environ["GSM_EGRESS_LOG_MAX"] = old_max
    print("ok 流水 append/tail/stats/裁剪")


def test_kernel_log_parsing() -> None:
    reset()
    egress.save_egress_policy(enabled=True, mode="allowlist", domains="a.example.com", actor="tester")
    egress.clear_resolve_cache()
    egress.resolve_cached("a.example.com")

    blocked = ("[123.456] gsm-egress-block: IN= OUT=eth0 SRC=10.0.0.5 DST=198.51.100.9 "
               "LEN=60 PROTO=TCP SPT=41234 DPT=443 WINDOW=65535 SYN")
    parsed = egress.parse_kernel_log_line(blocked)
    check(parsed == {"action": "block", "ip": "198.51.100.9", "port": 443, "proto": "tcp"},
          "解析结果不对：%s" % parsed)
    check(egress.parse_kernel_log_line("<6>kernel: TCP: whatever") is None, "非本模块日志必须忽略")
    check(egress.parse_kernel_log_line("gsm-egress-block: PROTO=TCP") is None, "没有 DST 必须忽略")

    allowed = "gsm-egress-allow: IN= OUT=eth0 SRC=10.0.0.5 DST=203.0.113.10 PROTO=TCP DPT=443"
    parsed = egress.parse_kernel_log_line(allowed)
    check(parsed["action"] == "allow" and parsed["ip"] == "203.0.113.10", "放行日志解析不对：%s" % parsed)
    check(egress.parse_kernel_log_line("gsm-egress-block: DST=1.2.3.4 PROTO=UDP")["port"] == 0,
          "没有 DPT 时端口必须是 0")

    check(egress._consume_kernel_line(blocked) is True, "拦截流水必须落库")
    check(egress._consume_kernel_line(allowed) is True, "放行流水必须落库")
    rows = egress.egress_log_tail(limit=5)
    check(len(rows) == 2, "应当落两条流水：%s" % rows)
    allow_row = [row for row in rows if row["action"] == "allow"][0]
    block_row = [row for row in rows if row["action"] == "block"][0]
    check(allow_row["domain"] == "a.example.com", "放行必须反查域名：%s" % allow_row)
    check(allow_row["reason"] == "allowlist", "放行 reason 不对：%s" % allow_row)
    check(block_row["domain"] == "" and block_row["reason"] == "not-in-allowlist", "拦截字段不对：%s" % block_row)
    egress.save_egress_policy(enabled=False, actor="tester")
    egress._consume_kernel_line(blocked)
    check(egress.egress_log_tail(limit=1)[0]["reason"] == "egress-disabled", "关闭时 reason 必须是 egress-disabled")
    print("ok 内核日志解析与落库")


def test_probe_failure_path() -> None:
    import socket

    reset()
    egress.save_egress_policy(enabled=False, actor="tester")
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    good = egress.tcp_probe("127.0.0.1", port, timeout=2.0)
    check(good["ok"] is True, "本机监听端口应当探测成功：%s" % good)
    check(set(good) == {"host", "port", "ok", "detail", "elapsed_ms"}, "字段名必须固定：%s" % sorted(good))
    server.close()
    bad = egress.tcp_probe("127.0.0.1", port, timeout=1.0)
    check(bad["ok"] is False, "关闭监听后应当探测失败：%s" % bad)
    check("连接失败" in bad["detail"], "失败详情不对：%s" % bad["detail"])
    check(isinstance(bad["elapsed_ms"], int) and bad["elapsed_ms"] >= 0, "elapsed_ms 必须是整数")

    check(egress.tcp_probe("", 443)["ok"] is False, "空主机必须失败而不是抛异常")
    check(egress.tcp_probe("127.0.0.1", 0)["ok"] is False, "非法端口必须失败")
    check(egress.tcp_probe("127.0.0.1", 99999)["ok"] is False, "超范围端口必须失败")

    result = egress.egress_probe("127.0.0.1", port, timeout=1.0)
    check(result["ok"] is False and "关闭状态" in result["detail"],
          "关闭状态下探测失败必须解释为预期：%s" % result)

    egress.save_egress_policy(enabled=True, mode="allowlist", domains="a.example.com", actor="tester")
    result = egress.egress_probe("127.0.0.1", port, timeout=1.0)
    check("白名单" in result["detail"], "白名单模式下探测失败要提示不在白名单：%s" % result)
    print("ok probe 成功/失败路径")


def test_diag_and_doctor() -> None:
    reset()
    fake = install_fake()
    last = egress.get_egress_policy()
    globals()["last_script_policy"] = last
    egress.egress_apply_now(actor="tester")
    text = egress.egress_diag_text()
    for keyword in ("出口访问", "数据面", "流水", "DNS"):
        check(keyword in text, "诊断文本缺少 %s：%s" % (keyword, text))
    code, doctor = egress.egress_doctor()
    check(code in (0, 1), "doctor 退出码只能是 0/1")
    check("结论" in doctor, "doctor 必须有结论：%s" % doctor)
    summary = egress.egress_summary()
    expected = {"enabled", "mode", "mode_label", "domains", "backend", "ok", "detail", "updated_at",
                "updated_by", "available", "installed", "block_quic", "ipv6", "domain_list",
                "dns_servers", "allow4_count", "allow6_count"}
    check(expected <= set(summary), "egress_summary 字段不齐：缺 %s" % sorted(expected - set(summary)))
    check(fake.queries, "doctor 必须真的查询过 nft 表")
    print("ok 诊断文本与 doctor")


def test_cli_exit_codes() -> None:
    """退出码：0 成功；1 参数/校验错误或本机未启用守卫；3 保存成功但下发失败。"""
    global last_script_policy
    # 未安装守卫：set/apply/domains 返回 1，show 仍可用。
    reset()
    egress_service_file = egress.SERVICE_FILE
    egress.SERVICE_FILE = SANDBOX / "no-such-service.service"
    old_host = os.environ.pop("GSM_EGRESS_HOST", None)
    old_rundir = os.environ["GSM_EGRESS_RUNDIR"]
    os.environ["GSM_EGRESS_RUNDIR"] = str(SANDBOX / "absent-run")
    try:
        check(egress.guard_installed() is False, "没有服务/运行时目录时必须视为未安装")
        check(egress.egress_available() is False, "未安装时 egress_available 必须为假")
        check(egress.egress_cli(["set", "--enable"]) == 1, "未安装守卫 set 必须返回 1")
        check(egress.egress_cli(["apply"]) == 1, "未安装守卫 apply 必须返回 1")
        check(egress.egress_cli(["domains"]) == 1, "未安装守卫 domains 必须返回 1")
        check(egress.get_egress_policy()["enabled"] is False, "未安装守卫时不得改到策略")
        check(egress.egress_cli(["show"]) == 0, "show 在未安装守卫时也必须可用（返回 0）")
    finally:
        egress.SERVICE_FILE = egress_service_file
        if old_host is not None:
            os.environ["GSM_EGRESS_HOST"] = old_host
        os.environ["GSM_EGRESS_RUNDIR"] = old_rundir

    check(egress.egress_available() is True, "恢复后应当可用")
    check(egress.egress_cli(["set", "--enable", "--disable"]) == 1, "互斥参数必须返回 1")
    check(egress.egress_cli(["set", "--mode", "bogus"]) == 1, "非法模式必须返回 1（argparse 退出码归一）")
    check(egress.egress_cli(["set", "--domains", "不是域名"]) == 1, "非法域名必须返回 1")
    check(egress.egress_cli(["nosuch"]) == 1, "未知子命令必须返回 1")

    # 保存成功但下发失败 → 3。
    reset()
    fake = install_fake()
    fake.fail_bodies = True
    fake.fail_queries = True
    last_script_policy = egress.get_egress_policy()
    code = egress.egress_cli(["set", "--enable", "--mode", "allowlist", "--domains", "a.example.com"])
    check(code == 3, "保存成功下发失败必须返回 3，实际 %s" % code)
    check(egress.get_egress_policy()["enabled"] is True, "策略仍然要保存下来")
    check(egress.egress_apply_state()["ok"] is False, "下发失败必须体现到 state")

    # 保存成功且下发成功 → 0。
    fake = install_fake()
    last_script_policy = egress.get_egress_policy()
    code = egress.egress_cli(["set", "--enable", "--domains", "a.example.com,b.example.com", "--no-ipv6"])
    check(code == 0, "下发成功必须返回 0，实际 %s" % code)
    check(egress.get_egress_policy()["ipv6"] is False, "--no-ipv6 必须写入策略")

    # domains 子命令输出、log 子命令、doctor 都应有合法退出码。
    out = []
    real_print = print
    egress.print = lambda *args, **kwargs: out.append(" ".join(str(a) for a in args))  # type: ignore[attr-defined]
    try:
        check(egress.egress_cli(["domains"]) == 0, "domains 应当返回 0")
    finally:
        egress.print = real_print  # type: ignore[attr-defined]
    check(out == ["a.example.com", "b.example.com"], "domains 必须每行一个：%s" % out)
    check(egress.egress_cli(["log", "--limit", "5"]) == 0, "log 应当返回 0")
    check(egress.egress_cli(["doctor"]) in (0, 1), "doctor 退出码只能是 0/1")
    print("ok CLI 退出码 0/1/3")


def test_daemon_start_applies_deny() -> None:
    """守护进程启动即下发：默认关闭 → 全拒绝；GSM_EGRESS_RUN_SECONDS 到点退出。"""
    import threading
    import time

    global last_script_policy
    reset()
    fake = install_fake()
    last_script_policy = egress.get_egress_policy()
    env_backup = {key: os.environ.get(key) for key in
                  ("GSM_EGRESS_POLL_INTERVAL", "GSM_EGRESS_KMSG", "GSM_EGRESS_RUN_SECONDS")}
    os.environ["GSM_EGRESS_POLL_INTERVAL"] = "0.05"
    os.environ["GSM_EGRESS_KMSG"] = str(SANDBOX / "no-kmsg")
    os.environ["GSM_EGRESS_RUN_SECONDS"] = "0.6"
    worker = threading.Thread(target=egress.egress_run)
    worker.start()
    try:
        state_file = egress.egress_runtime_dir() / egress.STATE_FILE
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if state_file.is_file():
                saved = json.loads(state_file.read_text(encoding="utf-8"))
                if saved.get("actor") == "daemon":
                    break
            time.sleep(0.05)
        state = egress.egress_apply_state()
        check(state["ok"] is True, "守护进程启动必须下发成功：%s" % state)
        check(fake.scripts and "gsm-egress-block:" in fake.scripts[0], "启动脚本必须包含拒绝规则")
        check("@allow4" not in fake.scripts[0], "关闭状态启动时不得引用 @allow4")
        saved = json.loads(state_file.read_text(encoding="utf-8"))
        check("流水不可用" in saved["detail"], "没有内核日志时 detail 必须标注：%s" % saved["detail"])
    finally:
        worker.join(timeout=10)
        for key, value in env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    check(worker.is_alive() is False, "守护进程必须在到点/收到停止信号后退出")
    print("ok 守护进程启动即下发（全拒绝）")


def test_nft_missing_is_human_readable() -> None:
    """没有 nft 可执行文件时：backend=none，detail 用人话，不泄露 WinError/ENOENT。"""
    reset()
    saved_nft = os.environ.pop("GSM_EGRESS_NFT", None)
    real_which = egress.shutil.which
    egress.shutil.which = lambda name: None      # 模拟未安装 nftables
    egress.run_nft = ORIGINAL_RUN_NFT            # 本条用例要测真实的 run_nft
    try:
        check(egress._nft_backend() == "none", "找不到 nft 时 backend 必须是 none")
        rc, _out, err = egress.run_nft("list tables\n")
        check(rc != 0, "没有 nft 时必须返回非 0")
        check("未找到 nft 命令" in err, "错误信息必须是人话：%s" % err)
        for native in ("WinError", "Errno", "No such file", "Traceback"):
            check(native not in err, "不得暴露原生报错文本 %s：%s" % (native, err))
        state = egress.egress_apply_now(actor="tester")
        check(state["ok"] is False, "没有 nft 时下发必须失败")
        check(state["backend"] == "none", "state.backend 必须是 none：%s" % state)
        check("nft 不可用" in state["detail"], "detail 必须用契约措辞：%s" % state["detail"])
        check("回退到全拒绝" in state["detail"], "detail 必须说明回退：%s" % state["detail"])
        for native in ("WinError", "Errno", "No such file"):
            check(native not in state["detail"], "detail 不得含原生报错：%s" % state["detail"])
    finally:
        egress.shutil.which = real_which
        egress.run_nft = ORIGINAL_RUN_NFT
        if saved_nft is not None:
            os.environ["GSM_EGRESS_NFT"] = saved_nft
    print("ok 缺少 nft 时的错误措辞与 backend=none")


def test_resolve_timeout_is_bounded() -> None:
    """DNS 解析必须有有限超时，且超时不会抛异常。"""
    real = egress.resolve_host
    backup = os.environ.get("GSM_EGRESS_RESOLVE_TIMEOUT")
    os.environ["GSM_EGRESS_RESOLVE_TIMEOUT"] = "0.5"
    try:
        egress.resolve_host = ORIGINAL_RESOLVE_HOST
        import time as _time

        started = _time.monotonic()
        result = egress.resolve_host("no-such-host.invalid")
        elapsed = _time.monotonic() - started
        check(result == [], "解析失败必须返回空列表：%s" % result)
        check(elapsed < 3.5, "解析耗时必须受 GSM_EGRESS_RESOLVE_TIMEOUT 约束：%.2fs" % elapsed)
        check(egress._resolve_timeout() == 0.5, "超时配置未生效")
    finally:
        egress.resolve_host = real
        if backup is None:
            os.environ.pop("GSM_EGRESS_RESOLVE_TIMEOUT", None)
        else:
            os.environ["GSM_EGRESS_RESOLVE_TIMEOUT"] = backup
    print("ok DNS 解析超时有界")


def test_no_flask_or_app_import() -> None:
    """egress.py 顶层不能 import app / flask（避免循环依赖）。"""
    source = (Path(__file__).resolve().parents[1] / "egress.py").read_text(encoding="utf-8")
    top_level = [
        line.strip() for line in source.splitlines()
        if (line.startswith("import ") or line.startswith("from ")) and "app" not in line.split()[1]
    ]
    check("import flask" not in source and "from flask" not in source, "egress.py 不得依赖 flask")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("import app") or stripped.startswith("from app "):
            raise AssertionError("egress.py 不得 import app：%s" % stripped)
    check(any("import" in line for line in top_level), "源码读取异常")
    print("ok 不依赖 app/flask")


def main() -> int:
    test_default_policy_and_schema_idempotent()
    test_old_database_upgrade()
    test_truth_table()
    test_normalize_and_parse_domains()
    test_nft_script_order_and_content()
    test_apply_success_writes_state()
    test_apply_failure_falls_back_to_deny()
    test_verify_rejects_missing_drop()
    test_resolver_and_fingerprint()
    test_egress_log_flow()
    test_kernel_log_parsing()
    test_probe_failure_path()
    test_diag_and_doctor()
    test_cli_exit_codes()
    test_daemon_start_applies_deny()
    test_nft_missing_is_human_readable()
    test_resolve_timeout_is_bounded()
    test_no_flask_or_app_import()
    print("all egress tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
