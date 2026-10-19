#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo '请执行 sudo ./uninstall.sh' >&2; exit 1; fi
exec apt-get remove claude-cn
