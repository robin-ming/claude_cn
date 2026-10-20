# claude-cn

通用的 Claude Code 供应商配置与网络控制工具，默认继承智谱 GLM 5.3 配置。可切换其他提供 Claude Code 所支持接口的供应商（通常需要 Anthropic 兼容接口）；只有 OpenAI 格式的接口需要额外转换。

本项目自己提供启动器、供应商配置、网络服务和 Debian 安装包，**不依赖或修改 `claude-glm`**。Claude Code 本体需另外安装，支持配置安装入口，不随包重分发。

## 安装

本机包 `claude-cn_0.3.1+local1_all.deb` 带你授权复制的临时智谱配置；通用包 `claude-cn_0.3.1_all.deb` 只有空 Key 模板。两者只能选一个安装，包名均为 `claude-cn`。

```bash
cd ~/Storage/Project/claude_cn
sudo apt install ./dist/claude-cn_0.3.1+local1_all.deb
sudo claude-cn-admin setup --user robin
claude-cn doctor
claude-cn --version
claude-cn
```

安装包不在首次安装时自动启用网络或修改用户目录；`setup` 明确注册一个运行用户、初始化独立配置并启用服务。已有供应商配置不会被覆盖。无需退出登录，日常启动不调用 sudo。

```bash
cd ~/your-project
claude-cn --resume
claude-cn run --help          # Claude Code 自己的帮助
claude-cn version             # 本项目版本
```

## 无 sudo 的日常启动

`claude-cn` 通过本机 Unix socket 向 `claude-cn-launch.service` 请求启动。服务用内核提供的对端 UID 核验已注册用户，子进程进入现有网络命名空间、设置 DNS，再降回该用户并安装 seccomp 后运行 Claude。root 服务不执行用户代码。

交互会话使用伪终端，支持窗口尺寸、信号和终端恢复；管道保留独立的标准输入、输出和错误流。客户端断开会清理对应进程组。服务失效时拒绝启动，不回退到 sudo 或未隔离的 Claude。

`claude-cn-admin` 继续负责系统管理，`setup`、`reload`、`reset` 需要管理员权限；日常启动和供应商配置不需要。

从 0.2.0 升级时自动启用新服务并删除旧 `/etc/sudoers.d/claude-cn`。已有用户无需重复 setup，首次安装仍需注册用户。升级会重启有关服务，请先退出正在运行的 Claude 会话。

## Claude 安装路径

管理员配置 `/etc/claude-cn/runtime.json`：

```json
{
  "claude_path": "auto",
  "settings_path": "~/.config/claude-cn/settings.json"
}
```

`auto` 每次启动依次查找调用者 PATH 中的 `claude`、`~/.local/bin/claude`、`~/.npm-global/bin/claude`、`/usr/local/bin/claude`、`/usr/bin/claude`。支持原生安装器和 npm/nvm 入口，PATH 保留，因此 npm 脚本可找到 Node。shell alias/function 不属于可执行文件查找范围。

也可将 `claude_path` 设置为稳定的绝对路径或 `~/...`，例如 `/opt/claude/bin/claude`。不要填固定的 `versions/2.x.x`。项目保留入口路径、不解析后锁定版本；原生安装器切换符号链接或 PATH 随 nvm 更新时自动跟随。显式配置的路径不可用时直接报错，不擅自换成其他程序。改路径无需重新安装，下一次启动读取新配置。

`settings_path` 默认值可独立配置，同样支持 `~/`（按运行用户展开）。改配置位置后用 `claude-cn init` 初始化，或自行放置0600权限的配置文件。

## 模型供应商

默认模型和所有模型别名为 `glm-5.3[1m]`，默认地址为 `https://open.bigmodel.cn/api/anthropic`。私有包继承当前环境的临时 Key，首次 setup 复制到独立配置；后续不读取任何旧启动器或旧配置目录。

三类配置分别是：

| 文件 | 用途 |
|---|---|
| `/etc/claude-cn/runtime.json` | Claude 程序与用户供应商配置路径 |
| `/etc/claude-cn/provider.json` | 首次初始化用的供应商种子，root 0600 |
| `~/.config/claude-cn/settings.json` | 实际使用的供应商地址、Key、模型，用户 0600 |

修改 Key（隐藏输入、不放入命令行参数）：

```bash
claude-cn configure
```

切换其他兼容供应商：

```bash
claude-cn configure --base-url https://api.example.cn/anthropic --model your-model
```

