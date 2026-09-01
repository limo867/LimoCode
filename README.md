# Local Coding Agent

这是一个从零实现的本地编程智能体框架。目前已完成前六轮核心能力：OpenAI 兼容 Chat Completions 客户端、多轮 Tool Calling、本地文件和命令工具、工作区安全、命令安全、上下文限制与故障恢复。文件与命令工具始终在本机执行。

## 运行

需要 Python 3.10+：

```powershell
python main.py --demo "列出项目文件"
```

### 推荐：终端交互界面

本地交互使用终端优先的 LimoCode 风格 TUI：任务过程以连续事件流显示，可查看历史任务、调整运行配置，并在高风险命令出现时进行本地确认。

```powershell
python -m coding_agent.tui --workspace .
```

启动后直接输入编程任务；输入 `/help` 查看命令。常用命令为：`/config` 查看或调整运行配置、`/history` 或 `/resume` 选择可恢复的历史会话、`/open <任务 ID 前缀>` 查看指定任务、`/new` 新建会话、`/compact` 压缩当前会话上下文、`/clear` 清屏，以及 `/quit` 退出。在全屏 TUI 中，输入 `/history` 或 `/resume` 后可用方向键或鼠标选中会话并按 Enter；选中后该会话成为当前会话，之后直接输入普通文本即可带着其上下文继续。只有希望开始无关联的新任务时才使用 `/new`。真实模型模式下，先配置 `LLM_API_KEY`，再省略 `--demo` 启动。

首次启动某个工作区时，TUI 或 Web 会要求确认是否信任该目录。确认后会按解析后的绝对路径保存到用户级 `~/.limocode/trusted-workspaces.json`；若该目录不可写，则自动使用工作区 `.coding-agent/trusted-workspaces.json` 回退。两个入口读取同一份记录，因此以相同的绝对 `--workspace` 启动后不会重复询问；通过 `/config workspace <目录>` 切换工作区时会重新检查。该信任只允许 Agent 在该工作区内执行本地任务，危险命令仍会单独请求确认。

### 长任务能力

以下命令会参与 Agent 的实际运行上下文，而不是仅展示状态：

- `/model`、`/models`、`/model <name>`：查看、列出和切换模型。通过 `LLM_MODELS` 配置可切换列表；会话消息保存于任务 SQLite 数据库，重启后输入 `/history` 或 `/resume` 选择任务，后续普通输入即可继续。`/continue <指令>` 保留为选择最新可恢复任务的兼容快捷方式。
- `/skills`、`/skill <name>`、`/skill auto`、`/skill reload`：发现、手动选择、恢复自动选择与重新加载 Markdown Skill。Skill 从工作区 `skills/<name>/SKILL.md`、用户目录 `~/.limocode/skills/` 和内置 Skill 中发现，工作区内容优先。
- `/memory`、`/memory add <内容>`、`/memory search <查询>`、`/memory delete <id>`、`/memory status`：管理和检查项目长期记忆。默认保存至工作区 `.coding-agent/memory.sqlite3`；每次任务和 `/continue` 都会重新按相关性检索，并在独立预算内注入模型上下文。
- `/compact`：手动压缩当前已选会话的完整短期上下文，而不是只压缩某一个任务。它会选择该会话最新可续接任务保存的消息快照，生成摘要并写回 SQLite；重启后先用 `/resume` 选中会话，同样可以压缩，不会重新执行任何任务。自动压缩会在 `AGENT_MAX_CONTEXT_TOKENS * AGENT_COMPACTION_THRESHOLD` 达到阈值时触发，并保留会话目标、进度、状态、决策、错误、文件与下一步。`/memory status` 同时显示当前短期消息数、估算 token、摘要和可续聊状态。

长期记忆是工作区级别的持久规则，不与任务历史混在一起。推荐通过 `/memory add <规则>` 显式保存；任务中只有包含“记住 / remember”的明确请求才会自动保存，避免把一次性任务约束误记为项目规则。短期记忆是当前会话的消息、工具结果和压缩摘要；任务完成后可直接继续当前会话，重启后通过 `/history` 或 `/resume` 选择会话恢复。输入 `/memory status` 可检查两层记忆的实际状态。

Skill 文件示例：

```markdown
---
name: debugging
description: Reproduce, diagnose, and fix software failures.
---

# Workflow
1. Reproduce the issue.
2. Read the exact error.
3. Add or run a regression test after the fix.
```

### 一次性配置真实模型

