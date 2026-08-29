# Local Coding Agent API

The local server binds to `127.0.0.1` by default. All task execution remains in the configured local workspace.

## Start

```powershell
python web_server.py --demo --workspace .
```

运行限制可直接覆盖环境变量默认值，例如：

```powershell
python web_server.py --workspace . --model-timeout 90 --timeout 45 --approval-timeout 180 --min-request-interval 250
```

`--min-request-interval 0` 会关闭额外的进程内模型请求间隔限制。

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Server health check. |
| `GET` | `/api/tasks?limit=N&offset=N` | Paginated task summaries (`limit` defaults to 50 and is capped at 100). |
| `POST` | `/api/tasks` | Create a task. |
| `POST` | `/api/tasks/{id}/approvals/{approval_id}` | Resolve a pending high-risk command approval. |
| `GET` | `/api/tasks/{id}` | Get a task summary. |
| `GET` | `/api/tasks/{id}/events?after=N` | SSE events after sequence `N`. |
| `GET` | `/api/tasks/{id}/event-log?after=N&limit=N` | Bounded JSON event history for inspection or recovery. |
| `DELETE` | `/api/tasks/{id}` | Request task cancellation. |

Create a demo task:

```json
{
  "task": "Inspect files and create a demo report",
  "demo": true
}
```

The SSE event payload includes `id`, `sequence`, `timestamp`, `type`, `task_id`, and event-specific `data`. Clients should persist the largest observed `sequence` and reconnect using `after` to avoid duplicate rendering.

Paged task and event-history responses include `next_offset` or `next_after`. A `null` value means the returned page is the final page. The event-history endpoint defaults to 100 entries and is capped at 500; it does not replace the real-time SSE endpoint.

## Limitations

Tasks and events are persisted to the local SQLite path configured by `AGENT_HISTORY_DB`, so completed history survives a server restart. Event-history pagination reads the persisted records, rather than only the bounded in-memory event window. Any task left queued or running during a restart is marked failed because its executing thread no longer exists. Cancellation is cooperative: the Agent checks between model and tool steps; command execution additionally terminates its child process when cancellation is observed.

High-risk commands are rejected unless the local operator has added an exact match to `AGENT_APPROVED_COMMANDS`. This is a local whitelist, not an operating-system sandbox.

Successful `write_file` results include bounded `previous_preview` and `content_preview` fields for UI diff displays.

High-risk commands not present in `AGENT_APPROVED_COMMANDS` produce a `command_approval_requested` event. A local UI can send `{"approved": true}` or `{"approved": false}` to the approval endpoint. A request can be resolved exactly once; cancellation or the configured `AGENT_COMMAND_APPROVAL_TIMEOUT` rejects execution.
