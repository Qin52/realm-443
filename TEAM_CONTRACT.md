# 契约 v2：GSM 出口访问控制（海外访问开关 + 便携命令）

本文件是团队成员之间的唯一接口约定。任何一方需要改变已约定的名称/字段/路径，必须先通知 Lead 并同步更新本文件，不得单方面改动。v2 在 v1 基础上补充了安装角色、nft 数据面细节、日志实现、可用性判定与测试要求。

## 0. 定位

在"中转 VPS"之外，GSM 还要能管一台"出口 VPS"（落地/出口机，通常 443 已被 Xray REALITY 占用）：该机的**出站访问**默认关闭，只有明确打开时才允许访问外网（域名白名单 / 全放开）。开关在面板上点，也可以在 SSH 里用便携命令 `gsm` 完成。

关键性质：关闭必须是默认态且是硬默认——面板/CLI 不在场（服务被停、被卸载、数据库被删）时，数据面仍然必须拒绝一切出站。因此判定逻辑放在常驻数据面守护进程里，而不是开关脚本里。

**部署模型（v2 明确）**：面板与守卫**同机部署**在出口 VPS 上（不通过 SSH 远程管理）。普通中转 VPS 安装默认**不安装、不启用**守卫，避免把 GOST 连落地、Caddy 申请证书等必要出站一起挡掉。是否启用由安装环境变量 `GSM_EGRESS=1` 决定（见第 9 节）。

## 1. 组件（严格按此命名）

| 名称 | 路径 | 职责 |
| --- | --- | --- |
| 守卫守护进程 | /opt/gost-sni-manager/egress.py（仓库 panel/egress.py） | 常驻；读 SQLite 策略；用 nftables 集合做数据面拦截；记录访问流水 |
| 便携命令 | gsm = /usr/local/bin/gsm，符号链接到 /opt/gost-sni-manager/tools/gsm（仓库 tools/gsm，Bash） | 不依赖 SSH 交互即可完成全部配置与开关 |
| 数据库表 | egress_policy（单行，主键 id=1） | 存放开关与域名清单 |
| 访问流水表 | egress_log（追加写，自动裁剪） | 记录每次放行/拦截 |
| 数据面 | nftables：table inet gsm_egress（链 gsm_out）+ table ip6 gsm_egress（链 gsm_out6） | 唯一强制点 |
| systemd | gsm-egress.service | 运行守卫 |
| 运行时状态 | /run/gsm-egress/state.json | 最近一次下发结果，面板/CLI 读取 |
| 回归检查脚本 | tools/check_egress_nft.sh（仓库） | 在 Linux 上人工/dry-run 校验 nft 规则生成结果 |

### 为什么用 nftables + 集合

放行名单要能热更新且不断流：域名被解析成 IP 后写进 nft set，集合查找是 O(1)，不需要逐个域名写规则。防火墙里只有两条判断：命中放行集合 accept，否则 drop。开关切换 = 改集合 + 改一个 verdict 变量，整个过程不 reload 防火墙、不断已有连接，也不需要重启任何服务。

## 2. 仓库文件归属（写权限边界）

- A1 组（守卫核心，Teammate `backend-core`）：panel/egress.py（新建）、panel/tests/test_egress.py（新建）
- A2 组（面板后端，Teammate `panel-routes`）：panel/app.py、panel/wsgi.py、panel/requirements.txt（一般不动）
- B 组（前端，Teammate `frontend`）：panel/templates/dashboard.html、panel/templates/base.html、panel/templates/logs.html、panel/templates/egress_log.html（新建）、panel/static/style.css（新增片段只加在文件末尾，不改动已有选择器）
- C 组（便携命令 + 安装器，Teammate `cli-installer`）：tools/gsm（新建）、install.sh、uninstall.sh、tools/check_egress_nft.sh（新建）、tools/build_payload.sh（只按需改）
- D 组（文档，Teammate `docs`）：README.md、panel/README_PANEL.md
- Lead 独占：TEAM_CONTRACT.md、.gitignore、合并冲突裁决、payload 重建、最终验证与 git 提交/推送

**纪律**：只改自己名下的文件；不要 git add/commit/push；不要动别人的文件；不要在仓库根目录留下临时文件/测试数据库。

## 3. 策略语义

egress_policy 单行有效策略：

