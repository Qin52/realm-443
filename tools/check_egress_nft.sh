#!/usr/bin/env bash
# check_egress_nft.sh —— 出口守卫 nft 规则回归检查（契约第 1 节"回归检查脚本"）
#
# 作用：在 Linux 上把三种策略（关闭 / 全放开 / 白名单）渲染成完整的 nft 脚本，
#       用 `nft -c -f -` 做语法检查，输出人可读结论。
# 说明：
#   * 渲染逻辑只有一套，来自 panel/egress.py 的 nft_script()；本脚本不复制规则。
#   * 非 root 或没有 nft 时只做"渲染 + 提示"，不阻断；优雅跳过并给出原因。
#   * 只做 -c（check）语法校验，绝不真正加载规则，不会改动在跑的防火墙。
set -Eeuo pipefail

APP_DIR="${GSM_APP_DIR:-/opt/gost-sni-manager}"
PY="${GSM_PYTHON:-}"
RUN_CHECK=1

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  C_OK=$'\033[1;32m'
  C_WARN=$'\033[1;33m'
  C_ERR=$'\033[1;31m'
  C_INFO=$'\033[1;36m'
  C_OFF=$'\033[0m'
else
  C_OK="" C_WARN="" C_ERR="" C_INFO="" C_OFF=""
fi

ok()   { printf '%s[ok]%s %s\n' "${C_OK}" "${C_OFF}" "$*"; }
warn() { printf '%s[!]%s %s\n' "${C_WARN}" "${C_OFF}" "$*"; }
err()  { printf '%s[x]%s %s\n' "${C_ERR}" "${C_OFF}" "$*" >&2; }
info() { printf '%s[>]%s %s\n' "${C_INFO}" "${C_OFF}" "$*"; }

usage() {
  cat <<'EOF'
用法：bash tools/check_egress_nft.sh [--no-check] [--app-dir DIR]

  --no-check       只渲染规则，不调用 nft 做语法检查
  --app-dir DIR    应用目录（默认 /opt/gost-sni-manager，也可用 GSM_APP_DIR）

退出码：0 全部通过（含优雅跳过）；1 有策略渲染或 nft 语法检查失败。

需要 panel/egress.py（来自应用目录，或本脚本同级 ../panel/egress.py），
用它的 nft_script() 渲染三种策略，保证与生产用的是同一套规则生成逻辑。
EOF
}

while (( $# > 0 )); do
  case "$1" in
    --no-check) RUN_CHECK=0; shift ;;
    --app-dir)
      [[ $# -ge 2 ]] || { err "--app-dir 需要一个目录"; exit 1; }
      APP_DIR="$2"; shift 2 ;;
    --app-dir=*) APP_DIR="${1#--app-dir=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) err "未知参数：$1"; usage >&2; exit 1 ;;
  esac
done

# 定位 egress.py：应用目录优先，其次仓库内 ../panel/egress.py（便于开发机上直接跑）。
script_dir=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P 2>/dev/null || true)"
fi

EGRESS_PY=""
for candidate in \
  "${APP_DIR}/egress.py" \
  "${script_dir}/../panel/egress.py"; do
  if [[ -n "${candidate}" && -f "${candidate}" ]]; then
    EGRESS_PY="${candidate}"
    break
  fi
done

if [[ -z "${EGRESS_PY}" ]]; then
  warn "找不到 panel/egress.py（已查找 ${APP_DIR}/egress.py 与 ${script_dir:-.}/../panel/egress.py）。"
  warn "跳过 nft 规则检查；请先安装面板或指定 --app-dir。"
  exit 0
fi

if [[ -z "${PY}" ]]; then
  for candidate in "${APP_DIR}/venv/bin/python" python3 python; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      PY="${candidate}"
      break
    fi
  done
fi
if [[ -z "${PY}" ]]; then
  warn "找不到可用的 python 解释器，跳过 nft 规则检查。"
  exit 0
fi

info "egress.py: ${EGRESS_PY}"
info "python: ${PY}"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT

# 渲染器：把三种策略写成 nft 脚本；渲染失败会以非 0 退出。
render() {
  local name="$1" enabled="$2" mode="$3"
  "${PY}" - "${EGRESS_PY}" "${tmp_dir}/${name}.nft" "${enabled}" "${mode}" <<'PY'
import sys
from pathlib import Path

egress_path, out_path, enabled, mode = sys.argv[1:5]
sys.path.insert(0, str(Path(egress_path).resolve().parent))

import egress  # noqa: E402

policy = {
    "enabled": enabled == "1",
    "mode": mode,
    "domains": ["example.com", "203.0.113.10"] if mode == "allowlist" else [],
    "block_quic": True,
    "ipv6": True,
}
allow4 = ["203.0.113.10"] if mode == "allowlist" else []
allow6 = ["2001:db8::1"] if mode == "allowlist" else []
script = egress.nft_script(policy, allow4, allow6, ["127.0.0.53"], ["::1"])
Path(out_path).write_text(script, encoding="utf-8")
print(f"渲染 {out_path}: {len(script.splitlines())} 行")
PY
}

failures=0
check_one() {
  local name="$1" enabled="$2" mode="$3" label="$4"
  local file="${tmp_dir}/${name}.nft"

  info "策略：${label}"
  if ! render "${name}" "${enabled}" "${mode}"; then
    err "渲染失败：${label}"
    failures=$((failures + 1))
    return 0
  fi

  # 关闭态必须落到 drop 结尾且不引用放行集合，这是安全红线第 3 条。
  if [[ "${mode}" == "off" ]] || [[ "${enabled}" == "0" ]]; then
    if grep -q '@allow4' "${file}"; then
      err "关闭态仍然引用了 @allow4，违反契约第 4 节。"
      failures=$((failures + 1))
      return 0
    fi
    if ! grep -qE '^\s*drop\s*$' "${file}"; then
      err "关闭态没有以裸 drop 结束，流量可能被放行。"
      failures=$((failures + 1))
      return 0
    fi
  fi

  if (( RUN_CHECK == 0 )); then
    ok "${label}：已渲染（--no-check，跳过语法检查）"
    return 0
  fi

  if ! command -v nft >/dev/null 2>&1; then
    warn "${label}：已渲染，但本机没有 nft 命令，跳过语法检查。"
    return 0
  fi
  if [[ "${EUID}" -ne 0 ]]; then
    warn "${label}：已渲染，但当前不是 root，跳过 nft -c 语法检查。"
    return 0
  fi

  # nft -c -f - 只做语法/语义检查，不加载规则。
  local out
  if out="$(nft -c -f "${file}" 2>&1)"; then
    ok "${label}：nft 语法检查通过"
  else
    err "${label}：nft 语法检查失败"
    printf '%s\n' "${out}" >&2
    failures=$((failures + 1))
  fi
}

check_one "off"       "0" "allowlist" "关闭（默认态，全拒绝）"
check_one "all"       "1" "all"       "打开 · 全放开"
check_one "allowlist" "1" "allowlist" "打开 · 白名单"

# 展示关闭态的脚本片段，方便人工 review（这是安全上最关键的一份）。
echo
info "关闭态渲染结果（${tmp_dir}/off.nft）:"
cat "${tmp_dir}/off.nft"
echo

if (( failures > 0 )); then
  err "检查结束：${failures} 项失败。"
  exit 1
fi
ok "检查结束：全部通过（渲染 3 种策略）。"
exit 0
