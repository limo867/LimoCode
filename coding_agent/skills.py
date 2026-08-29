"""Markdown-backed, on-demand workflow instructions for the coding agent."""

from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    instructions: str
    path: Path


class SkillManager:
    """Discovers metadata and loads full Skill instructions only when selected."""

    _ALIASES = {
        "debugging": {"debug", "bug", "error", "fail", "crash", "segfault", "fix", "定位", "报错", "修复"},
        "testing": {"test", "testing", "coverage", "pytest", "unittest", "测试"},
        "git": {"git", "commit", "branch", "merge", "rebase", "提交", "分支"},
        "code-review": {"review", "audit", "security", "审查", "评审"},
        "coding": {"implement", "feature", "refactor", "code", "实现", "开发", "重构"},
    }

    def __init__(self, roots: list[Path]):
        self.roots = roots
        self._skills: dict[str, Skill] = {}
        self.reload()

    @classmethod
    def default_roots(cls, workspace: Path) -> list[Path]:
        package_root = Path(__file__).resolve().parent / "default_skills"
        try:
            user_root = Path.home() / ".local-codex" / "skills"
        except RuntimeError:
            user_root = Path(".local-codex") / "skills"
        # Later roots override earlier ones, so project-specific workflows win.
        return [package_root, user_root, workspace / "skills"]

    def reload(self) -> None:
        discovered: dict[str, Skill] = {}
        for root in self.roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("SKILL.md")):
                try:
                    skill = self._parse(path)
                except (OSError, ValueError):
                    continue
                discovered[skill.name] = skill
        self._skills = discovered

    def metadata(self) -> list[Skill]:
        return sorted(self._skills.values(), key=lambda skill: skill.name)

    def load(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError as exc:
            raise ValueError(f"skill does not exist: {name}") from exc

    def select(self, task: str, manual: tuple[str, ...] = ()) -> list[Skill]:
        if manual:
            return [self.load(name) for name in manual]
        normalized_task = task.lower()
        words = set(re.findall(r"[a-z0-9_-]+|[\u4e00-\u9fff]+", normalized_task))
        selected: list[Skill] = []
        for skill in self.metadata():
            terms = set(re.findall(r"[a-z0-9_-]+|[\u4e00-\u9fff]+", f"{skill.name} {skill.description}".lower()))
            terms.update(self._ALIASES.get(skill.name, set()))
            if words & terms or any(any("\u4e00" <= char <= "\u9fff" for char in term) and term in normalized_task for term in terms):
                selected.append(skill)
        return selected

    @staticmethod
    def _parse(path: Path) -> Skill:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError("skill is empty")
        metadata: dict[str, str] = {}
        body = text
        if text.startswith("---\n"):
            closing = text.find("\n---", 4)
            if closing < 0:
                raise ValueError("skill front matter is not closed")
            for line in text[4:closing].splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    metadata[key.strip().lower()] = value.strip()
            body = text[closing + 4 :].strip()
        name = metadata.get("name", path.parent.name).strip()
        description = metadata.get("description", "").strip()
        if not name or not description or not body:
            raise ValueError("skill needs name, description, and instructions")
        return Skill(name, description, body, path)
