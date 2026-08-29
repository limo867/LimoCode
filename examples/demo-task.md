# 端到端演示任务

在一个包含简单 Python 测试的工作区中运行：

> 阅读项目文件，找出测试失败的原因，修改实现，运行测试验证修复，并总结修改内容。

推荐观察内容：

1. Agent 先使用 `list_files` 和 `read_file` 了解项目。
2. Agent 使用 `write_file` 修改实现。
3. Agent 使用 `run_command` 执行测试。
4. Agent 根据命令结果继续修复或给出最终总结。

DemoModel 会在工作区创建 `.coding-agent-demo/result.txt` 作为演示产物；该目录可在演示后删除。

Web Demo：

```powershell
python web_server.py --demo --workspace .
```

然后访问 `http://127.0.0.1:8765/`。当前 API 通过任务请求体中的 `demo: true` 选择离线模型。
