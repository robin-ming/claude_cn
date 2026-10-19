# 网络架构与宿主机影响


```text
claude-cn → root 管理的启动器 → 普通用户运行当前版本 Claude
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
