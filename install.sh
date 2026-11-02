#!/usr/bin/env bash
set -Eeuo pipefail

# GOST SNI Manager one-key installer for Debian/Ubuntu.
# It installs GOST v3, Caddy, and a lightweight web panel that manages SNI-based TCP forwarding.

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 root 执行：sudo bash install.sh"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

PANEL_USER="${PANEL_USER:-}"
PANEL_PASSWORD="${PANEL_PASSWORD:-}"
PANEL_DOMAIN="${PANEL_DOMAIN:-}"
PANEL_PORT="${PANEL_PORT:-}"
PANEL_BIND="${PANEL_BIND:-}"
PANEL_ACCESS_URL=""
SERVER_IP=""
CADDY_HTTPS_PORT="${CADDY_HTTPS_PORT:-8053}"
GOST_LISTEN="${GOST_LISTEN:-:443}"
GOST_FALLBACK="${GOST_FALLBACK:-127.0.0.1:${CADDY_HTTPS_PORT}}"
INIT_RULES="${INIT_RULES:-}"
INSTALL_DIR="${INSTALL_DIR:-/opt/gost-sni-manager}"
DATA_DIR="${DATA_DIR:-/etc/gost-panel}"
GOST_CONFIG="${GOST_CONFIG:-/etc/gost/config.yaml}"
CREDENTIALS_FILE="${CREDENTIALS_FILE:-/root/gsm.txt}"
RELAY_SERVICE="${RELAY_SERVICE:-gsm-relay}"
GOST_FORWARD_MODE="${GOST_FORWARD_MODE:-direct}"

log() {
  printf '\n\033[1;36m==> %s\033[0m\n' "$*"
}

warn() {
  printf '\n\033[1;33m[!] %s\033[0m\n' "$*"
}

die() {
  printf '\n\033[1;31m[x] %s\033[0m\n' "$*" >&2
  exit 1
}

validate_domain_bash() {
  local domain="$1"
  [[ "${domain}" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]]
}

sanitize_domain_input() {
  local raw="$1"
  raw="${raw#http://}"
  raw="${raw#https://}"
  raw="${raw%%/*}"
  raw="${raw%%:*}"
  printf '%s' "${raw}"
}

prompt_panel_domain() {
  if [[ -n "${PANEL_DOMAIN}" ]]; then
    PANEL_DOMAIN="$(sanitize_domain_input "${PANEL_DOMAIN}")"
    validate_domain_bash "${PANEL_DOMAIN}" || die "PANEL_DOMAIN 格式不正确：${PANEL_DOMAIN}"
    return 0
  fi

  if [[ -t 0 ]]; then
    echo
    echo "是否配置管理面板域名？"
    echo "- 配置域名：脚本会写入 Caddy，访问 https://你的域名/，并自动申请证书。"
    echo "- 不配置域名：面板直接开放在随机高位端口，访问 http://服务器IP:端口/。"
    read -r -p "是否现在配置面板域名？[y/N]: " ans || true
    case "${ans:-}" in
      y|Y|yes|YES|Yes|是)
        while true; do
          read -r -p "请输入面板域名，例如 admin.example.com: " input_domain || true
          PANEL_DOMAIN="$(sanitize_domain_input "${input_domain:-}")"
          if validate_domain_bash "${PANEL_DOMAIN}"; then
            break
          fi
          warn "域名格式不正确，请重新输入。"
        done
        ;;
      *)
        PANEL_DOMAIN=""
        ;;
    esac
  fi
}

require_debian_like() {
  if [[ ! -f /etc/os-release ]]; then
    die "未检测到 /etc/os-release，暂只支持 Debian/Ubuntu。"
  fi
  . /etc/os-release
  case "${ID:-}" in
    debian|ubuntu) ;;
    *) warn "当前系统是 ${PRETTY_NAME:-unknown}，脚本将按 Debian/Ubuntu 方式安装。" ;;
  esac
}

install_base_packages() {
  log "安装基础依赖"
  apt-get -qq update
  apt-get install -y -qq --no-install-recommends ca-certificates curl tar gzip openssl python3 python3-venv systemd iproute2
}

generate_username() {
  python3 - <<'PY'
import secrets
print('gsm_' + secrets.token_hex(4))
PY
}