将 [`.env.example`](.env.example) 复制为用户级配置文件 `~/.limocode/.env`，填入自己的 Key；程序会自动读取它，且该文件不在项目中，不会进入 Git。显式设置的系统环境变量优先于配置文件，适合临时切换模型。旧版 `~/.local-codex/.env` 仍会被兼容读取。

```dotenv
LLM_API_KEY=你的真实密钥
LLM_MODEL=gpt-4o-mini
LLM_BASE_URL=https://api.openai.com/v1
# 仅在网络要求通过代理访问模型服务时设置，例如 http://127.0.0.1:7890
LLM_PROXY=
LLM_MODELS=gpt-4o-mini,gpt-4.1-mini
AGENT_MAX_TURNS=60
AGENT_MAX_CONTEXT_TOKENS=128000
AGENT_COMPACTION_THRESHOLD=0.8
AGENT_MEMORY_CONTEXT_CHARS=4000
```

若真实模型请求显示 `WinError 10013`，说明 Windows 在 HTTP 请求到达模型服务前拦截了 Python 的出站连接，不是 API Key 或工作区信任问题。请从正常用户权限的 PowerShell 启动 `web_server.py` 或 `limocode`，并检查防火墙/安全软件是否允许 `python.exe` 访问模型域名的 HTTPS 端口；可先运行 `Test-NetConnection <模型域名> -Port 443` 验证（当前 DeepSeek 配置对应 `api.deepseek.com`）。所在网络必须走代理时，在 `.env` 中填写完整的 `LLM_PROXY=http://主机:端口`，然后重启服务。

之后每次直接运行即可：

```powershell
python -m coding_agent.tui --workspace .
```

需要为某个项目单独覆盖模型或限额时，可在该项目根目录放置 `.env`；其优先级高于用户级配置。

### 任意目录启动

在项目目录执行一次安装：

```powershell
python -m pip install -e .
```

之后可以在任意目录直接启动；未传入 `--workspace` 时，当前目录就是 Agent 的工作区：

```powershell
cd D:\你的项目
limocode
```

若要使用另一个工作区，可显式指定：`limocode --workspace D:\另一个项目`。

运行全部检查：

```powershell
.\scripts\test.ps1
```

常用 CLI 参数：`--workspace`、`--model`、`--model-timeout`、`--max-turns`、`--timeout`、`--approval-timeout`、`--min-request-interval`、`--color auto|always|never`、`--demo`、`--log-file`。全屏 TUI 默认 `--color auto`，即使父进程注入 `NO_COLOR` 也会保留终端中的语义颜色；需要无色显示时使用 `--color never`。日志文件只保存状态和工具摘要，不保存 API Key。

文件变更默认使用 `Approval` 模式：Agent 先生成带 Diff 的 ChangeSet，只有你确认后才写入工作区。可在 TUI 使用 `/mode auto`，或在 Web 的 Runtime settings 中切换至 Auto；Auto 仍保存 Diff，并可从 ChangeSet 执行 Undo。Undo 会检测 Agent 写入后文件是否被用户修改，检测到冲突时不会覆盖用户内容。

危险命令默认不会执行。需要预先放行的命令可完整写入 `AGENT_APPROVED_COMMANDS`，多条命令以 `;;` 分隔；只允许完全匹配，建议仅用于受控演示环境。未在该列表中的高风险命令会在 TUI、Web 与桌面 GUI 中请求本地人工确认；拒绝、取消或超过 `AGENT_COMMAND_APPROVAL_TIMEOUT`（默认 120 秒）都不会执行命令。

当同一服务进程需要并发执行真实模型任务时，可设置 `LLM_MIN_REQUEST_INTERVAL_MS` 为相邻模型请求的最小间隔（毫秒）。默认值 `0` 表示不额外限流；限流等待会显示为任务事件，并能响应任务取消。

真实模型运行前设置 `LLM_API_KEY`（以及可选的 `LLM_BASE_URL`、`LLM_MODEL`）：

```powershell
$env:LLM_API_KEY = "your-key"
python main.py "检查并修复测试"
```

指定工作区：

```powershell
python main.py --workspace . "检查并修复测试"
```

## 目录

- `coding_agent/config.py`：环境变量和运行配置
- `coding_agent/workspace.py`：工作区边界校验
- `coding_agent/tools.py`：工具声明与本地执行
- `coding_agent/agent.py`：模型客户端接口和 Agent 循环
- `main.py`：命令行入口
- `coding_agent/service.py`：任务服务、事件和取消
- `coding_agent/tui.py`：推荐的终端交互入口
- `coding_agent/web.py`、`web_server.py`：本地 Web API、SSE 和静态前端
- `coding_agent/gui.py`：保留的 tkinter 桌面入口
- `frontend/index.html`：浏览器界面

