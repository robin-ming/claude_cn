# 验证记录：0.3.1

## 本次变更（代码 review 修复）

- 移除 deb 中遗留的 sudo 依赖；启动代理轮询回收已结束会话，上限不再计入僵尸进程；客户端对畸形服务响应给出明确错误；`configure` 非交互环境跳过 Key 输入保留原值；修正 README 安装路径并注明沙箱内测试限制。

## 安装前验证

- 单元测试 41 项（新增非交互 configure 测试）：在 claude-cn 会话内 31 项通过；10 项 broker/proxy 测试因 AF_UNIX socket 被本工具隔离拒绝而无法在会话内运行，错误集合与 0.3.0 完全一致，需在会话外以 `make test` 复核全量。
- `verify_deb.py` 通过：双包结构、root 归属、provider 种子 0600、凭据分布、维护脚本语法、隔离导入检查；公共包重建逐字节一致，校验和更新于 `dist/SHA256SUMS`。
- 包元数据 `Depends` 不再包含 sudo；私有包权限 0600。

## 0.3.1 安装后核对（在 claude-cn 会话内执行）

- 已安装版本 0.3.1+local1；`dpkg-query` 确认 Depends 无 sudo。
- `/usr/lib/claude-cn/claude_cn`、`payload.py`、`service.py` 及五个 systemd 单元与仓库逐字节一致。
- `provider.json` 0600 root:root；旧 sudoers 文件与 enter 辅助程序均已不存在。
- `/run/claude-cn-launch/control.sock` 与 `/run/netns/claude-cn` 存在；本核对会话本身即经 0.3.1 broker 启动。
- 局限：会话内 seccomp 禁止 AF_UNIX，`systemctl`、doctor 服务项、`self-test`、`verify_launch.py` 无法由会话内代跑，需在会话外普通终端执行后确认。

以下为 0.3.0 验证记录。

# 验证记录：0.3.0

## 本次已完成

- 40 项自动化测试通过：原有网络策略、代理、seccomp、配置与路径测试，以及新增启动服务测试。
- 新服务测试使用真实 Unix SOCK_SEQPACKET / SCM_RIGHTS 和 fork/exec，验证内核对端 UID 拒绝、服务端身份检查、请求校验、FD 类型与消息大小限制、凭据和外部代理变量过滤。
- 客户端与测试服务实际运行：管道输入、分离 stdout/stderr、退出码、SIGTERM 转发、交互伪终端、`/dev/tty`、退出恢复终端，以及客户端被杀后的子进程清理。
- 公共包和私有包均通过包结构、root 所有权、配置权限、凭据分布、维护脚本语法、隔离导入检查；公共包重建内容一致。新包不再包含旧 enter 辅助程序。
- systemd 新服务定义通过 `systemd-analyze verify`。

上述启动服务测试以普通用户运行测试执行器，没有使用新 root 服务或真实 Claude；测试中只替换客户端 root 身份检查及最终隔离执行器，真实 IPC、UID 拒绝、终端转接和监督逻辑未替换。

## 0.3.0 安装后实测

用户已安装 0.3.0+local1，doctor 全部通过，启动服务 active，Claude 本体版本 2.1.303。

- `claude-cn self-test` 全通过：大陆 IPv4/IPv6、UDP/DNS、ping，境外直连拒绝、Unix socket 拒绝、代理拒绝境外 IP，百度和 GitHub 均 HTTP 200。
- `python3 scripts/verify_installed.py` 的 19 项真实网络探针通过，包括局域网高端口 TCP/UDP、SSH 握手、境外 TCP/UDP/ICMP 内核拒绝。用户配置内容保持不变。当前结果：`reports/installed-network.json`。
- `python3 scripts/verify_launch.py` 的 18 项实际启动检查通过，使用已安装的正式 root 服务，客户端身份检查和隔离执行器均未替换。在管道和交互 PTY 两种模式中确认 UID/EUID 都为 1000、有效 capabilities 为零、NoNewPrivs 与 seccomp 生效、专用网络命名空间、退出码传递、控制终端及终端恢复。实际进程祖先链中均无 sudo。旧 enter 和旧 sudoers 文件均已删除。结果：`reports/installed-launch-0.3.0.json`。
- 实际 Claude 在普通捕获输出和真实终端下均正常输出版本。实际智谱调用使用禁用工具、禁用会话保存的最小请求，返回预期 `CLAUDE_CN_OK`，exit=0、is_error=false，耗时约 2.26 秒。仅保存脱敏摘要：`reports/model-smoke-0.3.0.json`。

