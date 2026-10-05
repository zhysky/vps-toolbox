# VPS Toolbox

按 **系统重置 → 网络质量与本机连通性检测 → 针对性网络优化 → 安装 Trojan + Hysteria2** 管理 VPS。四个阶段独立执行；检测不重装系统、不自动改网络。报告仅保存在执行机器，不自动上传。

以下服务器命令在 **root 的 Bash** 中运行，无需手动上传文件。

## 1. 系统重置

**清空整个系统盘。先将需要保留的数据备份到另一台机器，并验证能恢复。** 脚本会核对适用条件并要求输入指定确认文字。

```bash
curl -fsSL https://raw.githubusercontent.com/zhysky/vps-toolbox/main/reset.sh -o vps-reset.sh && bash vps-reset.sh
```

仅检查条件：`bash vps-reset.sh --check`。

当前适配 Ubuntu 准备环境、amd64、KVM、单系统盘、ext4、GRUB、BIOS 或关闭 Secure Boot 的 UEFI；至少 800 MiB 可见内存和 10 GiB 系统盘。实测平台是 Ubuntu 26.04、UEFI、约 877 MiB 内存的 KVM VPS，其他组合不等于实测通过。

目标为 Canonical 官方 Ubuntu 26.04 minimal 镜像 `release-20261002`，验证清单签名和完整镜像 SHA-256。保留 root SSH 公钥、SSH 主机身份及已有可用的密码登录策略。**事先必须安装并用新连接验证 root 公钥登录；RAM 阶段使用公钥连接。**

流程：原系统准备和校验 → 单次启动 Alpine RAM → 核对磁盘、网络、SSH、镜像及可用内存 → 擦盘安装 → 补齐完整 initramfs、GRUB、DHCP 与 SSH → 自动重启。

- 擦盘前有独立 watchdog，预检未通过或未继续时超时回到旧系统。
- 擦盘开始后旧系统已清除，失败时保留 RAM 供诊断；不会自动重复擦盘，也不把此时重启称为回滚。
- `--prepare-only` 只准备；首次重启前可用 `--cancel` 撤销本工具的重装启动项。
- 自动入口 `--yes --backup-receipt 文件` 需要独立备份回执。受控验收可加 `--external-ready-gate`，从外部验证 RAM SSH 后才创建 `/configs/vps-reset/external-ready`。
- 新系统回执在 `/var/log/vps-reset/reset-receipt.json`。安装前生成的回执不能证明新系统已从外部可达，需重新登录检查。

## 2. 网络质量和到本机的连通性

安装工具并运行完整服务器检查：

```bash
curl -fsSL https://raw.githubusercontent.com/zhysky/vps-toolbox/main/check.sh -o vps-check.sh && bash vps-check.sh
```

Ubuntu / Debian 自动安装 Python、iperf3、MTR、sysbench 等依赖。覆盖系统/内核、CPU 单/多线程、内存、512 MiB 临时文件的 O_DIRECT 顺序和 QD1 随机 I/O、DNS、IPv4/IPv6、ICMP、TCP MTR、HTTPS、ASN 和公开端点上下行。CPU 最多 4 线程；测速会产生网络流量。

报告在 `/var/log/vps-quality-check/<时间-ID>/report.json` 和 `report.txt`。

```bash
bash vps-check.sh --install-only
vps-check run --route-target 你的本机公网IPv4
vps-check run --quick
bash vps-check.sh --uninstall  # 移除工具，保留报告与共享依赖
```

### 两端测试

服务器启动仅允许本机公网 IP 的临时服务：

```bash
vps-check peer-server --allow-client 你的本机公网IPv4 --ttl 1800 --output /var/log/vps-quality-check/peer-001
```

云安全组需允许 TCP/UDP `45201`、UDP `45202`、TCP `45203` 来自本机 IP。工具建立独立 nftables 表限制这些测试端口的来源；不改其他规则。正常退出、Ctrl+C 或 TTL 到期后清理自己的监听器和表。每次用新的输出目录。

本机需 Python 3 和 iperf3；下载同一份跨平台检测程序：

```powershell
curl.exe -fSL https://raw.githubusercontent.com/zhysky/vps-toolbox/main/vps_check.py -o vps_check.py
python vps_check.py peer-client --host 服务器IPv4 --bind 本机物理网卡IPv4 --interface-index 网卡序号 --expect-source 本机公网IPv4 --profile full --iperf iperf3.exe --output ./vps-quality-results
```

Windows 网卡序号用 `Get-NetIPConfiguration` 查看；程序未加入 PATH 时用完整路径。Linux 去掉 `--interface-index`，可加 `--interface 网卡名`。使用 TUN 时保持原配置并绑定物理网卡；服务端看到的来源与 `--expect-source` 不符时，停止测速。

