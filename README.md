# Local Coding Agent

这是一个从零实现的本地编程智能体框架。目前已完成前六轮核心能力：OpenAI 兼容 Chat Completions 客户端、多轮 Tool Calling、本地文件和命令工具、工作区安全、命令安全、上下文限制与故障恢复。文件与命令工具始终在本机执行。

## 运行

需要 Python 3.10+：

```powershell
python main.py --demo "列出项目文件"
```

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

## 后续步骤

1. 增加应用服务层和统一事件模型，为 Web 与桌面 GUI 复用。
2. 实现本地 Web API、实时事件流和 Web 前端。
3. 增加桌面 GUI，并持续完善日志、文档和演示。

运行基础测试：

```powershell
python -m unittest discover -s tests -v
```