0.2.0 → 0.3.0 的实际升级、服务启动及授权迁移已验证。卸载/清理后的系统参数恢复尚未实机演练；完整交互聊天界面的长时间使用仍由正常使用观察。

以下为旧版本历史结果，不能作为新服务已安装测试的证据。

---

# 历史验证：0.2.0+local1

当前主机：KDE neon / Ubuntu 24.04，Claude Code 2.1.303。用户已完成 deb 安装及 `claude-cn-admin setup --user robin`。

## 实机结果

- `claude-cn doctor` 全部通过：当前 Claude 入口、独立配置、Key 是否填写、0600权限、用户授权和四个服务状态正常。
- `claude-cn --version` 返回 `2.1.303 (Claude Code)`，确认自动找到原生安装入口。
- `claude-cn self-test` 全部通过：大陆直接 IPv4/IPv6 TCP、UDP/DNS、ping，境外直接 IPv4/IPv6 拦截，境外 UDP 拦截，宿主机 Unix socket 拦截，代理拒绝未放行 IP，百度和 GitHub HTTPS 均返回200。
- 独立的已安装环境探针：19 项全部通过，详见 [reports/installed-network.json](reports/installed-network.json)。
- 一次禁用工具、禁用会话持久化的最小智谱调用成功，返回预期的 `CLAUDE_CN_OK`，约3.7秒；没有在报告中记录凭据，见 [reports/model-smoke.json](reports/model-smoke.json)。

19 项探针在与真实 Claude 相同的特权入口、专用网络命名空间和 seccomp 下，以普通用户UID1000运行。通过临时 PATH 可执行入口执行测试，不修改已安装程序、实际 Claude 或供应商配置，测试完成即清理。

覆盖内容：

| 项目 | 实机结果 |
|---|---|
| 普通UID、独立命名空间、NoNewPrivs、seccomp、专用DNS | 通过 |
| 局域网随机高端口直接TCP与UDP回显 | 通过 |
| 直接读取SSH banner与OpenSSH主机握手（不登录） | 通过 |
| 境外IPv4/IPv6直接TCP、UDP、ping | 内核拒绝 |
| 局域网高端口通过CONNECT隧道 | 通过 |
| 代理访问未放行境外IP | HTTP403 |
| GitHub的非允许端口22 | HTTP403 |
| 白名单域名对应境外IP绕过代理直连 | 内核拒绝 |
| 测试前后用户供应商配置一致 | 通过 |

IPv4 TCP被规则拒绝可能返回 `EHOSTUNREACH`（113），不能仅凭该错误证明防火墙拦截。测试同时要求来自本命名空间地址的 `Packet filtered` 或 `Administratively prohibited` 反馈，避免把普通路由失败当作通过。IPv6 TCP返回EACCES，UDP返回EPERM。

## 宿主机检查

宿主机仍处于原网络命名空间，DNS仍使用原来的127.0.0.53，宿主机非特权ping范围保持原值，原 `claude-glm` 未替换，用户供应商配置未改变。网络服务仍全部active。检查记录见 [reports/host-check.json](reports/host-check.json)。

宿主机直接访问部分境外地址可能超时，不能据此认定是本工具造成；本工具过滤仅匹配Claude专用接口，代理访问GitHub已实测成功。

## 安装前验证

- 30 项本地测试通过，包括通用供应商、Claude自动/显式路径、稳定入口升级跟随、权限和参数转发。
- 两个deb完成解包验证：控制信息、root文件归属、供应商种子0600、维护脚本语法、独立模块加载。
- 通用包载荷不含当前临时Key；私有包只在授权的供应商种子中包含该配置。
- 通用包重复构建逐字节一致，校验和在 `dist/SHA256SUMS`。
- sudoers规则通过visudo检查。

## 未覆盖的管理员操作

当前工具会话没有通用免密sudo权限，因此没有执行需要root的独立模拟路由器实验，也没有为了测试而停止服务、升级/卸载包或检查这些操作的参数恢复。这些生命周期流程经过代码和脚本检查，尚未实机演练。

安装后的真实命名空间已通过上述普通用户探针验证，不再仅依赖规则字符串或单元测试推断。现有包无需为这些测试重新安装。