```
id            INTEGER PRIMARY KEY CHECK(id=1)
enabled       INTEGER NOT NULL DEFAULT 0   -- 0=关闭（默认），1=打开
mode          TEXT    NOT NULL DEFAULT 'allowlist'  -- 'allowlist' | 'all'
domains       TEXT    NOT NULL DEFAULT ''  -- 每行一个域名或 IP，小写
block_quic    INTEGER NOT NULL DEFAULT 1   -- 打开时是否顺带丢弃 UDP/443（逼客户端走 TCP）
ipv6          INTEGER NOT NULL DEFAULT 1   -- 是否同时守护 IPv6
updated_at    TEXT
updated_by    TEXT
```

判定（真值表，必须逐条实现）：

| enabled | mode | 域名匹配 | 结果 |
| --- | --- | --- | --- |
| 0 | 任意 | 任意 | 拒绝（出站全拒绝） |
| 1 | all | 任意 | 放行 |
| 1 | allowlist | 命中 | 放行 |
| 1 | allowlist | 未命中 | 拒绝 |

- 域名匹配：大小写不敏感，精确匹配；在 domains 里写 example.com 时同时覆盖 *.example.com 子域；匹配前去掉末尾的 . 。
- domains 也允许写 IPv4/IPv6 地址字面量（如 203.0.113.10），直接进放行集合，不参与域名后缀匹配。
- 未被守护进程接管的流量（本机、内网、回环、已建立连接）永远放行。
- 打开且 allowlist 时，必须放行到系统解析器（/etc/resolv.conf 里的 nameserver）的 UDP/TCP 53，否则域名无法解析、白名单会把自己锁死；其他 53 目标拒绝。关闭时 DNS 也拒绝。

## 4. 数据面合同（nftables）

- 表：`table inet gsm_egress`（IPv4）；当 ipv6=1 时另有 `table ip6 gsm_egress`（IPv6）。ipv6=0 时**删除** ip6 表（表示不守护 v6），并在 state.detail 里注明。
- 集合：inet 表内 `set allow4 { type ipv4_addr; }`；ip6 表内 `set allow6 { type ipv6_addr; }`。
- 链：`chain gsm_out { type filter hook output priority 0; policy accept; }`（inet）；`chain gsm_out6`（ip6）。
- 规则顺序（IPv4 链，逐条）：
  1. `oifname "lo" accept`
  2. `ip daddr { <private4 列表> } accept`（必须覆盖 127/8、10/8、172.16/12、192.168/16、169.254/16、100.64/10、224/4、255.255.255.255）
  3. `ct state established,related accept`
  4. 若 enabled=1 且 block_quic：`udp dport 443 drop`
  5. 若 enabled=0：`drop`（并结束；集合保持为空）
  6. 若 enabled=1 且 mode=all：`accept`（结束）
  7. 若 enabled=1 且 mode=allowlist：`ip daddr @allow4 log limit... accept`（allow-log，见下），再 `drop`
  8. IPv6 链同构（用 `ip6 daddr` 与 `@allow6`，private6 覆盖 ::1、fc00::/7、fe80::/10、ff00::/8）。
- 日志前缀（供第 5 节日志解析）：放行 `gsm-egress-allow:`，拒绝 `gsm-egress-block:`，同一条规则带 `limit rate 12/minute burst 6`，避免刷爆内核日志。all 模式不打放行日志；关闭状态打拒绝日志。
- 失败安全：
  - 任何一次下发失败，都必须回落到全拒绝形态（等价于 enabled=0 的规则），绝不允许出现表不存在/链为空导致流量自由出去的状态。
  - 下发方式：一次 `nft -f -` 事务内完成"删旧表+建新表"（表存在时脚本以 `delete table` 开头，同一事务原子替换，nft 失败即整体回滚，不留空窗）。
  - 下发后必须校验：`nft list table inet gsm_egress` 成功且输出包含 `drop`；不满足则视为失败并记录。
  - 并发：面板与守护进程都可能下发，用 /run/gsm-egress/apply.lock 上的 fcntl 排它锁串行化。
- 守护进程：启动即下发一次（默认关闭 → 全拒绝）；此后每 2 秒读一次 DB 策略哈希并解析域名（TTL 60 秒缓存），变化则重下发；每 30 秒无条件校验表是否仍在、链规则是否完整，缺失则重下发（自愈）。
- 关闭（enabled=0）时集合必须清空（重建表即空集合）。

