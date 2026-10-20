# 网络架构与宿主机影响


```text
claude-cn → 本机 socket → root 启动服务 → 降权后运行当前版本 Claude
                                    │
                            专用网络命名空间
                              ├─ TCP / UDP / ICMP 直接访问
                              │      ↓ 专用 veth
                              │   nftables 检查实际目的 IP
                              │      ├─ 大陆 / 已放行局域网 → NAT 出站
                              │      └─ 其他 → 拒绝
                              └─ 127.0.0.1:18080 代理
                                     ↓ socat → 外部白名单代理
                                       检查目的 IP 或境外域名例外
```

两层内核边界：命名空间输出规则、宿主机上仅匹配 `ccn-host` 接口的规则。Claude 没有修改规则所需的网络管理权限。境外白名单域名不会被转换为可直连 IP 白名单，避免共享 IP 扩大权限。

HTTP(S) 代理变量仅注入 Claude 进程树。HTTP(S) 默认经代理，直接 SSH、UDP、ping 不使用这些变量。境内 HTTP 也可用 `curl --noproxy '*' http://10.180.135.190:8080/` 直接访问。代理连接解析后检查过的数字 IP；HTTPS 使用 CONNECT，不解密 TLS、不记录正文、URL 路径或 API Key。

另有继承给子进程的 seccomp 规则，限制宿主机 Unix socket、io_uring、ptrace 和命名空间切换等绕过手段。不依赖 Claude 内置 Bash 沙箱。


## 宿主机改动


创建专用 `claude-cn` 命名空间、`ccn-host` / `ccn-peer` 虚拟网卡、专用 nftables 表和 NAT。为兼容 Docker 的 FORWARD DROP，增加带 `claude-cn` 注释、仅匹配专用接口的 FORWARD 例外。不清空其他规则，不改变默认防火墙策略、全局代理、DNS 或默认路由。

**路由式网络需要宿主机 IPv4 / IPv6 转发。** 未开启时会开启并记录原值；开启 IPv6 转发前将原有 `accept_ra=1` 调整为 `2`，保留宿主机接收路由通告的能力。停止服务/卸载时按记录尝试恢复未被其他管理员改动的参数。非特权 ping 权限只在 Claude 命名空间内开启。Claude 单独挂载允许的真实 DNS，宿主机 DNS 不变。

这不是完全零系统改动；全局转发开关可能与其他路由管理软件存在交互。其他防火墙管理器更晚执行的独立 DROP 规则也可能阻断 Claude；程序不会为解决此问题关闭其他防火墙。

## 启动服务边界

`claude-cn-launch.service` 保持在宿主机网络命名空间，以 root 运行。socket 位于 root 管理的 `/run/claude-cn-launch`，仅注册用户可连接，服务另用 `SO_PEERCRED` 核验 UID，客户端也验证服务端 UID 为 root。协议使用有界 SOCK_SEQPACKET 消息和 SCM_RIGHTS 传递三个标准输入输出 FD；拒绝 socket 类型的标准流，防止继承外部联网 socket 绕过网络规则。

每个会话由独立监督进程管理，启动子进程检查现有网络服务和规则，进入网络命名空间并创建私有 DNS 挂载后，先 initgroups/setgid/setuid，再处理用户工作目录和运行路径。终端控制也在降权后设置。进入 Claude 前关闭所有多余 FD，控制 socket 不会进入 Claude；seccomp 继续阻止其连接宿主机 Unix socket。最多同时接收 32 个会话，每个首包限 64 KiB、超时 10 秒。用户只能给自己的对应进程组发送允许的信号。

安装与维护仍需要管理员权限；日常启动不再依赖 sudo，也不再安装免密 sudoers 规则。

启动服务与代理使用独立生命周期：每日更新 IP 库和重启代理不会停止启动服务或其会话；新会话仍逐次检查网络规则和服务，依赖异常时拒绝启动。reset 和卸载显式先停止启动服务及其子进程。

## 剪贴板桥接

`claude-cn-clip.service`（root）在 `/run/claude-cn-clip/clip.sock` 监听；命名空间内 `claude-cn-clip-relay.service` 用 socat 把 `127.0.0.1:18081` 接到该 socket。沙箱内垫片只使用这条回环 TCP 通道，seccomp 与出站规则不变。守护从 `/proc` 发现授权用户的图形会话环境，降权后运行真实 `xclip`（X11）或 `wl-paste`（Wayland）读取剪贴板，仅回传白名单 MIME（六种图片格式与 `text/plain`），单次 16 MiB、超时 5 秒、每分钟 30 次限速，每次读取记录审计日志。

信任模型：这是唯一一条沙箱主动读取宿主桌面数据的通道。任意沙箱内进程都可在任意时刻发起读取，守护无法区分用户按键与程序行为；缓解依赖只读范围、MIME 白名单、审计日志与既有出站白名单。写入方向与 primary 选择区不提供。剪贴板含敏感文本时可停止上述两个服务关闭桥接，会话启动不受影响。
