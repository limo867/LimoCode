# LimoCode：本地编程智能体


## 运行前准备

1. 使用 Python 3.10+，在项目目录执行 `python -m pip install -e .`。
2. 复制 `.env.example` 为 `.env`，填写 `LLM_API_KEY`；可按模型服务填写 `LLM_BASE_URL`、`LLM_MODEL`。

## 终端 TUI

在目标项目目录执行 `limocode`，当前目录即工作区；也可执行 `limocode --workspace D:\目标项目`。若安装后命令尚未生效，使用 `python -m coding_agent.tui --workspace .`。启动后直接输入编程任务，`/help` 可查看会话、模型、记忆和权限相关命令。

## Web

在本项目目录执行 `python web_server.py --host 127.0.0.1 --port 8900 --workspace .`，再访问 `http://127.0.0.1:8900`。Web 服务已直接提供前端页面，无需单独启动 Vite；可在页面中选择或输入本地工作区路径后新建会话。

## 项目说明

LimoCode 不使用 LangChain、AutoGen 等 Agent 框架。主 Agent 通过原生 Tool Calling 形成“理解任务—调用本地工具—回填结果—继续推理”的循环，可在受限工作区中读写文件、执行命令、生成 Diff 并完成编程任务。

## 特色功能

1. **本地安全控制**：模型没有直接操作系统的权限。路径被限制在受信任工作区内；文件修改先生成 ChangeSet 和 Diff；写入及高风险命令按模式请求用户批准。
2. **Subagent 协作**：Explorer 只读定位代码、依赖和测试入口；Implementer 在限定范围内实现修改；Verifier 只执行安全的编译、测试和运行命令。各角色使用独立上下文、工具权限和轮次预算，避免无限递归调度。
3. **Reviewer 抗幻觉审查**：主 Agent 生成候选结果后，Reviewer 会只读核对用户原始需求、当前工作区、主 Agent 操作摘要和 Verifier 报告，识别遗漏需求、实现错误、未验证或答非所问。Reviewer 返回 `REJECT` 时，系统将具体反馈交回主 Agent 修正并重新审查；仅通过后才作为成功结果交付。该机制用于降低模型幻觉和错误自信风险，但不替代人工代码审查。
4. **可追溯交互**：SQLite 持久化任务、会话和事件，支持恢复会话、上下文压缩和项目记忆；TUI/Web 实时展示流式回复、工具调用、审批、代码变更和 Subagent 报告。
