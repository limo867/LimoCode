"""Context budgeting and structured compaction for long-running tool loops."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Any

from .config import Config


@dataclass(frozen=True)
class CompactionResult:
    messages: list[dict[str, Any]]
    compacted: bool
    before_tokens: int
    after_tokens: int
    summary: str = ""


class ContextManager:
    """Replaces old interaction history with a bounded, useful task summary."""

    def __init__(self, config: Config):
        self.max_tokens = max(128, config.max_context_tokens)
        self.threshold = min(0.95, max(0.1, config.compaction_threshold))
        self.summary_char_limit = min(max(200, config.max_history_chars), 12000, max(240, int(self.max_tokens * 2)))
        self.max_history_messages = max(1, config.max_history_messages)

    def estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        serialized = json.dumps(messages, ensure_ascii=False, default=str, separators=(",", ":"))
        return max(1, math.ceil(len(serialized) / 3.5) + len(messages) * 4)

    def compact(self, messages: list[dict[str, Any]], task: str, *, force: bool = False) -> CompactionResult:
        before = self.estimate_tokens(messages)
        history_limit_hit = len(messages) > 2 + self.max_history_messages
        if len(messages) <= 2 or (not force and before < self.max_tokens * self.threshold and not history_limit_hit):
            return CompactionResult(list(messages), False, before, before)
        head = messages[:2]
        history = messages[2:]
        keep_start = self._recent_start(history)
        omitted = history[:keep_start]
        recent = history[keep_start:]
        recent_limit = max(0, self.max_history_messages - 1)
        if len(recent) > recent_limit:
            recent = recent[-recent_limit:] if recent_limit else []
            if recent and recent[0].get("role") == "tool":
                recent = []
            omitted = history[: len(history) - len(recent)]
        if not omitted:
            omitted, recent = history, []
        summary = self._summary(task, omitted, recent)
        compacted = head + [{"role": "system", "content": summary}] + recent
        while len(recent) > 1 and self.estimate_tokens(compacted) > self.max_tokens * self.threshold:
            recent = recent[1:]
            compacted = head + [{"role": "system", "content": summary}] + recent
        if self.estimate_tokens(compacted) > self.max_tokens:
            summary = self._summary(task, omitted + recent, [], minimal=True)
            compacted = head + [{"role": "system", "content": summary}]
        return CompactionResult(compacted, True, before, self.estimate_tokens(compacted), summary)

    def _recent_start(self, history: list[dict[str, Any]]) -> int:
        budget = max(96, int(self.max_tokens * 0.28))
        used = 0
        start = len(history)
        for index in range(len(history) - 1, -1, -1):
            cost = self.estimate_tokens([history[index]])
            if start < len(history) and used + cost > budget:
                break
            start = index
            used += cost
        if start < len(history) and history[start].get("role") == "tool":
            while start > 0 and history[start].get("role") != "assistant":
                start -= 1
        return start

    def _summary(
        self,
        task: str,
        omitted: list[dict[str, Any]],
        recent: list[dict[str, Any]],
        *,
        minimal: bool = False,
    ) -> str:
        progress: list[str] = []
        issues: list[str] = []
        files: set[str] = set()
        decisions: list[str] = []
        for message in omitted:
            content = message.get("content")
            if message.get("role") == "assistant" and message.get("tool_calls"):
                progress.extend(f"requested {call.get('name', 'tool')}" for call in message["tool_calls"] if isinstance(call, dict))
            if message.get("role") == "tool" and isinstance(content, dict):
                progress.append("tool completed" if content.get("ok") else "tool failed")
                if not content.get("ok"):
                    issues.append(str(content.get("error", "tool failed"))[:240])
                if isinstance(content.get("path"), str):
                    files.add(content["path"])
            if isinstance(content, str):
                files.update(re.findall(r"\b[\w./-]+\.(?:py|md|txt|json|yaml|yml|js|ts|java)\b", content))
                if any(marker in content.lower() for marker in ("must", "never", "do not", "should", "必须", "不要", "统一")):
                    decisions.append(content[:240])
        sections = ["## Compacted Task Context", "## Task", task[:800]]
        if not minimal:
            sections += [
                "## Progress",
                "\n".join(f"- {entry}" for entry in self._unique(progress)[-10:]) or "- Earlier interactions compacted.",
                "## Current State",
                "Recent tool calls are preserved below; continue from their results.",
            ]
            if decisions:
                sections += ["## Important Decisions", "\n".join(f"- {entry}" for entry in self._unique(decisions)[-5:])]
            if issues:
                sections += ["## Known Issues", "\n".join(f"- {entry}" for entry in self._unique(issues)[-5:])]
            if files:
                sections += ["## Relevant Files", "\n".join(f"- {path}" for path in sorted(files)[:20])]
            sections += ["## Next Steps", "Use the preserved recent tool results, then verify the remaining task."]
        return "\n\n".join(sections)[: self.summary_char_limit]

    @staticmethod
    def _unique(items: list[str]) -> list[str]:
        return list(dict.fromkeys(item for item in items if item))