generate_password() {
  python3 - <<'PY'
import secrets, string
alphabet = string.ascii_letters + string.digits + '@#%+=_.:-'
print(''.join(secrets.choice(alphabet) for _ in range(36)))
PY
}

validate_port_number() {
  local port="$1"
  [[ "${port}" =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 ))
}

is_port_free() {
  local port="$1"
  ! ss -H -tuln 2>/dev/null | awk '{print $5}' | grep -Eq "(^|:|\])${port}$"
}

choose_panel_port() {
  if [[ -n "${PANEL_PORT}" ]]; then
    validate_port_number "${PANEL_PORT}" || die "PANEL_PORT 端口无效：${PANEL_PORT}"
    (( PANEL_PORT >= 50000 && PANEL_PORT <= 55000 )) || die "PANEL_PORT 必须在 50000-55000 范围内：${PANEL_PORT}"
    if ! is_port_free "${PANEL_PORT}"; then
      die "PANEL_PORT=${PANEL_PORT} 已被占用，请换一个端口。"
    fi
    return 0
  fi

  local port i
  for i in $(seq 1 200); do
    port=$((50000 + RANDOM % 5001))
    if is_port_free "${port}"; then
      PANEL_PORT="${port}"
      return 0
    fi
  done

  for port in $(seq 50000 55000); do
    if is_port_free "${port}"; then
      PANEL_PORT="${port}"
      return 0
    fi
  done
  die "50000-55000 之间没有找到未占用端口。"
}

detect_server_ip() {
  local ip=""
  ip="$(curl -4 -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
  if [[ -z "${ip}" ]]; then
    ip="$(curl -6 -fsS --max-time 5 https://api64.ipify.org 2>/dev/null || true)"
  fi
  if [[ -z "${ip}" ]]; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  printf '%s' "${ip:-服务器IP}"
}

prepare_runtime_options() {
  log "生成面板账号和运行参数"
  choose_panel_port
  if [[ -z "${PANEL_USER}" ]]; then
    PANEL_USER="$(generate_username)"
  fi
  if [[ -z "${PANEL_PASSWORD}" ]]; then
    PANEL_PASSWORD="$(generate_password)"
  fi

  if [[ -n "${PANEL_DOMAIN}" ]]; then
    PANEL_BIND="127.0.0.1"
    PANEL_ACCESS_URL="https://${PANEL_DOMAIN}/"
  else
    PANEL_BIND="0.0.0.0"
    SERVER_IP="$(detect_server_ip)"
    PANEL_ACCESS_URL="http://${SERVER_IP}:${PANEL_PORT}/"
  fi
}

detect_asset_arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo "amd64" ;;
    aarch64|arm64) echo "arm64" ;;
    armv7l|armv7*) echo "armv7" ;;
    armv6l|armv6*) echo "armv6" ;;
    *) die "暂不支持的 CPU 架构：$(uname -m)" ;;
  esac
}

install_gost() {
  log "安装或升级 GOST v3"
  local version="${GOST_VERSION:-3.2.6}"
  local arch tarball url tmp
  arch="$(detect_asset_arch)"
  tmp="$(mktemp -d)"
  tarball="gost_${version}_linux_${arch}.tar.gz"
  url="https://github.com/go-gost/gost/releases/download/v${version}/${tarball}"
  curl -fsSL -o "${tmp}/${tarball}" "${url}"
  tar -xzf "${tmp}/${tarball}" -C "${tmp}"
  install -m 0755 "${tmp}/gost" /usr/local/bin/gost
  ln -sf /usr/local/bin/gost /usr/bin/gost
  rm -rf "${tmp}"
  /usr/local/bin/gost -V || true
}

