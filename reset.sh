#!/usr/bin/env bash
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
base='https://raw.githubusercontent.com/zhysky/vps-toolbox/111709938c5a0e7b758656f816809161749816b7/reset'
while read -r digest name; do
    curl -fsSL --proto '=https' --tlsv1.2 --connect-timeout 15 --max-time 60 "$base/$name" -o "$temporary/$name"
    printf '%s  %s\n' "$digest" "$temporary/$name" | sha256sum --check --strict >/dev/null
done <<'HASHES'
516fffaced54a9822d5bd67680e3251dbd681532546df7c44715a4787bf48f85 prepare.py
2fb7927594113eed54a8799c299ea515a11620dc17cc60871315af74e28414be patch_sources.py
10b57ef8728b790e5cbe2136ea77e3941996efae0a22005dadb8c45049279b01 inject_initrd.py
492a7d1ca3cc0aa420569827a34e17a95121c23437fe248849cbeda46d87ce10 preflight.sh
09f411d74ad9abee43ef7b2f01d144a0bd866eaa94ae3761df4141d21c14004b gate.sh
3612678372f9f6bdb32f68adb64eca1bda5d01b7b5f0e2d9a571b2c697cd0079 finalize.sh
cd59f6a1503c407301305ebe8608e69bc4c2e29f6b9709ede3fa10e6a4938e2f finalize.py
0f2c25f38dbb17d5f707e03914089257ca0deaf8446d80376536790c17bc0416 bootguard.c
HASHES
python3 "$temporary/prepare.py" "$@"
