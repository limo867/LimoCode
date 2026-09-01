import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSanitize from "rehype-sanitize";
import { highlightCode } from "./shiki.js";
import "./styles.css";

const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const ACTIVE = new Set(["queued", "running"]);
const statusLabels = { queued: "排队中", running: "执行中", completed: "已完成", failed: "失败", cancelled: "已取消" };

async function api(path, options = {}) {
  const response = await fetch(path, {
    method: options.method || "GET",
    headers: options.body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined
  });
  const raw = await response.text();
  let payload = null;
  try { payload = raw ? JSON.parse(raw) : null; } catch { payload = { error: raw }; }
  if (!response.ok) throw new Error(payload?.error || `请求失败（${response.status}）`);
  return payload;
}

function Markdown({ content }) {
  return <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]} skipHtml components={{ code: CodeBlock }}>{content || ""}</ReactMarkdown></div>;
}

function CodeBlock({ inline, className, children, ...props }) {
  const language = (className || "").replace("language-", "").toLowerCase();
  const source = String(children || "").replace(/\n$/, "");
  const [html, setHtml] = useState("");
  useEffect(() => { let cancelled = false; if (!inline) highlightCode(source, language).then(value => { if (!cancelled) setHtml(value); }); return () => { cancelled = true; }; }, [source, language, inline]);
  if (inline) return <code className={className} {...props}>{children}</code>;
  return <pre className="code-block"><code dangerouslySetInnerHTML={html ? { __html: html } : undefined} {...props}>{html ? null : source}</code></pre>;
}

function DiffBlock({ diff, file, onOpen }) {
  const lines = String(diff || "").split("\n");
  return <button className="diff-summary" type="button" onClick={() => onOpen({ diff, file })}>
    <span className="diff-summary-mark">±</span><span className="diff-summary-file">{file || "文件变更"}</span>
    <span className="diff-summary-hint">查看差异</span>
    <span className="diff-preview" aria-hidden="true">{lines.filter(Boolean).slice(0, 4).map((line, index) => <span key={index} className={line.startsWith("+") && !line.startsWith("+++") ? "added" : line.startsWith("-") && !line.startsWith("---") ? "removed" : "context"}>{line}</span>)}</span>
  </button>;
}

function DiffDrawer({ diff, onClose }) {
  if (!diff) return null;
  const lines = String(diff.diff || "").split("\n");
  return <div className="drawer-layer" role="dialog" aria-modal="true" aria-label="文件差异">
    <button className="drawer-scrim" type="button" onClick={onClose} aria-label="关闭差异面板" />
    <aside className="diff-drawer"><header><div><div className="eyebrow">文件变更</div><h2>{diff.file || "工作区文件"}</h2></div><button className="icon-button" type="button" onClick={onClose} aria-label="关闭">×</button></header>
      <div className="diff-toolbar"><span>统一 Diff</span><span className="legend"><i className="legend-add">+</i>新增 <i className="legend-remove">−</i>删除</span></div>
      <div className="unified-diff">{lines.map((line, index) => { const header = /^(diff |index |@@|---|\+\+\+)/.test(line); const kind = header ? "header" : line.startsWith("+") ? "added" : line.startsWith("-") ? "removed" : "context"; const prefix = kind === "added" ? "+" : kind === "removed" ? "−" : " "; return <div className={`unified-line ${kind}`} key={index}><span className="line-prefix">{header ? " " : prefix}</span><span>{header || kind === "context" ? line : line.slice(1)}</span></div>; })}</div>
    </aside>
  </div>;
}