install_caddy_binary() {
  local version="${CADDY_VERSION:-2.10.2}"
  local arch tarball url tmp
  arch="$(detect_asset_arch)"
  tmp="$(mktemp -d)"
  tarball="caddy_${version}_linux_${arch}.tar.gz"
  url="https://github.com/caddyserver/caddy/releases/download/v${version}/${tarball}"
  curl -fsSL -o "${tmp}/${tarball}" "${url}"
  tar -xzf "${tmp}/${tarball}" -C "${tmp}" caddy
  install -m 0755 "${tmp}/caddy" /usr/bin/caddy
  rm -rf "${tmp}"
  mkdir -p /etc/caddy /var/lib/caddy /var/log/caddy
  if [[ ! -f /etc/systemd/system/caddy.service && ! -f /usr/lib/systemd/system/caddy.service && ! -f /lib/systemd/system/caddy.service ]]; then
    cat > /etc/systemd/system/caddy.service <<'EOF2'
[Unit]
Description=Caddy
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/bin/caddy run --environ --config /etc/caddy/Caddyfile
ExecReload=/usr/bin/caddy reload --config /etc/caddy/Caddyfile --force
TimeoutStopSec=5s
LimitNOFILE=1048576
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF2
  fi
}

install_caddy() {
  log "安装 Caddy"
  if ! command -v caddy >/dev/null 2>&1; then
    rm -f /etc/apt/sources.list.d/caddy-stable.list /etc/apt/sources.list.d/caddy*.list /usr/share/keyrings/caddy-stable-archive-keyring.gpg 2>/dev/null || true
    if ! apt-get install -y -qq --no-install-recommends caddy; then
      warn "系统仓库未能安装 Caddy，改用 GitHub 二进制安装。"
      install_caddy_binary
    fi
  fi
  caddy version || true
}

extract_payload() {
  local tmp_dir
  tmp_dir="$(mktemp -d)"
  local script_dir=""
  if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P 2>/dev/null || true)"
  fi
  if [[ -n "${script_dir}" && -f "${script_dir}/panel/app.py" ]]; then
    echo "${script_dir}/panel"
    return 0
  fi
  sed -n '/^__gsm_payload__()/,/^__GSM_PAYLOAD_START__$/p' "${BASH_SOURCE[0]}" | sed '1,2d;$d' | base64 -d | tar -xz -C "${tmp_dir}"
  echo "${tmp_dir}/panel"
}

install_panel_files() {
  log "安装管理面板"
  local src
  src="$(extract_payload)"
  rm -rf "${INSTALL_DIR}"
  mkdir -p "${INSTALL_DIR}" "${DATA_DIR}" /etc/gost
  cp -a "${src}/." "${INSTALL_DIR}/"
  python3 -m venv "${INSTALL_DIR}/venv"
  "${INSTALL_DIR}/venv/bin/python" -m pip install -q --upgrade pip setuptools wheel
  "${INSTALL_DIR}/venv/bin/pip" install -q -r "${INSTALL_DIR}/requirements.txt"
}

initialize_panel() {
  log "初始化账号、规则数据和 GOST 配置"
  GOST_PANEL_DATA="${DATA_DIR}" GOST_CONFIG="${GOST_CONFIG}" GOST_LISTEN="${GOST_LISTEN}" GOST_FALLBACK="${GOST_FALLBACK}" INIT_RULES="${INIT_RULES}" \
    "${INSTALL_DIR}/venv/bin/python" "${INSTALL_DIR}/app.py" init \
      --username "${PANEL_USER}" \
      --password "${PANEL_PASSWORD}" \
      --listen "${GOST_LISTEN}" \
      --fallback "${GOST_FALLBACK}" \
      --panel-domain "${PANEL_DOMAIN}" \
      --init-rules "${INIT_RULES}" \
      --mode "${GOST_FORWARD_MODE}" \
      --force-auth
}

write_services() {
  log "写入 systemd 服务"
  cat > /etc/systemd/system/gost-panel.service <<EOF2
[Unit]
Description=GOST SNI Manager Web Panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
Environment=GOST_PANEL_DATA=${DATA_DIR}
Environment=GOST_CONFIG=${GOST_CONFIG}
Environment=GOST_SERVICE=gost
Environment=CADDY_SERVICE=caddy
ExecStart=${INSTALL_DIR}/venv/bin/gunicorn --workers 1 --threads 4 --bind ${PANEL_BIND}:${PANEL_PORT} wsgi:app
Restart=always
RestartSec=3
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
EOF2

  cat > /etc/systemd/system/gsm-relay.service <<EOF2
[Unit]
Description=GOST SNI Manager Local Relay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
Environment=GOST_PANEL_DATA=${DATA_DIR}
Environment=GOST_PANEL_DB=${DATA_DIR}/gsm.db
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/relay.py
Restart=always
RestartSec=3
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF2

  cat > /etc/systemd/system/gost.service <<EOF2
[Unit]
Description=GOST v3 SNI Reverse Proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/gost -C ${GOST_CONFIG}
ExecReload=/bin/kill -SIGHUP \$MAINPID
Restart=always
RestartSec=3
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF2
}