## 5. 访问流水（egress_log）实现约定

nft 自身不做逐连接落库，采用"内核日志采样 + 守护进程解析"：

- 守护进程以线程方式读取内核日志：优先 `/dev/kmsg`（非阻塞 + select），不可用时退回 `journalctl -k -f -o cat --no-pager`；都不可用则在 state.detail 标注"流水不可用"，功能不崩。
- 解析 `gsm-egress-block:` / `gsm-egress-allow:` 行，提取 `DST=<ip>`、`DPT=<port>`、`PROTO=<tcp|udp>`（没有 DPT 时 port=0）。
- 落库字段：at(UTC ISO8601)、domain、ip、port、action(allow|block)、reason。放行时按当前解析缓存反查域名（查不到留空），reason=allowlist；拒绝时 domain 留空，reason=egress-disabled 或 not-in-allowlist。
- 表结构：`egress_log(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, domain TEXT DEFAULT '', ip TEXT DEFAULT '', port INTEGER DEFAULT 0, action TEXT NOT NULL, reason TEXT DEFAULT '')`。
- 裁剪：只保留最新 `GSM_EGRESS_LOG_MAX`（默认 2000）行，插入时每 50 条或启动时裁剪一次；写库失败只记日志，不能让守护进程退出。
- 说明性文档要写"流水是采样记录（nft limit 限速），不是全量审计"。

## 6. Python 公共 API（app.py / gsm / 前端都可依赖）

```python
EGRESS_ENABLED = True   # 环境变量 GSM_EGRESS 为 0/false/no/off 时为 False
EGRESS_MODES = ("allowlist", "all")
RUNTIME_DIR = Path("/run/gsm-egress")   # 环境变量 GSM_EGRESS_RUNDIR 可覆盖（测试用）

def ensure_egress_schema() -> None
def get_egress_policy() -> dict
    # {"enabled": bool, "mode": str, "domains": list[str], "block_quic": bool,
    #  "ipv6": bool, "updated_at": str, "updated_by": str, "summary": str}
def save_egress_policy(*, enabled=None, mode=None, domains=None, block_quic=None,
                       ipv6=None, actor="") -> dict
def normalize_domain(raw: str) -> str        # 非法时 raise ValueError
def parse_domain_text(raw: str) -> list[str] # 去重、保序、逐行校验
def domain_matches(domain: str, rules: list[str]) -> bool  # 精确或子域
def guard_installed() -> bool
    # GSM_EGRESS_HOST=1 或 /etc/systemd/system/gsm-egress.service 存在或 /run/gsm-egress 存在
def egress_available() -> bool               # EGRESS_ENABLED and guard_installed()
def egress_runtime_dir() -> Path             # 确保存在（失败时退回临时目录）
def egress_apply_now(actor: str = "") -> dict   # 同步下发，返回 egress_apply_state()
def egress_apply_state() -> dict
    # {"ok": bool, "detail": str, "backend": "nft"|"none", "last_at": str|None,
    #  "allow4_count": int, "allow6_count": int, "needs_apply": bool,
    #  "policy_hash": str, "applied_hash": str}
def egress_summary() -> dict
    # {"enabled": bool, "mode": str, "mode_label": str, "domains": int, "backend": str,
    #  "ok": bool, "detail": str, "updated_at": str, "updated_by": str,
    #  "available": bool, "installed": bool, "block_quic": bool, "ipv6": bool,
    #  "domain_list": list[str], "dns_servers": list[str], "allow4_count": int, "allow6_count": int}
def egress_log_tail(limit: int = 50, action: str = "") -> list[dict]
    # 每行 {"at","domain","ip","port","action","reason"}；action in ("allow","block","")
def egress_log_stats() -> dict
    # {"total": int, "allow": int, "block": int, "last": str|None}
def egress_log_append(at, domain, ip, port, action, reason) -> None   # 供守护进程/测试
def egress_diag_text() -> str   # 便携命令用的纯文本
def egress_doctor() -> tuple[int, str]   # (退出码 0/1, 文本)
def egress_probe(host: str = "1.1.1.1", port: int = 443, timeout: float = 4.0) -> dict
    # {"host","port","ok": bool, "detail": str, "elapsed_ms": int}
def tcp_probe(host: str, port: int = 443, timeout: float = 4.0) -> dict
def nft_script(policy: dict, allow4: list[str], allow6: list[str], dns4: list[str], dns6: list[str]) -> str
    # 纯函数，便于单元测试；返回完整 nft -f 脚本（含 delete table 前缀的逻辑由 apply 负责）
def egress_cli(argv: list[str]) -> int   # app.py 的 egress-* 子命令委托到这里
```

