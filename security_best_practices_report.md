# claude-cn 0.4.6 安全架构审查

审查日期：2026-10-20。基线：`93504571108dfc3a4d79ee82bb999240a981a353`，审查开始时工作区干净。

## 结论

项目对日常网络出口控制已有合理基础：启动 broker 校验内核对端 UID，在运行用户代码前降权；代理检查 DNS 结果并连接检查过的数字 IP；网络命名空间与宿主接口规则提供两层限制；配置与服务故障时没有主动退回未隔离启动。

但这不是对恶意代码的完整隔离环境。本轮识别 1 项高风险、4 项中风险：剪贴板后端可被用作宿主网络连接代理；Unix datagram socketpair 绕过现有 socket 创建限制；会话清理可被新的 session/process group 绕过；IP 库完整性检查可被重复记录绕过；不同信任角色共用系统账号扩大了失陷后的影响范围。

前三项与项目已宣称的网络、IPC、会话边界直接相关，应先修复。后两项分别属于输入完整性问题与有前置条件的权限分区风险。没有发现可以据此直接断言任意 root 代码执行的证据。

## 范围与验证方法

阅读了启动器、broker/client、seccomp、网络规则、代理与策略、剪贴板、IP 库更新、配置写入、打包维护脚本和 systemd 单元。项目主要是 Python 标准库服务及 shell/systemd 配置；security-best-practices 技能的 references 中没有对应的标准库守护进程指南，本报告依据源码和针对性实验，不套用 Web 框架检查表。

全量单元测试：77 项通过。额外验证仅使用临时文件、受控子进程和本机临时 socket；未读取真实凭据或真实剪贴板，未向公网发送验证数据，未修改安装配置、服务或防火墙。

额外验证不等于完整部署攻击实验：剪贴板实验使用合成的 proc 环境和真实 xclip；broker 实验使用生产的会话监督逻辑和测试 fixture runner；未以 root 新建完整部署命名空间。以下逐项说明证据与适用条件。

## 高风险

### SEC-001：剪贴板环境发现信任同 UID 进程，可驱动宿主网络连接

**影响：当攻击者提供的会话环境被选中时，受限进程可借宿主剪贴板工具向网络策略之外的目标建立 X11 TCP 连接，并在认证握手中携带攻击者控制的数据。**

代码位置：

