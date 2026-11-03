# GOST SNI Manager

GOST SNI Manager（简称 GSM）是一个用于中转 VPS 的轻量级 443 端口复用管理面板，支持 **自用模式** 和 **拼车模式** 两种运行方式，并支持在落地/出口 VPS 上开启 **出口访问控制**（默认拒绝该机的出站连接，只放行白名单域名或临时全放开）。

面板默认**只监听 `127.0.0.1`，只能经 SSH 隧道访问**，不对外网开放；面板本身也可以用 `gsm panel on/off` 在 SSH 里随时开关，关掉后端口映射转发照常工作。

它让 **GOST 转发**、**Caddy 反代** 和 **REALITY 多落地分流** 共存：中转 VPS 只暴露一个 443 端口，GOST 根据落地 VPS 的 REALITY `SNI / serverName` 判断流量应该转发到哪台落地 VPS；没有命中已配置 SNI 的流量，则转发到本机 Caddy，由 Caddy 处理网站、面板或其他反代服务。

## 项目功能

### 两种运行模式

- **自用模式（默认）**：GOST 命中 SNI 后直接转发到目标落地 VPS，链路最短，性能最好，适合自己使用。
- **拼车模式**：GOST 命中 SNI 后先转发到本机 `gsm-relay`，再由 `gsm-relay` 连接落地 VPS，可实现子账号、限速、流量统计、流量限制和有效期控制。
- 可在面板中随时切换运行模式，切换后自动生成 GOST 配置并重启服务。

### 443 端口复用

- 中转 VPS 实现 443 端口复用。
- GOST 监听公网 443。
- Caddy 默认监听本机 8053。
- GOST 识别 REALITY 握手里的 SNI。
- 命中 SNI 规则：按当前模式转发到落地 VPS。
- 未命中 SNI 规则：转发到 Caddy fallback。
- 避免为不同落地 VPS 暴露多个非标端口，降低非标端口转发 REALITY 协议带来的风险。

### 入站端口 → 目标端口映射

每条规则可以单独指定入站端口，实现「客户端连接 A 端口，转发到落地 B 端口」：

- 入站端口留空或与默认监听端口一致时，规则加入默认端口的 SNI 分流。
- 入站端口单独占用另一个端口时，该端口同样**只做 SNI 分流**：只有 SNI 与该端口上的规则匹配的客户端才会被转发。
- 只有主账号可以设置独立的入站端口，子账号固定使用默认端口。
- 入站端口不能低于默认监听端口（避免占用 22/80 等系统端口），也不能与面板端口、Caddy fallback 端口或本地中继保留段冲突。

例如 `:8443 → a.example.com:443`：客户端连接中转 VPS 的 8443，且必须携带 SNI `a.example.com`，落地仍是 443。配置写入后通过热重载生效，不会断开已有连接，也不会中断面板自身的访问。

#### 入站端口一定会校验 SNI

独立入站端口**不会**退化成「任意客户端都能连出去」的纯 TCP 转发。每个监听端口都会开启流量嗅探，逐个节点按 SNI 匹配：

- SNI 命中该端口的规则 → 转发到对应落地；
- SNI 不命中、没有 SNI、或根本不是 TLS 流量 → 直接关闭连接，不会转发到任何落地。

因此「在客户端里随便填一个中转端口就能走到某个落地出口」的用法是不成立的：端口号本身不携带任何身份，只有 SNI 才能选中线路。**新增线路请优先复用默认端口的 SNI 分流，不要让每条规则各占一个端口。**

> 说明：默认端口上的「未命中 SNI 的流量」仍会进入本机 Caddy（fallback），这是给面板域名和网站反代用的，不会通往任何落地 VPS。独立入站端口没有 fallback。

如果改写后的配置被 GOST 拒绝（例如端口被占用导致监听失败），面板会自动回滚到上一份可用配置，避免整台机器的转发全部中断。

### 拼车面板

拼车功能仅在“拼车模式”下启用：

- 安装时自动生成主账号用户名和强密码。
- 主账号可创建多个子账号。
- 主账号可限制子账号最多添加多少条转发规则。
- 主账号可限制子账号每条规则的限速。
- 主账号可限制子账号每条规则的流量。
- 主账号可限制子账号账号总流量。
- 主账号可设置子账号有效期。
- 子账号过期、禁用、超出流量后，其规则自动失效。
- 子账号可自行添加、编辑、删除自己的转发规则。
- 子账号不能修改主账号分配的限速、流量、规则数量和有效期。
- 子账号可查看自己的已用流量、剩余流量和到期时间。

### 运维功能

- 面板添加/修改规则后自动生成 `/etc/gost/config.yaml`。
- 自用模式下自动重启 `gost.service`。
- 拼车模式下自动启用并重启 `gost.service` 和 `gsm-relay.service`。
- 支持查看 GOST、本地中继、Caddy 日志。
- 支持查看实时 GOST 配置，方便排错。
- 一键脚本自动安装依赖、GOST v3、Caddy 和管理面板。
- 面板端口自动选择 `50000-55000` 之间未占用端口。
- **面板默认只监听 `127.0.0.1`，只能经 SSH 隧道访问**，不对外网开放（见下文「面板只从 SSH 隧道访问」）。
- 可用 `gsm panel on/off/status` 在 SSH 里直接开关面板；面板关掉后端口映射转发照常工作。
- 面板域名（Caddy 反代）属于**可选的高级用法**；即使配了域名，也只能经 SSH 隧道访问。
- 登录信息保存到 `/root/gsm.txt`。

