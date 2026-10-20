# Changelog

## 0.4.4

- 每次剪贴板工具调用运行于独立进程组（子进程 `setsid`），超时/超限以 `killpg` 终止整组，工具派生的进程不再残留（评审复现：工具派生子进程后挂住，直接子进程被杀而派生进程存活）。
- 会话发现同时收集 X11 与 Wayland 后端，首选后端连接失败时自动尝试另一后端（覆盖 SSH X11 转发残留、陈旧 DISPLAY、认证不完整等场景），文本类型回退在新结构下逐后端逐类型尝试。
- 非目标（后续改进）：剪贴板守护迁移为用户服务、doctor 增加不读内容的桥接协议探测、限速按会话拆分并加锁（类型查询是否计额度一并评估）、relay 显式并发与空闲限制。

## 0.4.3

- 超时判定的 raise 移入终止流程：`_wait_bounded` 返回 None 后立即进入 `_terminate`（SIGKILL + 有界回收），残留子进程不再逃逸；测试新增子进程确实消失的断言。
- 请求解析改为先拆分命令与类型再走统一白名单：`GET text/plain;charset=utf-8` 等含 `;`/`=` 的类型不再被误判为畸形请求。
- 修复守护端到端测试未开启文本开关导致的失败，并在该测试中补充关闭文本后经 socket 拒绝的断言。

## 0.4.2

- 剪贴板工具的读取与退出回收共用同一截止时间：stdout 提前关闭、子进程滞留时按超时失败并 SIGKILL（单元补 `CAP_KILL`），不再无期限阻塞处理线程。
- Wayland 文本改为显式 `--type` 请求并附加 `--no-newline`，消除自动选型绕过 MIME 白名单与多余换行；X11 文本失败回退 `UTF8_STRING`，Wayland 回退 `text/plain;charset=utf-8`。
- 文本剪贴板默认关闭：policy.json 新增 `allow_clipboard_text`（默认 false），按需开启后重启 clip 服务生效。
- 剪贴板单元接入可选组件处理：setup/升级容忍其启动失败，doctor 改为提示不判失败。

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