- [clip.py:140](claude_cn/clip.py#L140)：按 UID 扫描 `/proc`，没有排除受限会话进程，也没有验证环境来自可信桌面会话。
- [clip.py:175](claude_cn/clip.py#L175)：把发现的 `DISPLAY`、`XAUTHORITY` 传给真实 xclip。
- [claude-cn-clip.service:24](systemd/claude-cn-clip.service#L24)：允许 `AF_INET`，且守护没有加入受限网络命名空间。

沙箱中的程序可以为自己启动的子进程设置 DISPLAY 等变量；它和桌面进程使用相同 UID。若宿主没有更早被选中的有效桌面环境，例如无图形桌面的环境，伪造候选可能被采用。现有代码只取首个候选，因此这不是声称在所有桌面上都必然命中。

验证：在临时 proc 目录中放置当前 UID 的合成环境，将 DISPLAY 指向临时 TCP 监听端口，真实 xclip 发出了 X11 握手。进一步使用只含 `AUDIT_ONLY_12345` 的测试 Xauthority，临时监听端收到包含该标记的 48 字节握手。实际外网连通性还取决于宿主路由和防火墙；代码路径绕过了 claude-cn 命名空间的目的 IP 检查。

修复建议：

1. 本地剪贴板服务与工具只允许 `AF_UNIX`，拒绝 TCP 型远程 DISPLAY；在实际 systemd 单元下确认两种本地桌面均正常工作。
2. 不从任意同 UID 进程获取信任配置。近期至少排除受限网络命名空间中的进程；中期由明确的桌面会话启动用户桥接服务，固定其环境。
3. 将 Wayland endpoint 限定在预期的运行目录，校验类型、路径和所有者；不要把任意进程提供的路径当作桌面授权。
4. 增加“受限进程伪造 DISPLAY 不会产生宿主 TCP 连接”的隔离测试。仅验证 MIME 不足以验证这条边界。

## 中风险

### SEC-002：Unix datagram socketpair 可以重新连接宿主文件系统 socket

代码位置：[guard.py:45](claude_cn/guard.py#L45)。

seccomp 拒绝 `socket(AF_UNIX, ...)`，但允许 `socketpair()`。Unix datagram socketpair 的一端可以再次 `connect()` 到其他路径；当前规则没有阻断这条路径。

验证：子进程安装生产 guard 后，普通 AF_UNIX socket 创建返回 EPERM；随后创建 AF_UNIX/SOCK_DGRAM socketpair，连接父进程绑定的临时路径并发送消息，父进程成功收到 `guarded-process-to-host`。这个文件系统路径通道不会因仅隔离网络命名空间而消失。

影响受目标 socket 权限与协议限制。本轮没有证明可直接访问 Docker、SSH agent 或 D-Bus 的流式服务，也不应将 datagram 结论扩大为所有 Unix socket 均可绕过。

修复建议：确认运行时 IPC 需求后，至少禁止 AF_UNIX/SOCK_DGRAM 的 socketpair，类型检查应正确处理 CLOEXEC/NONBLOCK 位；为 STREAM、DGRAM、SEQPACKET 分别建立正反向测试。若要允许复杂 IPC 同时限制宿主路径，使用适当的 LSM/文件系统隔离策略，不单靠 socket 创建黑名单。

### SEC-003：会话进程组清理不能覆盖主动脱离的后代

代码位置：[broker.py:63](claude_cn/broker.py#L63)、[broker.py:108](claude_cn/broker.py#L108)、[claude-cn-launch.service:18](systemd/claude-cn-launch.service#L18)。

broker 在断开时清理原始进程组和直接子进程。用户程序可以派生进程并调用 setsid，创建新的 session/process group；直接主进程正常退出时，broker 又将 pid 置空，不再清理后代。

验证：使用生产 `broker.session()` 和已有测试 fixture runner，payload 安装 guard 后派生 `start_new_session=True` 的休眠进程。关闭客户端连接后，监督进程正常结束，脱离的后代仍存活；验证结束后已主动清理。

影响：任务可在用户认为会话结束后继续执行，MAX_SESSIONS 不代表存活任务总数；同一服务级 TasksMax 也不是每会话资源限制。`KillMode=control-group` 在整个服务停止时仍有作用，因此本报告不声称它能逃过服务级停止。

修复建议：每会话建立独立、非用户可迁出的 cgroup，退出或断开时终止整个 cgroup，并设置每会话任务数、内存和必要的 CPU 限额。不要用扫描一次进程树替代 cgroup；增加正常退出、断开、setsid、双重 fork 等清理测试。

### SEC-004：IP 库最小条数在去重前校验，不能保证完整性

代码位置：[update.py:17](claude_cn/update.py#L17)、[update.py:46](claude_cn/update.py#L46)、[policy.py:62](claude_cn/policy.py#L62)。

`parse_list()` 先检查原始记录数，再折叠网络。验证输入为 1000 条重复的 `1.0.0.0/8`，设置 IPv4 最少 1000 条，函数仍成功返回仅含一个网段的列表。

这证明完整性保护可被重复数据绕过，不代表已发现更新站点被入侵。要影响实际策略，需要更新源返回异常/恶意数据，或攻击者能修改可信缓存；普通沙箱用户不能据此直接改写 root 策略。

修复建议：检查去重后的有效记录数量及总地址覆盖变化，对相对旧版本的大幅扩张或收缩拒绝自动提升；在消费缓存时复用必要的结构和范围校验。下载到 staging 后验证、提升为活动库，失败保留已验证旧版本。若希望降低对单个发布源的信任，需要独立可验证的签名或来源交叉核对；HTTPS 不能证明网段归属本身正确。

### SEC-005：代理、更新器、两种 relay 共用身份，信任分区不足

代码位置：

- [claude-cn-proxy.service:7](systemd/claude-cn-proxy.service#L7)。
- [claude-cn-update.service:8](systemd/claude-cn-update.service#L8)。
- [claude-cn-clip-relay.service:8](systemd/claude-cn-clip-relay.service#L8)。
- [packaging/postinst:8](packaging/postinst#L8)。
- [clip.py:432](claude_cn/clip.py#L432)：剪贴板 socket 授权给同一 PROXY_USER。

HTTP 代理解析不可信请求并拥有宿主出口权限，剪贴板 relay 则代表桌面读取权限，更新器可修改被 root 网络规则消费的数据库。这些角色共用 `claude-cn-proxy` 身份。若代理出现代码执行漏洞，攻击者将同时具有连接剪贴板 Unix socket 的身份，削弱 ProtectHome 所表达的数据隔离。

此项是有“代理或同身份组件已失陷”前置条件的架构风险，本轮没有发现或利用代理 RCE。代理的 `ProtectSystem=strict` 确实限制其直接写缓存，不能仅凭文件属主相同断言它当下能从自身挂载视图直接改库。

修复建议：拆分 HTTP proxy、剪贴板 relay 和 updater 的服务身份；剪贴板 socket 仅授权专用 relay，并检查对端身份。更新器只写 staging，由小型 root 提升步骤验证后替换 root 管理的活动库。同步检查 socket 父目录、服务可写目录和各身份对彼此进程的可见性。

## 需要明确接受的架构限制

### 共享宿主用户文件系统

[entry.py:85](claude_cn/entry.py#L85) 恢复用户完整附加组并降为宿主用户，私有挂载主要用于 DNS，没有建立项目目录写入白名单。程序可以读写该用户有权限访问的 HOME、凭据及启动配置。修改 shell 启动文件、用户服务或主机稍后执行的脚本，可在宿主后续执行时脱离本项目的网络约束。

[README.md:159](README.md#L159) 已明确“不是完整文件沙箱”，因此这是当前范围限制，不作为新发现的未披露实现漏洞。但若目标是约束不可信代码或受提示注入影响的命令，仅网络命名空间不足以实现该目标。

建议提供独立的严格模式：使用专用身份或明确的文件系统隔离，HOME 采用独立目录，仅放行项目和必要缓存写入；保护宿主启动文件、用户服务定义与无关凭据。内核版本、应用兼容性和工作流验证后再选择 mount namespace、LSM 或 Landlock 等组合；不要仅增加几条路径黑名单就宣称完成隔离。

### 目的地放行不等于防数据外泄

默认配置放行大陆公网全部端口、10.0.0.0/8 和若干局域网，以及 GitHub 等境外域名。数据可以被发送到允许的模型 API、用户可写的托管服务或允许网络中的转发器。CONNECT 代理也不验证隧道内的 TLS SNI、HTTP Host 或业务用途。

这些行为大部分已在 README 解释，是网络策略选择，不应误报为一次新的白名单绕过。需要更强保护时，应提供最小出口配置：按业务指定目标与端口、默认不开放整个内网，并单独管理宿主机服务可达性。不要把大陆 IP、允许域名或只读剪贴板当作内容保密保证。

## 其他非阻塞改进

- 剪贴板读取迁到用户桌面服务，减少 root 守护所需 capabilities；迁移必须连同上述网络与 endpoint 限制实施。
- broker、clip 的服务权限与资源限额还可收紧，但 root broker 的 mount/setns 功能需要精确保留能力，不能照搬代理的空能力集。
- doctor 目前主要检查服务 active，建议加入不读取剪贴板内容的协议探测、网络边界实际检查与数据源状态报告。
- 剪贴板限速加锁，区分 TARGETS 与数据读取；relay 显式设置并发和连接时间限制。已有守护处理槽位会关闭超额连接，不能仅凭 socat fork 就断言无限空闲连接漏洞。
- 私有包建议完全在 0700 临时输出目录构建并在 0600 模式下发布，减少用户指定已有宽权限输出目录时最终 chmod 前的暴露窗口；避免把携带长期凭据的包作为常规分发形式。

## 修复顺序与验收

1. SEC-001：先关闭剪贴板后端的 TCP 能力，拒绝非本地 DISPLAY，防止本项目自身提供越界联网桥。
2. SEC-002：封堵 datagram socketpair 外连宿主路径，并确认所需 IPC 兼容性。
3. SEC-003：用每会话 cgroup 管理退出、断开与资源回收。
4. SEC-004：修复重复记录校验，建立活动 IP 库提升前验证。
5. SEC-005：拆分服务身份和活动缓存写入权限。
6. 按实际威胁模型决定是否增加严格文件系统模式，而不是默认把当前网络工具当作恶意代码沙箱。

验收应在实际 systemd 权限、真实命名空间和桌面环境下重复对应安全实验，并执行现有功能测试。原有 77 项通过只能证明这些测试覆盖的行为正确，不能替代本报告列出的安全属性测试。