- egress.py 不 import app.py（避免循环依赖），自己用 GOST_PANEL_DATA / GOST_PANEL_DB 环境变量定位数据库（默认 /etc/gost-panel、/etc/gost-panel/gsm.db）。
- 测试可替换底层执行器：所有 nft 调用集中走 `run_nft(script) -> (rc, stdout, stderr)`，测试里替换它；DNS 解析走 `resolve_host(host) -> list[str]`，测试可替换。
- 守护进程入口：`python egress.py run`（systemd 使用）。`python egress.py apply|show|set|log|probe|doctor|run` 由 egress_cli 实现；app.py 里 `egress-show/egress-set/egress-apply/egress-log/egress-probe/egress-domains/egress-doctor` 只做参数转发。

### 隐藏子命令（C 组依赖）

```
python egress.py run                                   # 守护进程（systemd 调它）
python app.py egress-show [--json]
python app.py egress-set --enable/--disable [--mode allowlist|all] [--domains-file PATH|--domains a,b]
                          [--block-quic/--no-block-quic] [--ipv6/--no-ipv6] [--actor gsm-cli] [--json]
python app.py egress-apply
python app.py egress-domains            # 每行一个域名，供 gsm allow/deny 合并
python app.py egress-log [--limit N] [--action allow|block] [--json]
python app.py egress-probe [--host H] [--port P] [--json]
python app.py egress-doctor [--json]
```

退出码：0 成功；1 参数/校验错误或本机未启用守卫；3 保存成功但数据面下发失败。

## 7. Web 路由（B 组表单必须提交到这些 action）

| 方法 | 路径 | endpoint | 权限 | 表单字段 |
| --- | --- | --- | --- | --- |
| POST | /egress/toggle | toggle_egress | 主账号 | csrf_token, enabled("1"/"0"), next |
| POST | /egress/settings | update_egress | 主账号 | csrf_token, enabled, mode, domains(多行文本), block_quic, ipv6 |
| POST | /egress/probe | probe_egress | 主账号 | csrf_token, host, port |
| GET | /egress/log | egress_log_page | 主账号 | 查询参数 action, limit |
| GET | /egress/log.json | egress_log_json | 主账号 | 同上，返回 JSON |
| POST | /egress/reapply | reapply_egress | 主账号 | csrf_token |

- 全部沿用现有 flash() + redirect() 风格，失败也重定向回去并 flash 错误；`next` 只接受以 "/" 开头的站内路径，否则回控制台。
- 非主账号访问一律拒绝（flash + redirect 到控制台），与现有 require_admin 行为一致。
- 路由仅在 `egress_available()` 为真时工作；否则 flash 提示"本机未启用出口守卫"并回控制台（避免在普通中转 VPS 上误操作 nft）。
- 保存策略后立即 `egress_apply_now()`；失败时 flash 具体 detail，且提示策略已保存、数据面仍保持拒绝（回落到全拒绝）。
- `/egress/log.json` 返回 `{"ok": true, "stats": {...}, "items": [...]}`。

## 8. 模板变量（A2 注入，B 组消费）

控制台 dashboard() 必须注入：

```python
egress = egress_summary(available 字段见第 6 节)
egress_state = egress_apply_state()
egress_stats = egress_log_stats()
egress_logs = egress_log_tail(limit=20)
```

- 变量一定存在（后端保证）；B 组对任意字段缺失都要能优雅降级（用 `egress.get('x','')` 或 Jinja `default`）。
- 全局 context_processor 额外注入 `egress_available`（bool，= egress_available()），供 base.html 导航判断。
- egress_log 里的字段名固定为 at/domain/ip/port/action/reason。
- 日志页 `egress_log_page` 注入：`egress=egress_summary()`、`egress_state`、`egress_stats`、`logs=egress_log_tail(limit, action)`、`action=当前筛选`、`limit=条数`。