启动 Web 界面：

```powershell
python web_server.py --demo --workspace .
```

`web_server.py` 支持与 CLI 一致的 `--workspace`、`--model`、`--model-timeout`、`--max-turns`、`--timeout`、`--approval-timeout` 和 `--min-request-interval` 配置参数。启动参数提供默认工作区以及模型和运行限制；浏览器可在同一服务中为不同项目分别新建会话。

浏览器界面是一个本地工作台：左侧按当前工作区显示会话历史和可展开的代码文件树，中间以流式 Markdown 展示每一轮请求、回答和可展开的工具活动，右侧可管理长期记忆、Skill、模型与会话上下文。完成的会话可直接继续，文件写入会在工具详情中展示前后预览；输入 `/` 可打开常用操作、历史会话和模型选择菜单。

### Web 工作区

点击 Web 页面的“新建会话”会先打开工作区选择器。可从已知项目中选择，或输入一个存在的本地目录并检查；未信任的目录必须先确认信任，之后才能开始任务和浏览文件。选中的目录会绑定到新会话，进入会话后左侧文件树只展示该目录；需要处理另一个项目时，应从选择器新建一个会话，而不是在已有会话中切换目录。

Web、TUI 与桌面 GUI 使用同一份按绝对路径记录的工作区信任，因此已经在 TUI 中信任过的同一目录不会在 Web 中重复询问。信任只允许该目录用于 Agent 工作和 Web 文件浏览；高风险命令仍须单独审批。为避免意外暴露元数据或越界读取，Web 文件树按目录逐层加载，忽略 `.git`、`.coding-agent`、依赖/缓存目录和符号链接。

每个通过 Web 选择的项目拥有独立的 Agent 服务：命令在该项目根目录执行，任务历史、短期会话上下文和长期记忆不会与其他项目混合；新选择的项目默认将持久状态保存在自己的 `.coding-agent/` 下。会话也始终绑定其创建时的工作区，跨工作区续聊会被拒绝。服务启动时读取的模型、网络和运行限制会用于这些项目；为避免服务端在浏览任意目录时加载未知配置，选择项目不会读取该项目的 `.env`。

启动保留的桌面 GUI（需要系统提供 tkinter）：

```powershell
python -m coding_agent.gui --demo --workspace .
```

桌面 GUI 是可选入口；日常本地使用建议优先使用 TUI。桌面 GUI 在任务开始前提供与 CLI/Web 一致的工作区、模型、API 超时、轮数、命令超时、危险命令审批等待时间和模型请求间隔设置。事件日志、命令输出、文件变更预览和最终答复显示在独立视图中。

在具有图形桌面会话的系统上，可执行不运行 Agent 任务的窗口启动检查：

```powershell
python scripts/gui_smoke.py --workspace .
```

该检查只验证 tkinter 窗口与本项目 GUI 可创建并关闭；它不替代对 macOS、Linux 或实际交互流程的人工验证。
本项目已于 2026-08-29 在 Windows 图形会话中运行该检查；其他平台仍需分别验证。

端到端演示任务见 `examples/demo-task.md`。Web API 默认监听 `127.0.0.1`，任务请求可通过 `demo: true` 使用离线模型。

Web 页面现在包含任务历史、实时 SSE 事件、事件去重与断线重连；事件详情中的工具参数和命令输出可展开查看。
文件写入事件还会展示受限的修改前后预览；大文件只显示截断内容。

## Web 展示截图

下列截图使用本机 Edge 无头模式运行离线 Demo 任务后生成，不包含 API Key 或真实模型数据：

- [桌面布局（1440 x 900）](examples/web-demo-desktop.png)
- [窄屏布局（390 x 844）](examples/web-demo-mobile.png)

浏览器可使用 `?task=<task-id>` 直接打开历史任务，便于展示事件时间线和最终结果。

Web API 文档见 `api.md`。GitHub Actions 会在推送和拉取请求时运行编译检查。

真实模型演示的去敏录制步骤见 `examples/real-model-demo.md`。录制产物默认不会被 Git 跟踪。

运行本地检查：

```powershell
scripts\test.ps1
```

本地回归测试和开发笔记保存在被 Git 忽略的 `.local-dev/` 中；公开仓库只保留可复现的编译检查。
