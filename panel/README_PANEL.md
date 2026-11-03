# Panel runtime files

这是 GOST SNI Manager 的面板程序目录。

- `app.py`：面板主程序，负责账号、规则、模式切换、配置生成和服务重启；出口 VPS 上还负责 `/egress/*` 路由与 `egress-*` 子命令的参数转发。
- `relay.py`：拼车模式使用的本地 TCP 中继，负责流量统计、限速、流量限制和过期规则拦截。
- `egress.py`：出口访问控制守卫（出口 VPS 常驻）。管策略读写、nftables 下发与校验、DNS 解析缓存、内核日志流水、探测/自检，以及 `gsm` 命令调用的 CLI 入口。不 import `app.py`，自己用 `GOST_PANEL_DATA` / `GOST_PANEL_DB` 定位数据库。
- `wsgi.py`：Gunicorn 入口。
- `tests/test_apply.py`：配置生成、端口校验与热重载/回滚自检。
- `tests/test_egress.py`：出口访问守卫自检（策略真值表、域名校验与匹配、nft 脚本内容与顺序、下发失败回落、流水落库与裁剪、探测失败路径、CLI 退出码等）；直接运行，不需要 root、systemd 和真实 nft。
- `templates/`：页面模板，其中包括出口访问流水页 `egress_log.html`。
- `static/style.css`：页面样式。
- `requirements.txt`：Python 依赖。

默认自用模式下，规则由 GOST 直接转发到目标落地 VPS；拼车模式下，规则先进入本机中继再连接落地 VPS。

每条规则都可以指定入站端口：留空或与默认监听端口一致时按 SNI 分流；单独占用另一个端口时，该端口**同样按 SNI 分流**，而不是无差别转发。所有监听端口都开启流量嗅探并使用 `selector.strategy: fifo`，只有 ClientHello 里的 SNI 命中某条规则才会转发，否则连接会被关闭。这样即使有人在客户端里随便填一个中转端口，也无法连到出口。独立入站端口只有主账号可以设置。配置写入后通过 GOST 的 SIGHUP 热重载生效，已有连接不会被断开。

## 出口访问控制（出口 VPS）

出口访问控制只在 `GSM_EGRESS=1` 安装的出口 VPS 上启用。面板与守卫**同机部署**，面板只做参数收集和展示，真正的判定与强制在 `egress.py` 里，和 `gsm` 命令共用同一份逻辑。

- `panel/egress.py`：策略表 `egress_policy`、流水表 `egress_log`、nftables 数据面（`inet gsm_egress` / `ip6 gsm_egress`）、下发失败回落到全拒绝、30 秒自愈校验、DNS 解析缓存（60 秒级 TTL）。
- `panel/app.py`：`ensure_egress_schema()` 建表；`/egress/toggle`、`/egress/settings`、`/egress/probe`、`/egress/log`、`/egress/log.json`、`/egress/reapply` 六个路由（全部只对主账号开放）；控制台注入 `egress` / `egress_state` / `egress_stats` / `egress_logs`，全局注入 `egress_available`。
- 未启用守卫的机器上 `egress_available()` 为假：导航和卡片不出现，路由被访问时只 flash 提示"本机未启用出口守卫"并回控制台，不会误动 nftables。
- 保存策略后立即下发；下发失败时策略仍然保存，数据面保持全拒绝，并把失败原因显示在控制台。

### 前端

- `templates/dashboard.html`：「出口访问」卡片，包含开关、模式/白名单/block_quic/ipv6 设置、`host:port` 探测、重新下发按钮、最近流水和状态摘要。模板变量缺失时优雅降级（Jinja `default`）。
- `templates/egress_log.html`：`/egress/log` 流水页，显示 `at/domain/ip/port/action/reason`，支持按 `action` 和条数 `limit` 筛选。
- `templates/base.html`：仅在 `egress_available` 为真时显示出口相关导航。
- `static/style.css`：出口卡片与流水页的样式片段追加在文件末尾，不影响已有选择器。

### 自检

出口守卫的单元测试不依赖 root、systemd 和真实 nft（底层 nft 执行器与 DNS 解析都可替换）：

```bash
python panel/tests/test_egress.py
```

手动查看策略与流水（在有守卫的出口 VPS 上）：

```bash
python panel/app.py egress-show
python panel/app.py egress-log --limit 20
python panel/app.py egress-probe --host 1.1.1.1 --port 443
```

## 配置应用与自检

保存规则后，面板在后台对 GOST 执行热重载（`systemctl reload` → `SIGHUP` → 必要时重启），随后检查配置里的每个入站端口是否真的在监听：

- 端口正常：本次修改生效。
- 端口缺失或服务非 active：判定为配置被拒绝，自动回滚到上一份可用配置并重新热重载，失败原因显示在控制台提示条。

单独运行自检（不依赖 systemd 和网络）：

```bash
python panel/tests/test_apply.py
```
