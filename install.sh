#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 2 ]]; then
  echo '用法：sudo ./install.sh /绝对路径/claude-cn_VERSION_all.deb 用户名' >&2
  exit 1
fi
if [[ $EUID -ne 0 ]]; then echo '请使用 sudo 执行。' >&2; exit 1; fi
package_path=$(realpath -- "$1")
apt-get install "$package_path"
/usr/sbin/claude-cn-admin setup --user "$2"
