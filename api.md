# Local Coding Agent API

The local server binds to `127.0.0.1` by default. Its startup workspace is the
default for compatibility, while the Web workbench can select an existing local
folder before creating each new conversation. Task execution, file browsing,
history, memory, skills, and command working directories remain local to the
selected workspace.

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
| `GET` | `/api/tasks?workspace=...&limit=N&offset=N` | Paginated task summaries for one workspace (`limit` defaults to 50 and is capped at 100). |
| `POST` | `/api/tasks` | Create a task. |
| `POST` | `/api/tasks/{id}/approvals/{approval_id}` | Resolve a pending high-risk command approval. |
| `GET` | `/api/permissions?workspace=...` | Read the file-change mode for a workspace. |
| `POST` | `/api/permissions` | Set `{ "mode":"approval"|"auto", "workspace":"..." }` while no task is active. |
| `GET` | `/api/changesets?workspace=...&task_id=...` | List persisted Agent file changes and bounded diffs. |
| `GET` | `/api/changesets/{id}?workspace=...` | Inspect one ChangeSet. |
| `POST` | `/api/tasks/{id}/change-approvals/{approval_id}` | Approve or reject a proposed file ChangeSet. |
| `POST` | `/api/changesets/{id}/undo` | Undo one applied ChangeSet, or return `409` on a user-edit conflict. |
| `GET` | `/api/tasks/{id}` | Get a task summary. |
| `GET` | `/api/tasks/{id}/events?after=N` | SSE events after sequence `N`. |
| `GET` | `/api/tasks/{id}/event-log?after=N&limit=N` | Bounded JSON event history for inspection or recovery. |
| `DELETE` | `/api/tasks/{id}` | Request task cancellation. |

Create a demo task:

```json
{
  "task": "Inspect files and create a demo report",
  "demo": true,
  "workspace": "D:\\projects\\demo"
}
```

The browser should always include `workspace` when it creates a task. Omitting
it remains a backward-compatible API mode and uses the server startup
workspace. An explicit workspace must be trusted first; otherwise task creation
returns `403`.

The SSE event payload includes `id`, `sequence`, `timestamp`, `type`, `task_id`, and event-specific `data`. Clients should persist the largest observed `sequence` and reconnect using `after` to avoid duplicate rendering.

Paged task and event-history responses include `next_offset` or `next_after`. A `null` value means the returned page is the final page. The event-history endpoint defaults to 100 entries and is capped at 500; it does not replace the real-time SSE endpoint.

## Limitations

Tasks and events are persisted to local SQLite, so completed history survives a server restart. For a newly Web-selected project, generated task history and durable memory use that project's `.coding-agent/` directory; another selected project receives separate state. Event-history pagination reads persisted records, rather than only the bounded in-memory event window. Any task left queued or running during a restart is marked failed because its executing thread no longer exists. Cancellation is cooperative: the Agent checks between model and tool steps; command execution additionally terminates its child process when cancellation is observed.

File writes use `AGENT_PERMISSION_MODE=approval` by default. In this mode a
`write_file` call produces a persisted ChangeSet and a
`changeset_approval_requested` event; it does not mutate the workspace until
the local UI approves it. `auto` applies ordinary ChangeSets immediately, but
still saves their diff and reversible pre-image. A later Undo compares the
current file hash with the Agent-written hash; a mismatch returns a conflict
instead of overwriting a user edit.

High-risk commands are rejected unless the local operator has added an exact match to `AGENT_APPROVED_COMMANDS`. In Approval mode, shell commands outside a narrow read-only allowlist also request local approval. This is a local whitelist, not an operating-system sandbox.

ChangeSet events and API responses include bounded unified diffs for UI review.

High-risk commands not present in `AGENT_APPROVED_COMMANDS` produce a `command_approval_requested` event. A local UI can send `{"approved": true}` or `{"approved": false}` to the approval endpoint. A request can be resolved exactly once; cancellation or the configured `AGENT_COMMAND_APPROVAL_TIMEOUT` rejects execution.

## Workspace Selection and State

The Web workbench uses this sequence before a new conversation:

1. List known folders with `GET /api/workspaces`, or inspect a manually entered existing directory with `POST /api/workspaces/inspect`.
2. If the inspection reports `"trusted": false`, record local consent with `POST /api/workspaces/trust`.
3. Create the task with the canonical `workspace` returned by inspection or trust.
4. Read the selected folder's lazy file tree with `GET /api/workspaces/tree`.