function App() {
  const [runtime, setRuntime] = useState(null); const [conversations, setConversations] = useState([]); const [conversation, setConversation] = useState(null); const [input, setInput] = useState(""); const [sending, setSending] = useState(false); const [drawer, setDrawer] = useState(null); const [error, setError] = useState(""); const sourceRef = useRef(null);
  const workspace = runtime?.workspace || conversation?.workspace || "";
  const activeTask = useMemo(() => (conversation?.tasks || []).find(task => ACTIVE.has(task.status)), [conversation]);

  const refresh = async () => { try { const [status, history] = await Promise.all([api("/api/status"), api("/api/conversations?limit=100")]); setRuntime(status); setConversations(history.conversations || []); } catch (err) { setError(err.message); } };
  useEffect(() => { refresh(); return () => sourceRef.current?.close(); }, []);
  const openConversation = async id => { try { const value = await api(`/api/conversations/${encodeURIComponent(id)}?workspace=${encodeURIComponent(workspace)}`); setConversation(value); setError(""); } catch (err) { setError(err.message); } };
  const submit = async event => { event.preventDefault(); const taskText = input.trim(); if (!taskText || sending || activeTask) return; setSending(true); setError(""); try { const task = await api(conversation ? `/api/conversations/${encodeURIComponent(conversation.id)}/tasks` : "/api/tasks", { method: "POST", body: { task: taskText, workspace } }); setInput(""); const next = conversation ? { ...conversation, tasks: [...(conversation.tasks || []), task] } : await api(`/api/conversations/${encodeURIComponent(task.conversation_id)}`); setConversation(next); subscribe(task.id); refresh(); } catch (err) { setError(err.message); } finally { setSending(false); } };
  const subscribe = taskId => { sourceRef.current?.close(); const source = new EventSource(`/api/tasks/${encodeURIComponent(taskId)}/events?after=0`); sourceRef.current = source; source.onmessage = event => { const value = JSON.parse(event.data); setConversation(current => { if (!current) return current; const tasks = (current.tasks || []).map(task => task.id === taskId ? { ...task, status: value.type === "task_finished" ? "completed" : value.type === "task_error" ? "failed" : value.type === "task_cancelled" ? "cancelled" : task.status, result: value.data?.result || task.result, error: value.data?.error || task.error, events: [...(task.events || []), value] } : task); return { ...current, tasks }; }); if (TERMINAL.has(value.data?.status) || ["task_finished", "task_error", "task_cancelled"].includes(value.type)) { source.close(); refresh(); } }; };
  const mode = runtime?.permissions?.mode || "approval";
  const changeFiles = (conversation?.tasks || []).flatMap(task => (task.events || []).filter(event => event.type === "tool_finished" && event.data?.result?.unified_diff).flatMap(event => [{ file: event.data.result.path, diff: event.data.result.unified_diff, taskId: task.id }]));
  return <div className="app"><aside className="sidebar"><div className="brand"><span className="brand-mark">&gt;_</span><strong>LimoCode</strong><span>网页</span></div><div className="workspace"><div className="eyebrow">工作区</div><strong>{workspace ? workspace.split(/[\\/]/).filter(Boolean).pop() : "正在加载"}</strong><code>{workspace || "未选择工作区"}</code><span className={`trust ${runtime?.trusted ? "ok" : ""}`}>{runtime?.trusted ? "已信任" : "未信任"}</span></div><div className="section-title">会话 <button className="icon-button" onClick={refresh} aria-label="刷新">↻</button></div><div className="conversation-list">{conversations.map(item => <button key={item.id} className={`conversation ${conversation?.id === item.id ? "selected" : ""}`} onClick={() => openConversation(item.id)}><span className="status-dot" /><span><strong>{item.root_task?.task || "新会话"}</strong><small>{item.task_count || 0} 轮</small></span></button>)}</div></aside><main className="main"><header className="topbar"><div><div className="eyebrow">当前会话</div><h1>{conversation?.root_task?.task || "新建会话"}</h1></div><div className="top-actions"><select value={mode} onChange={async event => { try { const result = await api("/api/permissions", { method: "POST", body: { workspace, mode: event.target.value } }); setRuntime(current => ({ ...current, permissions: result })); } catch (err) { setError(err.message); } }}><option value="approval">确认后修改</option><option value="auto">自动修改</option></select><span className="status-badge">{activeTask ? "执行中" : "就绪"}</span></div></header><section className="transcript">{!conversation?.tasks?.length && <div className="empty"><h2>今天想完成什么？</h2><p>描述一个编程任务，Agent 会在当前工作区中检查代码、修改文件并运行验证。</p></div>}{conversation?.tasks?.map(task => <article className="task" key={task.id}><div className="task-user"><span className="avatar">你</span><div><div className="message-label">你的任务</div><div className="user-text">{task.task}</div></div></div><div className="task-result"><span className="avatar agent">&gt;_</span><div className="result-body"><div className="message-label">LimoCode · {statusLabels[task.status] || "处理中"}</div>{task.result ? <Markdown content={task.result} /> : task.error ? <div className="error-box">{task.error}</div> : <div className="thinking">正在处理<span>.</span><span>.</span><span>.</span></div>}{changeFiles.filter(item => item.taskId === task.id).map((item, index) => <DiffBlock key={`${item.file}-${index}`} diff={item.diff} file={item.file} onOpen={setDrawer} />)}</div></div><div className="task-divider" /></article>)}</section><form className="composer" onSubmit={submit}><textarea value={input} onChange={event => setInput(event.target.value)} placeholder="描述一个编程任务，或输入 / 选择命令" rows={2} disabled={Boolean(activeTask)} /><button type="submit" disabled={!input.trim() || sending || Boolean(activeTask)}>发送</button>{error && <div className="form-error">{error}</div>}</form></main>{drawer && <DiffDrawer diff={drawer} onClose={() => setDrawer(null)} />}</div>;
}

createRoot(document.getElementById("root")).render(<App />);