也可直接编辑用户 `settings.json` 的 `env`，使用 Claude Code 支持的供应商配置。示例在 [config/zhipu.example.json](config/zhipu.example.json)。公网大陆 API 自动允许，境外 API 仍需要管理员显式加入网络域名白名单。本机/局域网 API 地址需命中允许的 CIDR。

通用包初始化：

```bash
claude-cn init               # 不覆盖已有配置
claude-cn configure
# 或导入你自己的 Claude settings JSON：
claude-cn init --from /path/to/settings.json
```

## 网络策略

| 目标 | 直接 TCP / UDP / ICMP | HTTP/CONNECT 代理 |
|---|---|---|
| 大陆 IPv4 / IPv6 | 允许，不限制端口 | 所有 TCP 端口 |
| 明确放行局域网 | 允许，不限制端口 | 所有 TCP 端口 |
| 境外域名白名单 | 境外 IP 直连拒绝 | 默认 TCP 80/443 |
| 其他境外目标 | 拒绝 | 拒绝 |

局域网默认包含 `10.0.0.0/8`、`192.168.8.0/24`、`192.168.10.0/24`。境内 SSH、UDP、DNS、ping 有直连通路，HTTP(S) 默认经项目代理；`curl --noproxy '*'` 可对允许目标直接联网。宿主机其他进程不进入此命名空间。

```bash
sudoedit /etc/claude-cn/policy.json
sudo claude-cn-admin reload
```

`allow_domains` 配境外代理域名，`*.example.com` 不包含根域名；`allow_cidrs` 配额外直连 IP 网段。`ports` 只限制境外代理端口，默认 `[80,443]`。大陆按运营商/BGP/登记 IP 库判断，存在误差，不保证物理传输路径不经过境外。允许服务后续转发不在策略范围内。

详细架构和宿主机参数影响见 [docs/NETWORK.md](docs/NETWORK.md)。网络采用 veth、双栈 NAT、两层 nftables；按需开启宿主机转发并保留 IPv6 RA 行为，停服务时尝试恢复原值。ping 权限仅在专用命名空间开启。规则仅匹配项目接口，不清空其他规则或改变默认防火墙策略。

## 验证与维护

```bash
claude-cn doctor
claude-cn self-test
sudo python3 /usr/share/claude-cn/integration_network.py
journalctl -u claude-cn-netns.service -u claude-cn-proxy.service -n 60 --no-pager
```

`self-test` 不调用模型 API，检查直接 TCP/UDP/ping 和代理边界。独立 root 实验只在临时命名空间中验证，避免真实站点不回应造成歧义。内核转发部分需要管理员安装后测试；本地单元测试不等于已通过真实网络实验。

IP 库从作者境内镜像更新，初始快照随包提供，安装不依赖即时下载。每天定时校验更新，失败保留旧库，成功后原子重载规则并重启代理。10808 仅用于本轮开发临时下载，未写入持久配置。

```bash
sudo systemctl start claude-cn-update.service
sudo apt install ./dist/claude-cn_NEW_VERSION_all.deb
```

软件包升级前停止旧服务并恢复网络，升级后为已注册用户重新启用；用户供应商配置不覆盖，系统 JSON 由 dpkg conffiles 管理。首次安装不自动绑定某个用户名。

## 构建

```bash
make test
make deb                     # 通用包，不含 Key
make deb-private             # 私有包，读取 gitignore 中的 config/zhipu.local.json
```

需要 Python 3.12、dpkg-deb，不需要 root、Docker、第三方 Python 模块。没有快照时先运行 `python3 -m claude_cn.update --output data/cn-networks.json`。构建采用文件白名单，不复制用户 HOME、Claude 私有二进制或临时构建文件。在 claude-cn 会话内运行 `make test` 会因 AF_UNIX socket 被隔离拒绝而失败，请在会话外的普通终端执行测试。

`+local1` 包包含临时凭据，仅供本机使用；包和本地配置权限0600，通用包不包含这些凭据。密钥不会打印到构建或诊断输出中。

## 停用与卸载

```bash
sudo claude-cn-admin reset    # 停服务并撤销运行授权，保留配置
sudo apt remove claude-cn     # 停服务并删除包，保留系统/用户配置
sudo apt purge claude-cn      # 另清除系统配置和 IP 缓存，保留用户配置
```

项目不修改原有 `claude-glm` 或 `claude`。这是网络出口工具，不是完整文件沙箱；宿主机 Unix socket、SSH agent、D-Bus 和 Docker socket 仍受限，SSH 可用密钥文件或密码连接。命名空间的 localhost 指自身；其他防火墙或实际路由仍可能影响连通性。
