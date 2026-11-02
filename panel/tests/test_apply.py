#!/usr/bin/env python3
"""Self-checks for config generation and the reload/rollback path.

Runs with the standard library only, no network and no real systemd:

    python panel/tests/test_apply.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

SANDBOX = Path(tempfile.mkdtemp(prefix="gsm-tests-"))
os.environ.setdefault("GOST_PANEL_DATA", str(SANDBOX))
os.environ.setdefault("GOST_CONFIG", str(SANDBOX / "config.yaml"))
os.environ.setdefault("GOST_SERVICE", "gost")
os.environ.setdefault("GSM_RELAY_SERVICE", "gsm-relay")
os.environ.setdefault("CADDY_SERVICE", "caddy")
os.environ.setdefault("PANEL_PORT", "7090")
os.environ.setdefault("CADDY_HTTPS_PORT", "8053")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402


class FakeSystemctl:
    """Replaces only systemctl calls; real helpers (ss, file IO) still run."""

    def __init__(self, active: bool = True, reload_ok: bool = True):
        self.active = active
        self.reload_ok = reload_ok
        self.calls: list[list[str]] = []
        self.on_reload = None

    def __call__(self, cmd, allow_fail=False):
        self.calls.append(list(cmd))
        if cmd[:2] == ["systemctl", "is-active"]:
            return (0, "active\n") if self.active else (3, "inactive\n")
        if cmd[:2] == ["systemctl", "reload"]:
            if self.on_reload:
                self.on_reload()
            return (0, "") if self.reload_ok else (1, "unknown operation")
        return (0, "")

    def verbs(self) -> list[str]:
        return [c[1] for c in self.calls if c[:1] == ["systemctl"]]


def install_user() -> str:
    app.init_db()
    user = app.find_user_by_username("testadmin")
    if not user:
        app.execute(
            "INSERT INTO users (id,username,password_hash,role,enabled,max_rules,created_at,updated_at)"
            " VALUES ('t1','testadmin','x','admin',1,999,?,?)",
            (app.utc_now(), app.utc_now()),
        )
    return "t1"


def reset_rules(admin_id: str) -> None:
    app.execute("DELETE FROM rules")
    app.set_setting("listen", ":443")
    app.set_setting("fallback_addr", "127.0.0.1:8053")


def make_rule(rule_id: str, sni: str, target: str, port: int, listen_port: int) -> dict:
    return {
        "id": rule_id,
        "owner_id": "t1",
        "sni": sni,
        "target": target,
        "port": port,
        "listen_port": listen_port,
        "note": "",
        "enabled": 1,
        "relay_port": 56000 + int(rule_id.lstrip("r") or 1),
        "speed_limit_mbps": None,
        "traffic_limit_gb": None,
    }


def service_block(config: str, port: int) -> str:
    """Return the YAML block of the service listening on `port`."""
    blocks = config.split("\n- name: ")
    for block in blocks:
        if f'addr: ":{port}"' in block:
            return block
    raise AssertionError(f"no service block for port {port}\n{config}")


def test_default_port_uses_sni_dispatch(admin_id: str) -> None:
    reset_rules(admin_id)
    app.insert_rule(make_rule("r1", "a.example.com", "198.51.100.1", 443, 443))
    app.insert_rule(make_rule("r2", "b.example.com", "198.51.100.2", 443, 443))
    config = app.generate_gost_config()
    assert config.count("sniffing: true") == 1, config
    assert config.count("matcher:") == 2, config
    assert "caddy-fallback" in config, config
    print("ok default port SNI dispatch")


def test_mapped_port_single_rule_stays_sni_gated(admin_id: str) -> None:
    """Regression: a lone rule on its own port used to be a plain TCP forward.

    That made the port an open exit -- any SNI, and any client that never got
    the SNI, was forwarded to the landing target. Every service must sniff and
    every client node must carry a matcher.
    """
    reset_rules(admin_id)
    app.insert_rule(make_rule("r1", "a.example.com", "198.51.100.1", 443, 8443))
    config = app.generate_gost_config()
    assert 'addr: ":8443"' in config, config
    block = service_block(config, 8443)
    assert "sniffing: true" in block, block
    assert block.count("matcher:") == 1, block
    assert "rule: Host(`a.example.com`)" in block, block
    # No catch-all: an unmatched SNI must be closed, not forwarded somewhere.
    assert "caddy-fallback" not in block, block
    assert "strategy: fifo" in block, block
    print("ok mapped port single rule stays SNI gated")


def test_mapped_port_multi_rule_uses_sni(admin_id: str) -> None:
    reset_rules(admin_id)
    app.insert_rule(make_rule("r1", "a.example.com", "198.51.100.1", 443, 8443))
    app.insert_rule(make_rule("r2", "b.example.com", "198.51.100.2", 443, 8443))
    config = app.generate_gost_config()
    assert 'addr: ":8443"' in config, config
    block = service_block(config, 8443)
    assert block.count("matcher:") == 2, block
    assert "caddy-fallback" not in block, block
    assert "strategy: fifo" in block, block
    print("ok mapped port SNI dispatch")


def test_every_client_node_is_matcher_gated(admin_id: str) -> None:
    """No node outside the Caddy fallback may be catch-all."""
    reset_rules(admin_id)
    app.insert_rule(make_rule("r1", "a.example.com", "198.51.100.1", 443, 443))
    app.insert_rule(make_rule("r2", "b.example.com", "198.51.100.2", 443, 8443))
    app.insert_rule(make_rule("r3", "c.example.com", "198.51.100.3", 443, 8444))
    config = app.generate_gost_config()
    nodes: list[list[str]] = []
    current = None
    for line in config.splitlines():
        if line.startswith("    - name: "):
            current = [line]
            nodes.append(current)
        elif current is not None:
            current.append(line)
    matchless = ["\n".join(n) for n in nodes if not any(l.strip() == "matcher:" for l in n)]
    assert len(matchless) == 1, matchless
    assert "caddy-fallback" in matchless[0], matchless[0]
    print("ok every client node is matcher gated")


def test_validator_rejects_open_forward() -> None:
    """A config that exposes a port as an open forward must not be written."""
    poisoned = "\n".join([
        "services:",
        "- name: reality-8443",
        '  addr: ":8443"',
        "  handler:",
        "    type: tcp",
        "    metadata:",
        "      sniffing: true",
        "  listener:",
        "    type: tcp",
        "  forwarder:",
        "    nodes:",
        "    - name: sni-a-example-com",
        "      addr: 198.51.100.1:443",
        "  selector:",
        "    strategy: fifo",
    ])
    try:
        app.validate_gost_config(poisoned)
    except RuntimeError as exc:
        assert "开放出口" in str(exc), exc
    else:
        raise AssertionError("validator accepted a matcher-less forward")
    print("ok validator rejects open forward")


def test_validator_requires_sniffing() -> None:
    poisoned = "\n".join([
        "services:",
        "- name: reality-8443",
        '  addr: ":8443"',
        "  handler:",
        "    type: tcp",
        "  listener:",
        "    type: tcp",
        "  forwarder:",
        "    nodes:",
        "    - name: sni-a-example-com",
        "      addr: 198.51.100.1:443",
        "      matcher:",
        "        rule: Host(`a.example.com`)",
        "  selector:",
        "    strategy: fifo",
    ])
    try:
        app.validate_gost_config(poisoned)
    except RuntimeError as exc:
        assert "SNI" in str(exc), exc
    else:
        raise AssertionError("validator accepted a service without sniffing")
    print("ok validator requires sniffing")


def test_inbound_port_validation(admin_id: str) -> None:
    reset_rules(admin_id)

    class Form(dict):
        def get(self, key, default=""):
            return dict.get(self, key, default)

    actor = {"id": admin_id, "role": "admin"}
    sub = {"id": "sub1", "role": "user"}
    for port, expected in (("7090", "面板"), ("8053", "Caddy"), ("56000", "中继"), ("99999", "1-65535"),
                           ("0", "1-65535"), ("22", "常见服务端口段"), ("443", "常见服务端口段")):
        if port == "443":
            continue  # 443 is the default listen port here, handled below
        try:
            app.parse_rule_form(Form(sni="x.example.com", target="198.51.100.1", port="443", listen_port=port), actor, actor, None)
        except ValueError as exc:
            assert expected in str(exc), (port, exc)
        else:
            raise AssertionError(f"inbound port {port} was accepted")

    # 子账号不能另开入站端口，只能用默认端口。
    sub_owner = {"id": "sub1", "role": "user"}
    try:
        app.parse_rule_form(Form(sni="x.example.com", target="198.51.100.1", port="443", listen_port="8443"), sub, sub_owner, None)
    except ValueError as exc:
        assert "主账号" in str(exc), exc
    else:
        raise AssertionError("sub account was allowed to pick a dedicated inbound port")

    rule = app.parse_rule_form(Form(sni="x.example.com", target="198.51.100.1", port="443", listen_port="8443"), actor, actor, None)
    assert rule["listen_port"] == 8443 and rule["port"] == 443, rule
    default_rule = app.parse_rule_form(Form(sni="y.example.com", target="198.51.100.1", port="443", listen_port=""), sub, sub_owner, None)
    assert default_rule["listen_port"] == 443, default_rule
    print("ok inbound port validation")


def test_reload_prefers_sighup() -> None:
    real = app.run_cmd
    fake = FakeSystemctl()
    app.run_cmd = fake
    try:
        app.reload_gost_service()
    finally:
        app.run_cmd = real
    assert fake.verbs() == ["is-active", "reload"], fake.verbs()

    fake = FakeSystemctl(reload_ok=False)
    app.run_cmd = fake
    try:
        app.reload_gost_service()
    finally:
        app.run_cmd = real
    assert fake.verbs()[-1] == "kill", fake.verbs()

    fake = FakeSystemctl(active=False)
    app.run_cmd = fake
    try:
        app.reload_gost_service()
    finally:
        app.run_cmd = real
    assert fake.verbs()[-1] == "restart", fake.verbs()
    print("ok reload path selection")


def test_bad_config_rolls_back(admin_id: str) -> None:
    reset_rules(admin_id)
    app.insert_rule(make_rule("r1", "a.example.com", "198.51.100.1", 443, 443))
    app.apply_config(restart=False)
    good = Path(app.GOST_CONFIG).read_text(encoding="utf-8")

    real = app.run_cmd
    state = {"checks": 0}

    def fake(cmd, allow_fail=False):
        if cmd[:2] == ["systemctl", "is-active"]:
            state["checks"] += 1
            # first check = pre-reload (active); second = verify after reload (failed)
            return (0, "active\n") if state["checks"] != 2 else (3, "failed\n")
        return (0, "")

    app._PREVIOUS_CONFIG = good
    Path(app.GOST_CONFIG).write_text("services:\n", encoding="utf-8")
    app.run_cmd = fake
    try:
        original_in_use = app.port_in_use
        app.port_in_use = lambda port: False
        app._apply_worker()
    finally:
        app.run_cmd = real
        app.port_in_use = original_in_use

    assert "回滚" in (app.APPLY_STATE["error"] or ""), app.APPLY_STATE
    assert Path(app.GOST_CONFIG).read_text(encoding="utf-8") == good
    print("ok rollback on rejected config")


def main() -> int:
    admin_id = install_user()
    test_default_port_uses_sni_dispatch(admin_id)
    test_mapped_port_single_rule_stays_sni_gated(admin_id)
    test_mapped_port_multi_rule_uses_sni(admin_id)
    test_every_client_node_is_matcher_gated(admin_id)
    test_validator_rejects_open_forward()
    test_validator_requires_sniffing()
    test_inbound_port_validation(admin_id)
    test_reload_prefers_sighup()
    test_bad_config_rolls_back(admin_id)
    print("all apply tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