Paths must name existing local directories. Relative paths resolve from the
server startup workspace. The inspect endpoint does not create an Agent service,
SQLite file, or `.coding-agent` directory, so selecting a folder remains
read-only until a task actually starts.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/workspaces` | List the startup workspace, currently open workspaces, and paths in the shared user-level trust store. Returns `default_workspace` and `workspaces`; each item has `path`, `name`, `trusted`, and `available`. A workspace trusted only in its local fallback store can still be entered and inspected by path. For compatibility, adding `?workspace=...` returns that workspace's runtime status instead of the list. |
| `POST` | `/api/workspaces/inspect` | Validate and canonicalize `{"workspace":"D:\\projects\\demo"}` without creating project state. Returns runtime and trust metadata. |
| `POST` | `/api/workspaces/trust` | Record consent for `{"workspace":"D:\\projects\\demo","trusted":true}`. Returns the inspected runtime payload. |
| `GET` | `/api/workspaces/tree?workspace=...&path=src&limit=200` | Return one trusted workspace directory level. `path` is relative, `limit` is `1..500`, and the payload has `entries` and `truncated`. |
| `GET` | `/api/conversations/{conversation_id}/files?workspace=...&path=src&limit=200` | Return a trusted tree for the workspace that owns that conversation. Supplying `workspace` lets a restored browser session reopen an external project after a server restart. |

The tree endpoint rejects absolute paths and paths outside the workspace, does
not follow symbolic links, and omits implementation/cache directories such as
`.git`, `.coding-agent`, `.venv`, `node_modules`, and `__pycache__`. It is a
directory listing API, not a file-content API.

`GET /api/workspace` (also available as `GET /api/status`) returns runtime
metadata for the default workspace, or for `?workspace=...` when supplied. The
response includes the canonical workspace path, trust state, demo mode, model
metadata, and non-secret runtime limits; it never includes `LLM_API_KEY`.

`POST /api/workspace/trust` is retained for older clients and trusts only the
server startup workspace. New Web clients should use `/api/workspaces/trust`.
Trust records are shared with the TUI and desktop GUI by canonical absolute
path, with the workspace-local fallback store used when the user-level store is
not writable.

Each selected workspace receives an isolated `AgentService`: code tools run
from that directory, and conversation history, durable memory, project skills,
and runtime selections do not merge with another folder. The server reads model
credentials and limits only when it starts; choosing a project intentionally
does not load that project's `.env` file.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/models?workspace=...` | Current model and available model metadata for a workspace. |
| `POST` | `/api/models/select` | Select a configured model with `{"model":"name","workspace":"..."}`. Returns `409` while a task is queued or running in that workspace. |
| `GET` | `/api/skills?workspace=...` | Skill metadata, current selection, and auto/manual mode for a workspace. |
| `POST` | `/api/skills/select` | Set manual skills with `{"skills":["debugging","testing"],"workspace":"..."}` or restore auto selection with `{"mode":"auto","workspace":"..."}`. |
| `POST` | `/api/skills/reload` | Rediscover `SKILL.md` files for `{"workspace":"..."}` and retain only still-valid selections. |
| `GET` | `/api/memory?workspace=...&query=...&limit=N` | List durable project memories, or search when `query` is given. |
| `POST` | `/api/memory` | Save manual durable memory with `{"content":"...","workspace":"..."}`. |
| `GET` | `/api/memory/status?workspace=...&task_id=...` | Storage status and optional task short-term-context status. A supplied task is resolved from its owning workspace. |
| `DELETE` | `/api/memory/{id}?workspace=...` | Delete one durable memory. |

## Conversations

A conversation is a user-visible chain of task turns. Individual task records
and their SSE streams remain available under `/api/tasks`; the conversation
endpoints group those records for history and continuation.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/conversations?workspace=...&limit=N&offset=N&resumable=true` | Paginated conversation summaries for one workspace. `resumable` is optional. |
| `GET` | `/api/conversations/{conversation_id}?workspace=...` | One conversation including ordered task records and its owning `workspace`. Supplying its trusted workspace restores an external project's conversation after a server restart. |
| `POST` | `/api/conversations/{conversation_id}/tasks?workspace=...` | Submit a follow-up to the latest resumable turn. Include `{"task":"...","workspace":"..."}` in Web requests. A different query/body workspace or a different conversation workspace returns `409`. |
| `POST` | `/api/conversations/{conversation_id}/compact?workspace=...` | Compact the latest persisted context for that conversation. Returns `409` if it has no resumable context or is still running. |

For compatibility, `POST /api/tasks` also accepts an optional
`"resume_from": "task-id"`. It creates a continuation in that task's existing
conversation. When `workspace` is also supplied, it must identify the same
workspace as the source task. This parameter is optional; omitting both it and
`workspace` retains legacy default-workspace behavior.
