import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REF = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
BASE = 'https://raw.githubusercontent.com/zhysky/vps-toolbox/' + REF


def sha(path):
    data = subprocess.check_output(['git', '-C', str(ROOT), 'show', REF + ':' + path])
    return hashlib.sha256(data).hexdigest()


reset_files = ['prepare.py', 'patch_sources.py', 'inject_initrd.py', 'preflight.sh', 'gate.sh', 'finalize.sh', 'finalize.py', 'bootguard.c']
reset_hashes = {name: sha('reset/' + name) for name in reset_files}
reset = r'''#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
set -Eeuo pipefail
umask 077
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    cat <<'HELP'
用法：bash reset.sh [--check | --prepare-only | --cancel]
默认：交互确认独立备份后，重置整个系统盘为 Ubuntu 26.04 minimal 并自动重启。
支持：Ubuntu、amd64、KVM、单盘 ext4、GRUB、BIOS/UEFI（关闭 Secure Boot）。
准备好并独立验证 root SSH 公钥；保留 SSH 主机密钥和当前可用的 root 密码登录。
受控执行：--yes --backup-receipt 文件 [--external-ready-gate]
--check             只检查环境，不改变启动项
--prepare-only      准备并验证启动项，不自动重启
--cancel            首次重启前取消本工具准备的重装启动项
HELP
    exit 0
fi
[[ $EUID -eq 0 ]] || { echo '请使用 root 运行。' >&2; exit 1; }
for executable in curl python3 sha256sum; do command -v "$executable" >/dev/null || { echo "缺少 $executable" >&2; exit 1; }; done
temporary=$(mktemp -d /var/tmp/vps-reset-helper.XXXXXXXX)
cleanup() { case "$temporary" in /var/tmp/vps-reset-helper.*) rm -rf --one-file-system -- "$temporary";; esac; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
base='__BASE__/reset'
while read -r digest name; do
    curl -fsSL --proto '=https' --tlsv1.2 --connect-timeout 15 --max-time 60 "$base/$name" -o "$temporary/$name"
    printf '%s  %s\n' "$digest" "$temporary/$name" | sha256sum --check --strict >/dev/null
done <<'HASHES'
__HASHES__
HASHES
python3 "$temporary/prepare.py" "$@"
'''.replace('__BASE__', BASE).replace('__HASHES__', '\n'.join(value + ' ' + key for key, value in reset_hashes.items()))
engine_hash = sha('vps_check.py')
check = r'''#!/usr/bin/env bash
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
    '__BASE__/vps_check.py' -o "$temporary/vps_check.py"
printf '__ENGINE_HASH__  %s\n' "$temporary/vps_check.py" | sha256sum --check --strict >/dev/null
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
'''.replace('__BASE__', BASE).replace('__ENGINE_HASH__', engine_hash)
optimizer_hash = sha('network_optimize.py')
optimize = r'''#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
set -Eeuo pipefail
umask 077
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    cat <<'HELP'
用法：bash optimize.sh [--status | --rollback | --commit | 优化参数]
默认：发行版内核 BBR + 实际网卡 FQ，保留已有缓冲，不限制出口带宽。
只接受可以完整恢复参数的简单默认 pfifo_fast / fq_codel 队列。
--cc cubic          测试 CUBIC + FQ 对照组
--buffer-mib 8      有测量依据时尝试缓冲候选档；默认 0（保留）
--rate-mbps 数值    可选：限制整台 VPS 的总出口；默认 0（不限制）
--manual-commit     在新 SSH 连接验证后执行 --commit，否则 180 秒后回滚
--rollback          恢复本工具修改前的参数并取消持久设置
--status            查看真实运行状态与事务记录
普通模式执行服务端自查后提交；服务端自查不能代替外部新 SSH 登录验证。
HELP
    exit 0
fi
[[ $EUID -eq 0 && -d /run/systemd/system ]] || { echo '需要 root 和 systemd。' >&2; exit 1; }
for executable in python3 curl sha256sum tc ss sysctl systemctl modprobe; do
    command -v "$executable" >/dev/null || { echo "缺少依赖 $executable，请先运行检测工具安装命令。" >&2; exit 1; }
done
case ${1:-} in
    --status) shift; set -- status "$@";;
    --rollback) shift; set -- rollback "$@";;
    --commit) shift; set -- commit "$@";;
    apply|status|rollback|commit) ;;
    *) set -- apply "$@";;
esac
temporary=$(mktemp -d /var/tmp/vps-opt-helper.XXXXXXXX)
cleanup() { case "$temporary" in /var/tmp/vps-opt-helper.*) rm -rf --one-file-system -- "$temporary";; esac; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
curl -fsSL --proto '=https' --tlsv1.2 --connect-timeout 15 --max-time 60 \
    '__BASE__/network_optimize.py' -o "$temporary/network_optimize.py"
printf '__HASH__  %s\n' "$temporary/network_optimize.py" | sha256sum --check --strict >/dev/null
python3 "$temporary/network_optimize.py" "$@"
'''.replace('__BASE__', BASE).replace('__HASH__', optimizer_hash)
for name, value in [('reset.sh', reset), ('check.sh', check), ('optimize.sh', optimize)]:
    (ROOT / name).write_text(value, encoding='utf-8', newline='\n')
(ROOT / 'release-manifest.json').write_text(json.dumps({'source_commit': REF, 'reset_helper_sha256': reset_hashes,
                                                       'vps_check_sha256': engine_hash, 'optimizer_sha256': optimizer_hash}, indent=2) + '\n', encoding='utf-8', newline='\n')
print(json.dumps({'source_commit': REF, 'reset_helpers': len(reset_files), 'engine_sha256': engine_hash}))