### 出口访问控制（出口 VPS 专用）

如果这台 VPS 是**落地/出口机**（客户端最终的出口），可以在安装时开启出口访问控制：对该机的**出站访问默认拒绝**，只在你明确放行时才允许连外网。

- **默认拒绝**：关闭状态下，IPv4/IPv6 的出站连接（含 DNS）全部被丢弃。判定与强制都在常驻守卫里，不依赖面板/CLI 是否在场：面板停掉、数据库被删、守卫进程被杀，只要 nftables 表已经下发过，出站仍然是拒绝的。
- **两种放行方式**：`allowlist`（只放行域名/IP 白名单）或 `all`（全部放行，相当于临时关闭拦截，但保留开关与流水）。
- **为什么默认关闭**：普通中转 VPS 需要正常出站（GOST 连落地、Caddy 申请证书、apt 升级）。只有明确把这台机器当出口机时才需要拦出站，所以必须显式打开，避免把中转机的必要连接一起挡掉。
- **和中转 VPS 的关系**：出口访问控制只运行在出口 VPS 上，管的是"这台机器能连出去什么"；中转 VPS 的 443 复用、SNI 分流、拼车中继等入站逻辑完全不受影响。普通中转 VPS 的安装默认**不安装、不启用**守卫。

## 工作原理

### 自用模式，默认

```text
客户端连接中转 VPS:443
        ↓
GOST 监听 443，并读取 TLS/REALITY 握手里的 SNI
        ↓
如果 SNI 命中规则：GOST 直接连接到落地 VPS 的域名或 IP:端口
        ↓
落地 VPS 的 Xray REALITY 继续处理连接

如果 SNI 未命中规则：GOST 转发到 127.0.0.1:8053
        ↓
Caddy 处理网站、管理面板或其他反代服务
```

自用模式链路最短：

```text
客户端 → GOST:443 → 落地 VPS:443
```

### 拼车模式

```text
客户端连接中转 VPS:443
        ↓
GOST 监听 443，并读取 TLS/REALITY 握手里的 SNI
        ↓
如果 SNI 命中规则：GOST 转发到本机 gsm-relay 中继端口
        ↓
gsm-relay 统计流量、执行限速、检查账号是否过期或超量
        ↓
gsm-relay 连接到落地 VPS 的域名或 IP:端口

如果 SNI 未命中规则：GOST 转发到 127.0.0.1:8053
        ↓
Caddy 处理网站、管理面板或其他反代服务
```

拼车模式链路为：

```text
客户端 → GOST:443 → gsm-relay 本机中继 → 落地 VPS:443
```

## 轻量说明

GSM 本身比较轻量。默认自用模式下，`gsm-relay` 不启动，资源占用更低。

```text
GOST：负责 443 SNI 分流
GSM Panel：Flask + Gunicorn 管理面板
Caddy：负责 fallback 网站/面板反代
gsm-relay：仅拼车模式启用，用于统计流量和执行限速
gsm-egress：仅出口 VPS 启用，负责出站访问控制的策略下发与流水记录（基于 nftables）
```

大致资源占用如下，不同系统和版本会有差异：

```text
项目面板代码：约几 MB
Python 虚拟环境：约 35-90 MB
GOST 二进制：约 20-50 MB
Caddy 及依赖：约 30-120 MB
SQLite 数据库：初始很小，随规则和流量统计增长
脚本依赖：采用 --no-install-recommends，尽量避免安装 gcc、g++、build-essential 等编译组件
总磁盘占用：通常约 120-260 MB

自用模式空闲内存：通常约 60-160 MB
拼车模式空闲内存：通常约 80-240 MB
```

转发性能主要取决于中转 VPS 线路、带宽、CPU、并发连接数以及中转到落地的网络质量。GOST 在这里只做 TCP 透传和 SNI 分流，不解密 REALITY 流量。自用模式性能最好；拼车模式会多经过本机中继，换来限速、流量统计和账号管理能力。

## 一键安装

