# Panel runtime files

这是 GOST SNI Manager 的面板程序目录。

- `app.py`：面板主程序，负责账号、规则、模式切换、配置生成和服务重启。
- `relay.py`：拼车模式使用的本地 TCP 中继，负责流量统计、限速、流量限制和过期规则拦截。
- `wsgi.py`：Gunicorn 入口。
- `tests/test_apply.py`：配置生成、端口校验与热重载/回滚自检。
- `templates/`：页面模板。
- `static/style.css`：页面样式。
- `requirements.txt`：Python 依赖。

默认自用模式下，规则由 GOST 直接转发到目标落地 VPS；拼车模式下，规则先进入本机中继再连接落地 VPS。

每条规则都可以指定入站端口：留空或与默认监听端口一致时按 SNI 分流；单独占用另一个端口时，该端口做纯 TCP 端口转发，可用于「A 端口 → 目标 B 端口」的映射。配置写入后通过 GOST 的 SIGHUP 热重载生效，已有连接不会被断开。

## 配置应用与自检

保存规则后，面板在后台对 GOST 执行热重载（`systemctl reload` → `SIGHUP` → 必要时重启），随后检查配置里的每个入站端口是否真的在监听：

- 端口正常：本次修改生效。
- 端口缺失或服务非 active：判定为配置被拒绝，自动回滚到上一份可用配置并重新热重载，失败原因显示在控制台提示条。

单独运行自检（不依赖 systemd 和网络）：

```bash
python panel/tests/test_apply.py
```
