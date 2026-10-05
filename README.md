# VPS Toolbox

按 **系统重置 → 网络质量与本机连通性检测 → 针对性网络优化 → 安装 Trojan + Hysteria2** 的顺序管理自己的 VPS。

检测、重置与优化分开执行，检测不会自行重装系统或更改网络参数。报告仅保存在执行检测的机器上。

本仓库正在进行首台 VPS 实测；验证记录和完整命令将在实测完成后补充。此阶段请阅读脚本适用条件，尤其是系统盘重置的擦盘范围。

## 系统重置

`reset/` 是对固定版本 [bin456789/reinstall](https://github.com/bin456789/reinstall/tree/80c3d5e175f39c2d2bbd267cd583842140154140) 的保护性包装，保留上游分区、NBD 与系统安装流程。

- 当前适配：Ubuntu 准备环境、amd64、KVM、单系统盘、ext4 根分区、GRUB、BIOS 或 UEFI（关闭 Secure Boot）。
- 目标：官方 Ubuntu 26.04 minimal 云镜像，固定 release-20261002；验证 Canonical 签名与完整镜像 SHA-256。
- 需要至少 800 MiB 可见内存、10 GiB 系统盘，并在 Alpine RAM 环境再次检查实际可用内存。
- **清空整个系统盘。必须先在另一台机器保存并验证所需备份。**
- 保留 root SSH 公钥、SSH 主机身份及已有的可用密码登录。先安装并独立验证 root 公钥；RAM 阶段使用公钥。
- 擦盘前设置超时回到原系统的保护；开始写盘后不再把重启称作回滚，失败时保留 RAM 环境供诊断。
- 默认完整执行并自动重启；`--prepare-only`、`--external-ready-gate` 用于受控验收，`--check` 只检查适用条件。

## 网络检测

`vps_check.py run` 检测系统、CPU、内存、512 MiB 磁盘短测、DNS、IPv4/IPv6、ICMP、MTR、HTTPS、公开测试端点上下行和 ASN 信息。

`peer-server` / `peer-client` 检测两台机器之间的 TCP/UDP、单/多连接、不同 socket buffer、报文大小、负载延迟、内容完整性、SSE 与 WebSocket。临时服务只接受指定客户端 IP，并在指定时间后退出、清理自己的防火墙表。

无 IPv6 路由、缺少工具、HTTP 拒绝、超时和不支持的参数分别记录；不把它们填成零带宽，也不把中间节点的 ICMP 丢包直接当作线路故障。

## 代理安装

使用独立的 [dual-proxy-installer](https://github.com/zhysky/dual-proxy-installer)，支持交互安装、保留配置更新和卸载。

## 来源与许可

- 系统重装引擎：[bin456789/reinstall](https://github.com/bin456789/reinstall)，固定提交如上；执行时保留补丁 diff 和来源摘要。
- 镜像：[Canonical 官方 minimal 云镜像](https://cloud-images.ubuntu.com/minimal/releases/resolute/)。
- 双端带宽：[iperf3 官方文档](https://software.es.net/iperf/invoking.html)。
- 外部带宽样本：[Cloudflare Speedtest](https://github.com/cloudflare/speedtest)。

代码按 GPL-3.0 提供，参见 [LICENSE](LICENSE)。系统软件包与下载的上游程序分别遵循各自许可证。