在中转 VPS 上执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Qin52/realm-443/master/install.sh)
```

脚本会自动安装轻量依赖、GOST v3、Caddy 和面板。安装结束后按屏幕提示，用 SSH 隧道访问面板（见下文「面板只从 SSH 隧道访问」）。

### 访问方式：SSH 隧道（默认，推荐）

面板默认只监听 `127.0.0.1`，不对外网开放。安装脚本会自动选择 `50000-55000` 之间未占用的高位端口作为面板端口，请在**你自己的电脑**上建立隧道：

```bash
ssh -L <面板端口>:127.0.0.1:<面板端口> root@<中转VPS IP>
```

保持这个 SSH 会话不断，然后在浏览器打开：

```text
http://127.0.0.1:<面板端口>/
```

面板端口、用户名、密码都记录在服务器的 `/root/gsm.txt` 里，SSH 登录后 `cat /root/gsm.txt` 即可查看。

### 面板域名（可选的高级用法）

> ⚠️ 高级用法：即使配置了面板域名，Caddy 站点也是 `bind 127.0.0.1` 只监听本机，**域名同样只能经 SSH 隧道访问**，不会因为这个域名就把面板暴露到公网。日常使用不需要配置域名。

先将面板域名解析到中转 VPS，例如：

```text
admin.example.com -> 中转 VPS IP
```

安装时选择配置面板域名，然后输入：

```text
admin.example.com
```

访问时仍然先建隧道，把本地端口指向本机的 443 或面板端口：

```bash
ssh -L <面板端口>:127.0.0.1:<面板端口> root@<中转VPS IP>
```

> 提示：面板域名证书由 Caddy 通过 HTTP-01 申请，安装时 80 端口需要能从公网访问；证书申请与续期都需要正常出站。

## 在出口 VPS 上安装（出口访问控制）

普通中转 VPS 按上面的「一键安装」操作即可。如果这台机器是**落地/出口 VPS**，并且希望管住它的出站访问，安装时加 `GSM_EGRESS=1`：

```bash
GSM_EGRESS=1 bash <(curl -fsSL https://raw.githubusercontent.com/Qin52/realm-443/master/install.sh)
```

出口模式会额外做这些事：

- 安装 `nftables` 依赖，并写入常驻守卫服务 `gsm-egress.service`（最后启动，避免挡住安装过程本身的必要出站）。
- 如果**没有**显式设置 `GOST_LISTEN`，GOST 默认监听改为 `127.0.0.1:18443`，避免和出口机上已经占用 443 的 Xray REALITY 抢端口。需要改回其他地址时显式传入，例如：

  ```bash
  GSM_EGRESS=1 GOST_LISTEN=":443" bash <(curl -fsSL https://raw.githubusercontent.com/Qin52/realm-443/master/install.sh)
  ```

- 安装便携命令 `gsm`，并把出口状态和用法写入 `/root/gsm.txt`。
- 首次安装后开关默认是**关闭**（出站全拒绝）；需要时登录面板或 SSH 执行 `gsm on` 打开。

普通中转 VPS（不设置 `GSM_EGRESS`）的安装行为保持不变，**不安装守卫、不动 nftables**，面板里也不会出现出口访问卡片；`gsm` 命令仍会安装，但在没装守卫的机器上会明确提示"未启用出口守卫"并拒绝改策略，不会误配防火墙。如果检测到机器上已有 `gsm-egress.service`，会按"升级出口机"处理：刷新守卫程序与服务并保留已保存的策略（已打开的开关不会在升级时被改回关闭）。

> 提示：安装过程中的 `apt`、下载 GOST/Caddy、申请证书都需要正常出站，所以守卫是**全部安装完成之后**才启动的。

## 安装完成后的信息

安装结束后脚本会显示访问方式（SSH 隧道命令）、用户名和密码，例如：

```text
面板端口: 54735（仅监听 127.0.0.1）
SSH 隧道: ssh -L 54735:127.0.0.1:54735 root@中转VPS_IP
浏览器打开: http://127.0.0.1:54735/
用户名: gsm_xxxxxxxx
密码: 随机强密码
登录信息已保存: /root/gsm.txt
```

出口 VPS（`GSM_EGRESS=1`）还会额外提示出口访问控制的当前状态和 `gsm` 用法。

默认（未配置面板域名）会显示类似：

```text
面板端口: 54735（仅监听 127.0.0.1）
SSH 隧道: ssh -L 54735:127.0.0.1:54735 root@中转VPS_IP
浏览器打开: http://127.0.0.1:54735/
用户名: gsm_xxxxxxxx
密码: 随机强密码
登录信息已保存: /root/gsm.txt
```

> 面板不监听公网，公网直连面板端口会被拒绝。

请立即保存用户名和密码。后续也可以在 VPS 上查看：

```bash
cat /root/gsm.txt
```

## 面板只从 SSH 隧道访问

面板（网页）**默认只监听 `127.0.0.1`**，不监听公网。想从自己的电脑打开面板，用 SSH 端口转发把服务器上的本机端口映射到你自己的电脑：

```bash
# 面板端口以 /root/gsm.txt 中记录的为准，下面以 54735 为例
ssh -L 54735:127.0.0.1:54735 root@<中转VPS IP>
```

保持这条 SSH 连接不断，然后在浏览器打开：

```text
http://127.0.0.1:54735/
```

要点：

- 隧道建立后，浏览器里访问的是「本机」，所以不会触发来源限制；关闭 SSH 会话，面板页面随即打不开（服务器上的转发业务不受影响）。
- 从公网直接访问面板端口会被拒绝（面板根本没有监听 `0.0.0.0`）。
- 面板页面上方会持续提示当前访问方式，并显示「当前来源：本机 / SSH 隧道」或「非本机来源」。看到「非本机来源」说明你不是经隧道进来的，端口映射等敏感操作会被拒绝，请改用 SSH 隧道。
- 子账号只看得到自己的规则，不需要（也不会）看到这条提示。
- 手机/平板没有 SSH 客户端时，可先用电脑建隧道，或用带端口转发功能（VNC/SSH 类）的客户端；不建议为此把面板开放到公网。

如果确实需要临时公网监听，见下面「用指令开关面板」里的 `gsm panel remote on`，它会打印醒目警告，且仍会拒绝未经允许的远程来源。

## 用指令开关面板

面板开关全部在 SSH 里用 `gsm` 完成，不需要打开网页，x-ui 式的「SSH 里管一切」：

| 命令 | 说明 |
| --- | --- |
| `gsm panel on` | 启动面板并设为开机自启；只监听 `127.0.0.1`（幂等，重复执行不报错） |
| `gsm panel off` | 停掉面板并取消开机自启；**端口映射转发继续工作**，只是暂时没有网页可改 |
| `gsm panel status` | 查看服务状态、监听地址、是否允许远程、最近一次开关时间与操作者（服务在跑退 `0`，没跑退 `1`） |
| `gsm panel remote on` | 显式允许监听公网（`0.0.0.0`），会打印醒目警告 |
| `gsm panel remote off` | 恢复只监听 `127.0.0.1` |

典型用法：

```bash
# 平时关掉面板，减少暴露面；需要改规则时再打开
gsm panel off
gsm panel status

# 改完再关掉
gsm panel off
```

关键点：

- **关闭面板不影响转发**：`gost`、`gsm-relay`、`gsm-egress`、`caddy` 都照常运行，已经配置的端口映射和 SNI 分流一秒都不受影响，只是「没有网页可以改规则」。
- 面板端口、用户名、密码都在 `/root/gsm.txt`。
- 面板绑定地址与是否允许远程，记录在 `/etc/gost-panel/panel.conf`，`gsm panel` 会同步修改它。
- 如果面板关了又忘了开，直接在 SSH 里 `gsm panel on` 即可。

## 使用示例

假设有 3 台 VPS：

### 1. 中转 VPS A

```text
中转 VPS：A
中转域名：a.example.com
网站/反代域名：aweb.example.com

a.example.com 已解析到 VPS A 的 IP
aweb.example.com 已解析到 VPS A 的 IP
```

### 2. 落地 VPS B

```text
落地 VPS：B
落地域名：b.example.com

b.example.com 已解析到 VPS B 的 IP
VPS B 已安装 Xray
VPS B 已配置 REALITY
REALITY SNI / serverName：apple.com
REALITY 连接端口：443
```

### 3. 落地 VPS C

```text
落地 VPS：C
落地域名：c.example.com

c.example.com 已解析到 VPS C 的 IP
VPS C 已安装 Xray
VPS C 已配置 REALITY
REALITY SNI / serverName：visa.cn
REALITY 连接端口：443
```

### 4. 在中转 VPS A 安装 GSM 面板

在 VPS A 执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Qin52/realm-443/master/install.sh)
```

安装完成后登录面板。默认是“自用模式”。

### 5. 添加落地 B 的规则

在面板中添加：

```text
SNI / serverName：apple.com
目标域名或 IP：b.example.com
目标端口：443
备注：落地 B
```

### 6. 添加落地 C 的规则

在面板中添加：

```text
SNI / serverName：visa.cn
目标域名或 IP：c.example.com
目标端口：443
备注：落地 C
```

### 7. 添加 Caddy 反代规则，可选

如果你希望 `aweb.example.com` 明确进入本机 Caddy，可以在面板中添加：

```text
SNI / serverName：aweb.example.com
目标域名或 IP：127.0.0.1
目标端口：8053
备注：反代
```

说明：默认情况下，未匹配到任何规则的 SNI 也会进入 `127.0.0.1:8053`。所以这条规则不是必须的，但添加后逻辑更直观。

Caddy 中仍需要你自行配置 `aweb.example.com` 的具体反代规则。

### 8. 客户端连接方式

连接落地 VPS B 时，把客户端里的连接地址改成中转 VPS A：

```text
原连接地址：b.example.com
新连接地址：a.example.com
端口：443
SNI / serverName：apple.com
```

连接落地 VPS C 时：

```text
原连接地址：c.example.com
新连接地址：a.example.com
端口：443
SNI / serverName：visa.cn
```

访问 Caddy 反代服务时，直接访问：

```text
https://aweb.example.com/
```

## 拼车模式使用方法

主账号登录后，在“运行模式”中切换为“拼车模式”。切换后，面板会启用 `gsm-relay.service`，并让 GOST 将命中的 SNI 先转到本机中继。

主账号可以创建子账号，例如：

```text
用户名：user01
最多规则：2 条
每条规则限速：30 Mbps
每条规则流量：100 GB
账号总流量：200 GB
有效期：2026-12-31 23:59
```

子账号登录后可以自己添加规则，但不能修改限速、流量和有效期。子账号能看到：

```text
账号到期时间
账号总流量
已用流量
剩余流量
每条规则限速
每条规则流量
```

如果账号过期、账号总流量用完、某条规则流量用完，相关转发会自动停止。

## 出口访问控制使用方法

出口访问控制只在通过 `GSM_EGRESS=1` 安装的出口 VPS 上出现。主账号登录面板后有两种操作方式，二者改的是同一份策略、走同一条判定逻辑。

### 面板操作

控制台（Dashboard）里有「出口访问」卡片，可以看到当前开关、模式、后端状态、最近流水，并完成：

- **打开/关闭**：一键切换 `enabled`。关闭即出站全拒绝（含 DNS）。
- **模式与白名单**：选择 `allowlist`（只放行白名单）或 `all`（全部放行），编辑域名/IP 白名单（每行一个），可同时设置 `block_quic`、`ipv6`。
- **探测**：对指定 `host:port` 做一次 TCP 探测，验证当前策略下能不能连出去。
- **重新下发**：手动让守卫把当前策略重新应用到 nftables（自愈用）。
- **查看流水**：点击卡片里的入口进入 `/egress/log`，按放行/拦截筛选，查看最近的访问流水（默认 20 条，最多 2000 条；日志由系统日志服务限速，落库的是采样记录，不是全量审计）。

保存策略后面板会立即下发；如果下发失败，策略仍然保存，数据面会回落到"全拒绝"，卡片上会显示具体原因。

> 子账号看不到也改不了出口开关，只有主账号可以操作。

### 便携命令 gsm

SSH 登录出口 VPS 后可以直接用 `gsm`，不需要进面板：

| 命令 | 说明 |
| --- | --- |
| `gsm` / `gsm status` | 查看状态：开关、模式、域名数、数据面下发结果 |
| `gsm on [--mode all\|allowlist] [--allow a.com,b.com]` | 打开出口访问。不写 `--mode` 时沿用当前模式；`--allow` 是逗号分隔的便利写法，先补域名再打开开关 |
| `gsm off` | 关闭出口访问（关闭是默认态，关闭即出站全拒绝） |
| `gsm allow a.com b.com` | 追加放行域名（不改开关状态；自动去重、排序后写回） |
| `gsm deny a.com` | 删除放行域名（不改开关状态） |
| `gsm domains` | 列出当前放行域名 |
| `gsm test [host] [port]` | 出站连通性探测，默认 `1.1.1.1 443` |
| `gsm log [-n N] [--allow\|--block]` | 查看访问流水（采样记录，非全量审计），`-n20` 这种粘连写法也支持 |
| `gsm doctor` | 自检：守卫服务、nft 表、DNS、探测等 |
| `gsm install-cli` | 把 `gsm` 安装到系统（需要 root） |
| `gsm version` | 显示版本 |
| `gsm help` | 显示帮助 |

命令都有一到两个简写别名，例如 `gsm show` / `gsm enable` / `gsm disable` / `gsm remove` / `gsm list` / `gsm probe` / `gsm diag`。

退出码与 `gsm` 调用到的面板子命令一致：`0` 成功；`1` 参数错误或本机未启用出口守卫；`3` 已保存但数据面下发失败。

**未安装守卫时**的分工：`status` / `log` / `doctor` 退 `0`（只读，照常打印 `available=False`），`on` / `off` / `allow` / `deny` / `domains` 退 `1`（明确拒绝读改策略），`test` 探测不通退 `1`。

补充几种常见情形：

- `gsm doctor` 只在发现真错误时退 `1`，只有警告时退 `0`。
- `gsm test <host> [port]` 默认探测 `1.1.1.1 443`，通退 `0`、不通退 `1`。
- 参数错误一律退 `1`（与守卫是否可用无关），例如 `gsm on --mode bogus`、`gsm log -n 0`、未知子命令。
- 退 `3` 时 `gsm` 会额外区分两种情况：`gsm on` 的第一步（写白名单）失败会提示"开关尚未改动，出口访问保持原状态"；改开关本身失败会提示"开关已改动（策略已保存），但数据面下发失败"。

`gsm` 不依赖 tty、不依赖 `jq`，只用 bash 和系统自带命令，所以在最小化系统上也能直接跑。可用环境变量：

```bash
GSM_APP_DIR=/opt/gost-sni-manager   # 应用目录，默认就是它
GSM_PYTHON=/opt/gost-sni-manager/venv/bin/python   # 指定解释器，默认按 venv → python3 → python 探测
NO_COLOR=1                          # 关闭彩色输出
```

常用示例：

```bash
# 1) 打开白名单模式，并放行 GitHub 和 Debian apt 源
gsm on
gsm allow github.com deb.debian.org

# 2) 临时全部放行（例如装软件、跑一次性脚本），完事再收回白名单
gsm on --mode all
gsm off

# 3) 出问题时先自检，再看被拦截的流水
gsm doctor
gsm log -n 50 --block
```

说明：

- 域名白名单按**解析出的 IP** 放行。守卫每 2 秒检查策略变化、并按 60 秒级 TTL 重新解析域名，DNS 记录变了会自动跟随，不需要手动重下发。
- 打开且 `allowlist` 时，会额外放行到系统解析器（`/etc/resolv.conf` 里的 nameserver）的 53 端口，否则域名无法解析、白名单会把自己锁死；其他 53 目标仍然拒绝。关闭时 DNS 也拒绝。
- 本机、内网、回环和已建立连接的流量永远放行。
- `block_quic` 打开时会丢弃 UDP/443，逼客户端走 TCP，避免 QUIC 绕过基于 IP 的放行集合造成体验问题。
- 规则里的 `log` 只带前缀、后面紧跟 verdict，**不加 `limit`**：nft 的 `log ... limit` 写在同一条规则里会让该规则的 verdict 失效（真机实测不再拦截），所以日志限速交给系统日志服务。落库的流水因此是采样记录。
- `gsm on` 成功后会顺带打印一次状态；`gsm allow` / `gsm deny` 会回显最新的完整清单，方便直接核对。

## 出口访问控制：行为与安全说明

### 判定真值表

| 开关 | 模式 | 域名匹配 | 结果 |
| --- | --- | --- | --- |
| 关闭 | 任意 | 任意 | 拒绝（出站全拒绝，含 DNS） |
| 打开 | all | 任意 | 放行 |
| 打开 | allowlist | 命中白名单 | 放行 |
| 打开 | allowlist | 未命中 | 拒绝 |

域名匹配大小写不敏感、按精确域名或子域（白名单写 `example.com` 同时覆盖 `*.example.com`），末尾的点会被忽略；白名单里也可以直接写 IPv4/IPv6 地址，这些地址不参与域名后缀匹配。

### 数据面与失败安全

- 强制点是 nftables：`table inet gsm_egress`（链 `gsm_out`）与 `table ip6 gsm_egress`（链 `gsm_out6`，`ipv6=0` 时不创建）。
- 放行名单用 nft 集合（`allow4` / `allow6`）承载，域名解析成 IP 后热更新集合，不 reload 防火墙、不断已有连接，也不需要重启服务。
- 每次下发都在一个 nft 事务里完成"删旧表 + 建新表"，失败整体回滚；下发后还会校验表和规则确实存在，校验不通过同样按失败处理。
- **任何一次下发失败都会回落到全拒绝形态**：脚本先装拒绝形态再补放行规则，校验不通过也按失败处理，不会留下"表存在但链为空 = 放行"的中间状态。
- **前提是表已经成功下发过一次**。nftables 规则不跨重启保留，且守卫启动前的窗口里没有表。所以：正常运行的机器上，守卫崩溃/重启期间出站仍然拒绝（表还在）；但机器刚开机、守卫还没跑起来的短暂窗口里，出站是放行的——这正是守卫 `Restart=always` 和安装器把它安排在最后启动的原因。
- 守卫启动即下发一次，之后每 2 秒读一次策略、每 30 秒无条件校验表是否还在，缺失就重新下发（自愈）。
- 面板与 `gsm` 命令用的是同一套策略逻辑和同一个下发入口，不会出现两套判定。

### 流水与可观测性

- 放行/拦截由内核日志记录，前缀分别是 `gsm-egress-allow:` 和 `gsm-egress-block:`。规则里只有 `log prefix ...` 紧跟 verdict，**不带 `limit`**——nft 的 `log` 与 `limit` 写在同一条规则里会让 verdict 失效（真机实测该规则不再拦截，等于漏放），所以限速只能交给内核/系统日志服务（journald、rsyslog 等）自身的限速。
- 守卫从 `/dev/kmsg`（不可用时退回 `journalctl -k -f`）读取这些行，解析出目标 IP/端口/协议后写入 `egress_log` 表，只保留最近 2000 行（可用 `GSM_EGRESS_LOG_MAX` 调整）。
- **流水是采样记录，不是全量审计**：日志量受系统日志服务的限速约束，被丢弃的日志行不会出现在流水里，流量统计不能以流水为准。

### 恢复与排错

```bash
gsm doctor                    # 一键自检：服务、nft 表、策略、DNS、最近下发结果
gsm off                       # 出问题时最直接的回退：恢复出站全拒绝
journalctl -u gsm-egress -f   # 看守卫日志
systemctl restart gsm-egress  # 重启守卫（启动时会重新下发一次策略）
nft list table inet gsm_egress  # 直接看数据面规则
```

守卫进程挂掉（已经下发过一次的情况下）不影响拒绝本身：nft 表还在，出站仍然是拒绝的；此时面板和 SSH 仍然能进（本机/回环/已建立连接永远放行），可以登录上去 `systemctl restart gsm-egress` 或 `gsm off`。

反过来说，**不要手工删表或 flush 链**：守卫每 30 秒会校验并重建，但在那之前出站是放行的窗口。机器重启后守卫会自动重新下发，规则不跨重启保留，这是 `Restart=always` 的作用。




## 面板操作逻辑

### 自用模式

1. 添加 SNI 和转发目标。
2. 面板保存规则到 SQLite 数据库 `/etc/gost-panel/gsm.db`。
3. 面板生成 `/etc/gost/config.yaml`。
4. GOST 监听公网 443。
5. 命中 SNI 的流量由 GOST 直接转发到目标落地 VPS。
6. 未命中 SNI 的流量转发到 Caddy fallback。

### 拼车模式

1. 主账号切换为拼车模式。
2. 添加 SNI 和转发目标。
3. 面板为每条规则分配一个本机中继端口。
4. 面板生成 `/etc/gost/config.yaml`。
5. GOST 继续监听公网 443。
6. 命中 SNI 的流量转发到本机限速中继。
7. 本机限速中继统计流量、执行限速和流量限制。
8. 中继再连接到真正落地 VPS。
9. 未命中 SNI 的流量转发到 Caddy fallback。

## 生成的 GOST 配置示例

自用模式：

```yaml
services:
- name: reality-443
  addr: ":443"
  handler:
    type: tcp
    metadata:
      sniffing: true
  listener:
    type: tcp
  forwarder:
    nodes:
    - name: sni-apple-com-xxxxxx
      addr: b.example.com:443
      matcher:
        rule: Host(`apple.com`)
    - name: sni-visa-cn-xxxxxx
      addr: c.example.com:443
      matcher:
        rule: Host(`visa.cn`)
    - name: caddy-fallback
      addr: 127.0.0.1:8053
  selector:
    strategy: fifo
```

拼车模式：

```yaml
services:
- name: reality-443
  addr: ":443"
  handler:
    type: tcp
    metadata:
      sniffing: true
  listener:
    type: tcp
  forwarder:
    nodes:
    - name: sni-apple-com-xxxxxx
      addr: 127.0.0.1:56001
      matcher:
        rule: Host(`apple.com`)
    - name: sni-visa-cn-xxxxxx
      addr: 127.0.0.1:56002
      matcher:
        rule: Host(`visa.cn`)
    - name: caddy-fallback
      addr: 127.0.0.1:8053
  selector:
    strategy: fifo
```

`selector.strategy: fifo` 是必须的：GOST 默认的 round 策略在嗅探出 SNI **之前**就选好节点，一旦选中的节点 SNI 不匹配，客户端仍然会被送到错误的落地。fifo 按顺序取第一个匹配成功的节点，才是真正的按 SNI 分流。

## 常用命令

查看登录信息：

```bash
cat /root/gsm.txt
```

查看服务状态：

```bash
systemctl status gost --no-pager
systemctl status gost-panel --no-pager
systemctl status caddy --no-pager
systemctl status gsm-relay --no-pager
```

查看日志：

```bash
journalctl -u gost -f
journalctl -u gost-panel -f
journalctl -u caddy -f
journalctl -u gsm-relay -f
```

重启服务：

```bash
systemctl restart gost gost-panel caddy
systemctl restart gsm-relay
```

查看 GOST 配置：

```bash
cat /etc/gost/config.yaml
```

查看数据库文件：

```bash
ls -lh /etc/gost-panel/gsm.db
```

查看/开关面板（不需要打开网页）：

```bash
gsm panel status          # 服务状态、监听地址、是否允许远程、最近一次开关
gsm panel off             # 关掉面板（端口映射继续工作）
gsm panel on              # 重新打开面板（只监听 127.0.0.1）
gsm panel remote on       # 临时允许公网监听（会打印警告）
gsm panel remote off      # 收回公网监听
```

查看出口访问状态（出口 VPS）：

```bash
gsm status
gsm doctor
```

出口守卫服务与数据面：

```bash
systemctl status gsm-egress --no-pager
journalctl -u gsm-egress -f
nft list table inet gsm_egress
nft list table ip6 gsm_egress
```

## 常见问题

### 打不开面板 / 浏览器提示无法连接

先确认面板是否在运行、监听在哪里：

```bash
gsm panel status
```

常见原因：

- **面板被 `gsm panel off` 关了**：SSH 里执行 `gsm panel on` 重新打开。关闭面板不影响端口映射转发。
- **忘了建 SSH 隧道**：面板只监听 `127.0.0.1`，公网直连一定失败。先在本机执行 `ssh -L <面板端口>:127.0.0.1:<面板端口> root@<服务器IP>`，再访问 `http://127.0.0.1:<面板端口>/`。
- **隧道端口写错**：面板端口以 `cat /root/gsm.txt` 里的记录为准；本地端口和远端端口要一致时才用同一个数字。
- **看到「面板仅允许通过 SSH 隧道访问」**：说明你是从公网直连进来的，改用隧道访问；确需公网直连才用 `gsm panel remote on`（不推荐）。

### 面板关了，端口映射会断吗

不会。`gsm panel off` 只停掉网页服务，`gost`、`gsm-relay`、`gsm-egress`、`caddy` 都不受影响，已配置的规则继续转发。需要改规则时再 `gsm panel on`。

### 提示 curl: command not found

极简 Debian 系统可能没有预装 `curl`，而 raw 一键安装命令本身依赖 curl。先执行：

```bash
apt update && apt install -y curl ca-certificates
```

然后再运行一键安装命令。

### 打开出口访问后 apt / curl / git 不通

这是预期行为：默认拒绝下所有未放行的出站（含 DNS）都会被丢弃。按需放行：

```bash
gsm allow deb.debian.org security.debian.org github.com
gsm domains          # 确认白名单
gsm test github.com 443
```

如果只是临时装东西，可以先 `gsm on --mode all`，装完再 `gsm off` 或切回 `allowlist`。

注意：只放行域名不够时，还需要放行它实际连接的目标（CDN、对象存储、跳转后的镜像站等）。用 `gsm log --block` 看被拦截的目标，再决定是否放行。

### 面板域名证书续期被挡

Caddy 续期走的是 ACME（Let's Encrypt），需要从出口机出站到 `acme-v02.api.letsencrypt.org`。如果面板域名挂在出口 VPS 上，请在白名单里放行：

```bash
gsm allow acme-v02.api.letsencrypt.org
```

Caddy 可能还会用到 HTTP-01 校验的回连，以及 OCSP 查询；如果续期日志里出现其他被拦的域名，按 `gsm log` 里的拦截记录一并放行。实在赶时间可以先 `gsm on --mode all` 完成续期，再切回白名单。

### 出口机重复安装/升级会不会把开关关掉

不会。安装器对出口机是幂等的：重复执行或升级只会刷新守卫程序、服务与 `gsm` 命令，**保留数据库里已有的策略**。全新安装的默认状态是关闭；已经打开的机器升级后仍然保持打开（含白名单、模式、`block_quic`、`ipv6` 设置）。

如果升级后想确认状态：

```bash
gsm status
```

### gsm 提示"未启用出口守卫"

说明这台机器不是出口机（没有 `GSM_EGRESS=1` 安装，或者没有 `gsm-egress.service`）。`gsm` 会拒绝改策略、也不会碰 nftables。要在这台机器上启用出口访问控制，用出口模式重新安装：

```bash
GSM_EGRESS=1 bash <(curl -fsSL https://raw.githubusercontent.com/Qin52/realm-443/master/install.sh)
```

### Caddy 源或 keyring 报错

新版脚本不再使用 Caddy Cloudsmith 源，默认优先从系统仓库安装 Caddy；如果系统仓库不可用，会自动改用 GitHub 二进制安装。

如果旧脚本残留过 Caddy 源，可以先清理：

```bash
rm -f /etc/apt/sources.list.d/caddy-stable.list
rm -f /etc/apt/sources.list.d/caddy*.list
rm -f /usr/share/keyrings/caddy-stable-archive-keyring.gpg
apt update
```

## 卸载

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Qin52/realm-443/master/uninstall.sh)
```

卸载脚本会删除面板、数据库、GOST 配置和 systemd 服务，在出口 VPS 上还会：停用并删除 `gsm-egress.service`、删除 nftables 表（`inet gsm_egress` 与 `ip6 gsm_egress`）、删除 `/usr/local/bin/gsm`。之后会询问是否删除 GOST 二进制和 Caddy 软件包。

## 注意事项

1. 中转 VPS 的 443 必须由 GOST 监听。
2. Caddy 的所有站点（面板域名站和 fallback 站）都用 `bind 127.0.0.1` 监听 8053，**只对本机开放**，只能经 GOST:443 进入，不能从公网直连绕过 SNI 校验。面板域名证书由 Caddy 通过 HTTP-01 申请，安装时 80 端口需要能从公网访问。
3. 面板默认只监听 `127.0.0.1`，请用 SSH 隧道访问；面板域名属于可选的高级用法，即使配了域名也只能经隧道访问。网站域名需要解析到中转 VPS。
4. 落地 VPS 的 REALITY SNI 必须和面板里的 SNI 一致。
5. 客户端连接地址改成中转 VPS，SNI 仍保持落地 VPS 的 REALITY SNI。
6. 自用模式性能最好，但不做限速和流量统计。
7. 拼车模式支持限速和流量统计，但会多经过本机中继，高速大流量场景请给中转 VPS 留足 CPU 和内存。
8. 面板不会公网监听；想临时放开用 `gsm panel remote on`（会打印警告），用完 `gsm panel remote off` 收回。不需要面板时用 `gsm panel off` 关掉，端口映射转发照常工作。
9. 出口访问控制只装在使用 `GSM_EGRESS=1` 安装的出口 VPS 上，管的是该机的**出站**；中转 VPS 不要开启，否则会把 GOST 连落地、Caddy 申请证书等必要出站一起挡掉。
10. 出口 VPS 的 GOST 默认监听 `127.0.0.1:18443`（避免和 443 上的 Xray REALITY 冲突）；如果你确实要它直接监听 443，安装时用 `GOST_LISTEN` 显式指定。
11. 出口访问的开关状态保存在 SQLite（`/etc/gost-panel/gsm.db` 的 `egress_policy` 表），强制点在 nftables。不要手工 `nft flush` 或删除 `gsm_egress` 表：删掉表等于去掉拦截，守卫会在 30 秒内重建，但这段窗口是放行的；nft 规则也不跨重启保留，靠守卫启动时重新下发。
12. 想临时停掉拦截用 `gsm off`（保留守卫服务、恢复默认拒绝），不要只 `systemctl stop gsm-egress`：守卫停了但表还在，出站仍然是拒绝的，此时改策略只能靠 `gsm`/面板重新下发，需要先 `systemctl restart gsm-egress`。彻底移除用卸载脚本。
