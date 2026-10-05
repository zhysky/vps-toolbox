#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
set -Eeuo pipefail
umask 022
PREFIX=/opt/vps-quality-check
LAUNCHER=/usr/local/bin/vps-check
MARKER=vps-toolbox-check:v1
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    cat <<'HELP'
用法：bash check.sh [--install-only | --uninstall | run [检测参数]]
默认安装依赖和 vps-check，然后执行完整服务器检测。
--install-only  只安装工具，之后运行 vps-check run
--uninstall     删除本工具，保留报告和系统共享依赖
支持 Ubuntu / Debian。服务器报告默认保存到 /var/log/vps-quality-check/。
两端检测：vps-check peer-server --help；vps-check peer-client --help
HELP
    exit 0
fi
[[ $EUID -eq 0 ]] || { echo '请使用 root 运行。' >&2; exit 1; }
[[ ! -L "$PREFIX" && ! -L "$LAUNCHER" ]] || { echo '目标路径是符号链接，停止。' >&2; exit 1; }
[[ $(realpath -m -- "$PREFIX") == "$PREFIX" && $(realpath -m -- "$LAUNCHER") == "$LAUNCHER" ]] || exit 1
if [[ -e "$PREFIX" ]]; then
    [[ -f "$PREFIX/.owner" && $(<"$PREFIX/.owner") == "$MARKER" ]] || { echo '目标目录不属于本工具。' >&2; exit 1; }
fi
if [[ -e "$LAUNCHER" ]]; then
    grep -Fqx '# Managed by vps-toolbox-check:v1' "$LAUNCHER" || { echo '同名命令不属于本工具。' >&2; exit 1; }
fi
if [[ ${1:-} == --uninstall ]]; then
    rm -f -- "$LAUNCHER" "$PREFIX/vps_check.py" "$PREFIX/.owner"
    if [[ -d "$PREFIX" ]]; then rmdir -- "$PREFIX" 2>/dev/null || true; fi
    echo '已删除检测工具；报告和共享依赖保留。'
    exit 0
fi
install_only=0
if [[ ${1:-} == --install-only ]]; then install_only=1; shift; fi
[[ -r /etc/os-release ]] || exit 1
# shellcheck disable=SC1091
. /etc/os-release
case ${ID:-} in ubuntu|debian) ;; *) echo '自动安装当前支持 Ubuntu / Debian。' >&2; exit 1;; esac
temporary=$(mktemp -d /var/tmp/vps-quality-helper.XXXXXXXX)
cleanup() { case "$temporary" in /var/tmp/vps-quality-helper.*) rm -rf --one-file-system -- "$temporary";; esac; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
apt-get -o DPkg::Lock::Timeout=120 update -qq
if ! command -v iperf3 >/dev/null && command -v debconf-set-selections >/dev/null; then
    printf 'iperf3 iperf3/start_daemon boolean false\n' | debconf-set-selections
fi
apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends --no-upgrade \
    ca-certificates curl python3 iproute2 iputils-ping mtr-tiny sysbench iperf3 procps nftables
curl -fsSL --proto '=https' --tlsv1.2 --connect-timeout 15 --max-time 60 \
    'https://raw.githubusercontent.com/zhysky/vps-toolbox/bcd5fe92644a4f8ef92628f8440970536db7d8a2/vps_check.py' -o "$temporary/vps_check.py"
printf '892b86271a10e574afcff7c222b1978762dfdae744a05e46936c13c2d820a37c  %s\n' "$temporary/vps_check.py" | sha256sum --check --strict >/dev/null
python3 "$temporary/vps_check.py" --version
install -d -m 0755 "$PREFIX"
install -m 0644 "$temporary/vps_check.py" "$PREFIX/vps_check.py.new"
mv -f -- "$PREFIX/vps_check.py.new" "$PREFIX/vps_check.py"
printf '%s\n' "$MARKER" > "$PREFIX/.owner"
cat > "$temporary/launcher" <<'LAUNCHER_SCRIPT'
#!/bin/sh
# Managed by vps-toolbox-check:v1
exec python3 /opt/vps-quality-check/vps_check.py "$@"
LAUNCHER_SCRIPT
install -m 0755 "$temporary/launcher" "$LAUNCHER"
echo '已安装：vps-check'
if [[ $install_only == 0 ]]; then
    if [[ $# == 0 ]]; then set -- run; fi
    "$LAUNCHER" "$@"
fi