write_caddyfile() {
  log "写入 Caddy 配置"
  mkdir -p /etc/caddy
  if [[ -n "${PANEL_DOMAIN}" ]]; then
    cat > /etc/caddy/Caddyfile <<EOF2
{
    https_port ${CADDY_HTTPS_PORT}
    auto_https disable_redirects
    skip_install_trust
}

https://${PANEL_DOMAIN} {
    encode zstd gzip
    reverse_proxy 127.0.0.1:${PANEL_PORT}
}

:${CADDY_HTTPS_PORT} {
    tls internal
    respond "GOST fallback OK" 200
}
EOF2
  else
    cat > /etc/caddy/Caddyfile <<EOF2
{
    https_port ${CADDY_HTTPS_PORT}
    auto_https disable_redirects
    skip_install_trust
}

:${CADDY_HTTPS_PORT} {
    tls internal
    respond "GOST fallback OK" 200
}
EOF2
  fi
  caddy fmt --overwrite /etc/caddy/Caddyfile || true
}

start_services() {
  log "启动服务"
  systemctl daemon-reload
  systemctl enable --now gost-panel
  systemctl restart gost-panel
  if [[ "${GOST_FORWARD_MODE}" == "carpool" ]]; then
    systemctl enable --now gsm-relay
    systemctl restart gsm-relay
  else
    systemctl disable --now gsm-relay >/dev/null 2>&1 || true
  fi
  systemctl enable --now caddy
  systemctl restart caddy
  systemctl enable --now gost
  systemctl restart gost
  systemctl restart caddy || true
}

write_credentials_file() {
  install -m 0600 /dev/null "${CREDENTIALS_FILE}"
  cat > "${CREDENTIALS_FILE}" <<EOF2
GOST SNI Manager 登录信息

面板访问: ${PANEL_ACCESS_URL}
用户名: ${PANEL_USER}
密码: ${PANEL_PASSWORD}

GOST 配置文件: ${GOST_CONFIG}
面板数据目录: ${DATA_DIR}
面板内部端口: ${PANEL_PORT}
默认运行模式: 自用模式（GOST 直连目标，gsm-relay 默认不启动）
拼车模式中继服务: gsm-relay.service
Caddy HTTPS fallback: 127.0.0.1:${CADDY_HTTPS_PORT}
GOST 监听: ${GOST_LISTEN}
未知 SNI 默认后端: ${GOST_FALLBACK}

请立即保存以上信息。此文件权限为 600，仅 root 可读。
EOF2
  chmod 0600 "${CREDENTIALS_FILE}"
}

print_summary() {
  write_credentials_file
  log "安装完成"
  echo "面板访问: ${PANEL_ACCESS_URL}"
  echo "用户名: ${PANEL_USER}"
  echo "密码: ${PANEL_PASSWORD}"
  echo "登录信息已保存: ${CREDENTIALS_FILE}"
  echo "请立即保存上面的用户名和密码。"
  if [[ -n "${PANEL_DOMAIN}" ]]; then
    echo "提示: ${PANEL_DOMAIN} 需要解析到本机公网 IP；公网 443 由 GOST 接收，未知 SNI 会转发到 Caddy:${CADDY_HTTPS_PORT}。"
    echo "如果首次访问证书还在签发，稍后重试或查看: journalctl -u caddy -f"
  else
    warn "未配置面板域名，面板直接监听 ${PANEL_BIND}:${PANEL_PORT}。建议用防火墙限制来源，或后续配置域名。"
  fi
}

main() {
  prompt_panel_domain
  require_debian_like
  install_base_packages
  prepare_runtime_options
  install_gost
  install_caddy
  install_panel_files
  initialize_panel
  write_services
  write_caddyfile
  start_services
  print_summary
}

main "$@"
exit 0

__gsm_payload__() {
  : <<'__GSM_PAYLOAD_START__'
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
AgA=

__GSM_PAYLOAD_START__
}
__gsm_payload__ >/dev/null 2>&1 || true