| Profile | 内容 |
| --- | --- |
| quick | P1 TCP、1 Mbps UDP 双向，完整性、SSE、WebSocket |
| standard | P1/P2/P4/P8 TCP 双向，1/10/50/100 Mbps UDP 双向及应用层检查 |
| full | standard 加窗口/块大小、MSS、TCP_NODELAY、同时双向、UDP 多种报文长度 |
| compare | P1/P4 下载和 P1 上传各重复 3 次，用于参数对照 |

每项默认 10 秒，可加 `--seconds`。吞吐期间记录 1200 字节低频 UDP 往返延迟；服务端每 2 秒保存真实 TCP 连接、内存和 PSI。切换拥塞控制后应重启测试监听器，避免旧监听套接字继承之前算法。

解读：上传/下载相对于本机，以接收端为准；同时双向分别记录。pass/partial/fail/skip/error 分开，命令完成不代表全部通过。未知值保留 null；Windows iperf3 未提供重传时不填 0。无 IPv6 路由是未测，高于容量的 UDP 发送可能主动造成丢包，中间路由不响应 ICMP 不等于终点丢包。HTTP 403 不记零带宽；未认证 API 的 401 不能证明账号/模型可用。短文件突发速率不能代表持续容量。

## 3. 针对性优化和回滚

默认使用发行版内核的 BBR 和实际网卡 FQ，保留缓冲，不限制出口：

```bash
curl -fsSL https://raw.githubusercontent.com/zhysky/vps-toolbox/main/optimize.sh -o vps-optimize.sh && bash vps-optimize.sh
```

先完成第 2 步。只接受能恢复的简单默认 pfifo_fast/FQ-CoDel，拒绝覆盖复杂队列、过滤器或其他工具配置。

```bash
bash vps-optimize.sh --manual-commit  # 180 秒独立回滚保护
# 从另一条 SSH 连接登录成功后：
bash vps-optimize.sh --commit
bash vps-optimize.sh --status
bash vps-optimize.sh --rollback
```

普通模式通过服务端 SSH 配置和出站 HTTPS 自查后提交；自查不能代替新的外部 SSH 登录。推荐远程手动提交。回滚恢复参数和队列类型/选项，不恢复计数器、handle 或尚未发送的数据包。

候选参数分开测试，切换前先 `--rollback`：

```bash
bash vps-optimize.sh --cc cubic --manual-commit
bash vps-optimize.sh --buffer-mib 8 --manual-commit
bash vps-optimize.sh --rate-mbps 35 --manual-commit
```

缓冲默认 0 不变；8/16/32 MiB 档分别要求至少 512 MiB/2 GiB/4 GiB 内存，不降低已有上限。仅当测量指向窗口瓶颈时尝试。

**`--rate-mbps` 限制整台 VPS 的总出口，不只限制代理。** 默认 0 不限速，非零使用 TBF + FQ。不要照搬另一台服务器速率。挂子队列后，TBF 的 latency 参数不构成全部流量的延迟保证。

状态在 `/var/lib/vps-net-opt/state.json`，`vps-net-opt.service` 按网卡 MAC 持久化。提交后重启一次，核对真实队列、新 SSH/TCP 连接和服务状态。

## 4. 安装代理

优化验证后执行独立的 [dual-proxy-installer](https://github.com/zhysky/dual-proxy-installer)：

```bash
curl -fsSL https://raw.githubusercontent.com/zhysky/dual-proxy-installer/main/install-dual-proxy.sh -o install-dual-proxy.sh && bash install-dual-proxy.sh
```

输入域名、账号标签、密码和可选端口。默认 Trojan TCP 443、Hysteria2 UDP 443，可共用 443。域名 A/AAAA 应正确指向服务器；TCP 80 用于公开 CA 的 HTTP-01 验证，协议端口也需外部可达。安装后仍应以真实客户端检查 TLS、认证、上下行、SSE 和 WebSocket。

```bash
bash install-dual-proxy.sh --update     # 更新程序，保留域名、认证信息和端口
bash install-dual-proxy.sh --uninstall  # 删除本安装器自己的服务、配置与证书
```

不接管其他安装器的现有服务，不自动修改云安全组或本机 TUN。

## 验证与来源

实测及局限见 [validation/2026-10-05.md](validation/2026-10-05.md)。启动脚本将助手文件固定到提交与 SHA-256，见 [release-manifest.json](release-manifest.json)。严格复现时，把 URL 的 main 换成审阅过的完整提交 ID。

- 重装：[bin456789/reinstall 固定版本](https://github.com/bin456789/reinstall/tree/80c3d5e175f39c2d2bbd267cd583842140154140)，执行时保存补丁 diff。
- 镜像：[Canonical minimal](https://cloud-images.ubuntu.com/minimal/releases/resolute/)。
- 双端测试：[iperf3 官方文档](https://software.es.net/iperf/invoking.html)。
- 外部样本：[Cloudflare Speedtest](https://github.com/cloudflare/speedtest)。

本仓库按 GPL-3.0 提供，参见 [LICENSE](LICENSE)。系统包、上游程序和代理安装器分别遵循各自许可。
