#!/usr/bin/env bash
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
    'https://raw.githubusercontent.com/zhysky/vps-toolbox/561f3b7f5ac81e2317ec686442cef691a007a123/network_optimize.py' -o "$temporary/network_optimize.py"
printf '31b59d26081a673e25edeb4bb8047c43520f66dc5cc5452ccb9231f9c4b90cf2  %s\n' "$temporary/network_optimize.py" | sha256sum --check --strict >/dev/null
python3 "$temporary/network_optimize.py" "$@"
