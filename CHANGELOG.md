# Changelog

## 0.4.1

- 修复剪贴板守护启动即崩溃：能力集补上 `CAP_CHOWN`（socket 移交 relay 用户）与 `CAP_DAC_READ_SEARCH`（读取授权用户的 `/proc` 会话环境）。
- socket 移交失败改为告警降级，不再以未处理异常退出触发重启循环。

## 0.4.0

- 沙箱内可直接向 Claude Code 粘贴截图与文本：只读剪贴板桥接，垫片经回环 relay 到达宿主守护，Claude Code 无需改动。
- 守护以授权用户身份运行真实 xclip / wl-paste；MIME 白名单（六种图片与 text/plain）、16 MiB 上限、5 秒超时与每分钟 30 次限速，逐次审计日志。
- 新增 `claude-cn-clip.service` 与 `claude-cn-clip-relay.service`，接入 doctor、admin 与卸载列表；deb 依赖加入 xclip 与 wl-clipboard。
- 沙箱 PATH 前置 `/usr/lib/claude-cn/sandbox-bin` 提供垫片，不影响 Claude 本体查找；写入方向与 primary 选择区永不放行。

## 0.3.1

- 移除 deb 中遗留的 sudo 依赖；日常启动与清理均不再需要 sudo 包。
- 启动代理轮询回收已结束会话，会话数上限不再把僵尸进程计入。
- 客户端对启动服务的畸形响应给出明确错误，不再抛出裸异常。
- `configure` 在非交互环境（脚本、管道）跳过 Key 输入并保留原值。
- 修正 README 安装路径，注明在 claude-cn 会话内运行单元测试会因隔离失败。

## 0.3.0

- 日常启动改为 root 系统服务代办，客户端不调用 sudo；升级自动移除旧免密授权。
- Unix socket 使用内核 UID 校验，限长请求与会话数，禁止传入 socket 标准流。
- 支持交互伪终端、窗口尺寸、信号、管道和退出码；连接断开清理会话进程组。
- 保留原网络规则、普通用户降权、供应商配置和 Claude 路径查找方式。

## 0.2.0

- 独立的 `claude-cn` 和管理员工具，不再使用外部 GLM 启动器。
- 通用供应商配置，默认继承智谱 GLM 5.3，支持兼容供应商的 API / 模型 / Key。
- 可配置 Claude 安装路径，默认动态查找稳定入口，支持原生和 npm/nvm 安装。
- 大陆与已放行局域网直接 TCP/UDP/ICMP，境外代理域名白名单。
- `.deb` 构建、私有/通用配置、升级与卸载生命周期。
