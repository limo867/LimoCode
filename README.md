# Local Coding Agent

这是一个从零实现的本地编程智能体框架。目前已完成前六轮核心能力：OpenAI 兼容 Chat Completions 客户端、多轮 Tool Calling、本地文件和命令工具、工作区安全、命令安全、上下文限制与故障恢复。文件与命令工具始终在本机执行。

## 运行

需要 Python 3.10+：

```powershell
python main.py --demo "列出项目文件"
```

### 推荐：终端交互界面

本地交互使用终端优先的 Codex 风格 TUI：任务过程以连续事件流显示，可查看历史任务、调整运行配置，并在高风险命令出现时进行本地确认。

```powershell
python -m coding_agent.tui --workspace .
```

启动后直接输入编程任务；输入 `/help` 查看命令。常用命令为：`/config` 查看或调整运行配置、`/history` 查看最近任务、`/open <任务 ID 前缀>` 重放历史事件、`/clear` 清屏，以及 `/quit` 退出。真实模型模式下，先配置 `LLM_API_KEY`，再省略 `--demo` 启动。

### 长任务能力

以下命令会参与 Agent 的实际运行上下文，而不是仅展示状态：

- `/model`、`/models`、`/model <name>`：查看、列出和切换模型。通过 `LLM_MODELS` 配置可切换列表；切换后使用 `/continue <指令>` 可保留上一任务的消息、工具结果与摘要继续执行。会话消息保存于任务 SQLite 数据库，重启后先用 `/open <任务 ID 前缀>` 选中任务即可继续。
- `/skills`、`/skill <name>`、`/skill auto`、`/skill reload`：发现、手动选择、恢复自动选择与重新加载 Markdown Skill。Skill 从工作区 `skills/<name>/SKILL.md`、用户目录 `~/.local-codex/skills/` 和内置 Skill 中发现，工作区内容优先。
- `/memory`、`/memory add <内容>`、`/memory search <查询>`、`/memory delete <id>`：管理项目长期记忆。默认保存至工作区 `.coding-agent/memory.sqlite3`，每次任务只检索相关条目注入上下文。
- `/compact`：手动压缩最近任务上下文。自动压缩会在 `AGENT_MAX_CONTEXT_TOKENS * AGENT_COMPACTION_THRESHOLD` 达到阈值时触发，并保留任务、进度、状态、决策、错误、文件与下一步。

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

将 [`.env.example`](.env.example) 复制为用户级配置文件 `~/.local-codex/.env`，填入自己的 Key；程序会自动读取它，且该文件不在项目中，不会进入 Git。显式设置的系统环境变量优先于配置文件，适合临时切换模型。

```dotenv
LLM_API_KEY=你的真实密钥
LLM_MODEL=gpt-4o-mini
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODELS=gpt-4o-mini,gpt-4.1-mini
AGENT_MAX_CONTEXT_TOKENS=128000
AGENT_COMPACTION_THRESHOLD=0.8
```

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
local-codex
```

若要使用另一个工作区，可显式指定：`local-codex --workspace D:\另一个项目`。

运行全部检查：

```powershell
.\scripts\test.ps1
```

常用 CLI 参数：`--workspace`、`--model`、`--model-timeout`、`--max-turns`、`--timeout`、`--approval-timeout`、`--min-request-interval`、`--demo`、`--log-file`。日志文件只保存状态和工具摘要，不保存 API Key。

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
- `current-status-roadmap.md`：当前功能盘点、限制与后续路线

启动 Web 界面：

```powershell
python web_server.py --demo --workspace .
```

`web_server.py` 支持与 CLI 一致的 `--workspace`、`--model`、`--model-timeout`、`--max-turns`、`--timeout`、`--approval-timeout` 和 `--min-request-interval` 配置参数。所选工作区及其运行限制会应用到该本地进程服务的全部浏览器任务。

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

Web API 文档见 `api.md`。GitHub Actions 会在推送和拉取请求时运行测试与编译检查。

真实模型演示的去敏录制步骤见 `examples/real-model-demo.md`。录制产物默认不会被 Git 跟踪。

运行基础测试：

```powershell
python -m unittest discover -s tests -v
```
