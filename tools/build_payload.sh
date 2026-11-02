#!/usr/bin/env bash
# Rebuild the base64 panel payload embedded at the bottom of install.sh.
#
# install.sh is a self-contained installer: it carries panel/ as a gzip'd tar
# archive after the `__GSM_PAYLOAD_START__` marker. Editing panel/* alone does
# not change what gets installed, so run this after every panel change:
#
#     ./tools/build_payload.sh && git commit -am "chore: rebuild payload"
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "${repo_root}"

[[ -f install.sh ]] || { echo "install.sh not found in ${repo_root}" >&2; exit 1; }
[[ -f panel/app.py ]] || { echo "panel/app.py not found" >&2; exit 1; }

payload="$(mktemp)"
head_part="$(mktemp)"

# Package exactly what install_panel_files() expects to copy: the panel dir.
tar -czf - \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.venv' \
  --format=gnu \
  panel | base64 -w 76 > "${payload}"

awk '/^__GSM_PAYLOAD_START__$/ {exit} {print}' install.sh > "${head_part}"

{
  cat "${head_part}"
  echo "__GSM_PAYLOAD_START__"
  cat "${payload}"
  echo
  echo "__GSM_PAYLOAD_START__"
} > install.sh.new

mv install.sh.new install.sh
chmod +x install.sh
rm -f "${payload}" "${head_part}"

echo "payload rebuilt: $(wc -l < install.sh) lines, $(du -h install.sh | cut -f1)"