## 9. 安装器要求（C 组）

环境变量：`GSM_EGRESS=1`（true/yes/on 等价）表示"这台机器是出口 VPS"；`0`/未设置时按下面规则自动判断。

- **出口模式（GSM_EGRESS=1）**：
  - 基础依赖增加 `nftables`。
  - 若用户没有显式设置 GOST_LISTEN，则默认改为 `127.0.0.1:18443`，避免和出口机上已有的 443 服务（Xray REALITY）抢端口。
  - 安装 gsm 到 /opt/gost-sni-manager/tools/gsm 并 `ln -sf` 到 /usr/local/bin/gsm；面板 systemd 加 `Environment=GSM_EGRESS=1`。
  - 写 /etc/systemd/system/gsm-egress.service（`Restart=always`、`RuntimeDirectory=gsm-egress`、ExecStart=venv python egress.py run）。
  - 启动顺序：其他服务都起来之后，**最后** `enable --now gsm-egress`（先前安装过程需要正常出站）。
  - 初始化/重复安装都必须保持切换开关的现有值（全新=关闭）；升级已打开的机器不得把 enabled 改回 0。
- **普通中转 VPS（未设置 GSM_EGRESS）**：保持现有安装行为；若检测到已有 /etc/systemd/system/gsm-egress.service，则按"升级出口机"处理：刷新 egress.py、tools/gsm、service 并重启，保留策略；否则**不安装守卫**、不动 nftables。面板 systemd 写 `Environment=GSM_EGRESS=0`（隐藏出口 UI）。
- 任何模式下都安装 gsm 命令（很小）；在没装守卫的机器上 gsm 会明确提示"未启用出口守卫"并拒绝改策略（退出码 1），不会误配 nft。
- 安装器必须在 /root/gsm.txt 增加出口状态、gsm 用法示例。
- payload：tools/build_payload.sh 需要把 tools/ 一起打进 payload（当前只打 panel/）；install.sh 的 extract_payload/install_panel_files 相应复制 panel 与 tools。
- uninstall.sh：停用并删除 gsm-egress.service、删除 nft 表（inet/ip6 gsm_egress）、删除 /usr/local/bin/gsm，其余行为不变。

## 10. 安全红线（任何一条违反都算失败）

1. 关闭状态下不得有任何到公网的出站连接被放行（IPv4/IPv6 都要）。
2. 面板与 CLI 必须使用同一条判定逻辑，不允许各写一套。
3. 不允许出现表不存在/链为空等于放行的失败模式。
4. 已有的 GOST/Caddy/面板功能不得被破坏；旧数据库必须能直接升级。
5. 子账号不得看到或修改出口开关。
6. 普通中转 VPS 安装/升级不得因该功能被挡掉出站（GOST 连落地、Caddy 申请证书、apt）。

## 11. 测试与验收

- `python panel/tests/test_apply.py` 必须继续全过。
- 新增 `python panel/tests/test_egress.py`（A1 负责），至少覆盖：
  - 默认策略（关闭、allowlist、block_quic=1、ipv6=1）与 schema 幂等；
  - 真值表四种组合 + 关闭时无 allow 规则；
  - normalize_domain/parse_domain_text/domain_matches（大小写、末尾点、子域、非法值、IP 字面量）；
  - nft_script 顺序与内容（private 放行、established、quic drop、DNS 放行、allow4、最终 drop；关闭时 end with drop 且不引用 @allow4）；
  - apply 失败回落（run_nft 返回非 0 → state.ok=False 且 detail 含"回退/全拒绝"）；
  - apply 成功后 state.json 与 allow4_count；
  - egress_log_append / tail / stats / 裁剪；
  - probe 失败路径；
  - CLI 退出码（未安装守卫时 1；保存成功下发失败 3）。
- `bash -n install.sh uninstall.sh tools/gsm tools/build_payload.sh` 全过。
- 前端：用 Jinja2 对 dashboard/base/egress_log 做一次 stub 渲染，确保字段缺失不炸。
- Lead 负责最终：跑全部测试、重建 payload、人工 diff 审核、提交并推送。

## 12. 版本

APP_VERSION 升到 `2.3.0`（A2 负责）。
