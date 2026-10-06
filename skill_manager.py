# -*- coding: utf-8 -*-
"""
skill_manager.py —— Skill（人格）系统

一个 ``.md`` 文件 = 一个人格。文件结构：

    ---
    name: 傲娇女友
    description: 嘴上不饶人、其实很在意你的青梅竹马
    voice: longxiaochun_v2      # 可选：该人格专属音色，留空则用全局配置
    temperature: 0.95           # 可选：该人格专属温度
    ---
    你是用户的青梅竹马……

    ## 行为准则
    - 说话要短，一次不超过三句
    - ……

分隔线以上的 ``---`` 区块是 frontmatter（元信息），以下全部是 System Prompt。
没有 frontmatter 也能用，此时文件名即人格名，全文即 System Prompt。

不需要 PyYAML：frontmatter 只支持 ``key: value`` 这种平坦结构，
自己解析 20 行搞定，还能避免多一个依赖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
DEFAULT_SKILLS_DIR = APP_DIR / "skills"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


@dataclass
class Skill:
    """一个人格。

    ``voice`` / ``speech_rate`` / ``pitch_rate`` 是该人格的**声线定制**，
    留空(None)则沿用 config 里的全局值。活泼的人格可以把语速调快一点，
    沉稳的调慢一点 —— 否则所有人物听起来都是同一种说话节奏。
    """

    name: str
    system_prompt: str
    description: str = ""
    voice: str = ""
    temperature: float | None = None
    speech_rate: float | None = None
    pitch_rate: float | None = None
    path: Path | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # 便于直接塞进 CTkOptionMenu
        return self.name


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """返回 (元信息字典, 正文)。"""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = _strip_quotes(value)
    return meta, text[match.end():]


class SkillManager:
    """扫描 ``skills/`` 目录，提供人格列表与切换。"""

    def __init__(self, skills_dir: Path | str | None = None) -> None:
        self.skills_dir = Path(skills_dir) if skills_dir else DEFAULT_SKILLS_DIR
        self._skills: list[Skill] = []
        self._current: Skill | None = None

    # ------------------------------------------------------------------ #
    @classmethod
    def create(cls, skills_dir: Path | str | None = None) -> "SkillManager":
        """构造并立即加载。**推荐用这个而不是直接 new**。

        因为 ``load_all()`` 返回的是 Skill 列表（一个查询结果），
        直接写 ``skills = SkillManager().load_all()`` 会拿到 list 而不是 manager ——
        后续调 ``skills.current()`` 就会炸。这里把"构造 + 加载"绑在一起，避免踩坑。
        """
        manager = cls(skills_dir)
        manager.load_all()
        return manager

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #
    def load_all(self) -> list[Skill]:
        """重新扫描目录。目录不存在会自动创建并写入内置示例人格。"""
        if not self.skills_dir.exists():
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            self._write_builtin_defaults()

        skills: list[Skill] = []
        for path in sorted(self.skills_dir.glob("*.md")):
            skill = self._load_file(path)
            if skill is not None:
                skills.append(skill)

        # 目录被清空时兜底，保证界面上永远有东西可选
        if not skills:
            skills = [self._fallback_skill()]

        self._skills = skills
        if self._current is None or self._current.name not in {s.name for s in skills}:
            self._current = skills[0]
        return skills

    def _load_file(self, path: Path) -> Skill | None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"[skill] 读取失败 {path.name}: {exc}")
            return None

        meta, body = _parse_frontmatter(text)
        body = body.strip()
        if not body:
            return None

        def _float(key: str) -> float | None:
            if key not in meta:
                return None
            try:
                return float(meta[key])
            except ValueError:
                print(f"[skill] {path.name} 的 {key}={meta[key]!r} 不是数字，已忽略")
                return None

        return Skill(
            name=meta.get("name") or path.stem,
            description=meta.get("description", ""),
            system_prompt=body,
            voice=meta.get("voice", ""),
            temperature=_float("temperature"),
            speech_rate=_float("speech_rate"),
            pitch_rate=_float("pitch_rate"),
            path=path,
            meta=meta,
        )

    # ------------------------------------------------------------------ #
    # 访问
    # ------------------------------------------------------------------ #
    def __iter__(self):
        return iter(self._skills)

    def __len__(self) -> int:
        return len(self._skills)

    @property
    def skills(self) -> list[Skill]:
        return list(self._skills)

    @property
    def names(self) -> list[str]:
        return [s.name for s in self._skills]

    def current(self) -> Skill:
        if self._current is None:
            self.load_all()
        assert self._current is not None
        return self._current

    def get(self, name: str) -> Skill | None:
        for skill in self._skills:
            if skill.name == name:
                return skill
        return None

    def select(self, name: str) -> Skill:
        """切换人格；找不到就回退到第一个，绝不抛异常。"""
        skill = self.get(name)
        if skill is None:
            skill = self._skills[0] if self._skills else self._fallback_skill()
        self._current = skill
        return skill

    # ------------------------------------------------------------------ #
    # 首次运行的示例人格
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fallback_skill() -> Skill:
        return Skill(
            name="通用助手",
            description="skills/ 目录为空时的兜底人格",
            system_prompt=(
                "你是一个友好、简洁、口语化的中文 AI 助手。"
                "回答要短，像在微信上聊天一样自然，不要使用 Markdown 标题和长列表。"
            ),
        )

    def _write_builtin_defaults(self) -> None:
        samples = {
            "傲娇女友.md": (
                "---\n"
                "name: 傲娇女友\n"
                "description: 嘴硬心软的青梅竹马\n"
                "temperature: 0.95\n"
                "---\n"
                "你是用户的青梅竹马，住在隔壁从小一起长大。\n\n"
                "## 性格\n"
                "- 典型傲娇：嘴上嫌弃，行动上关心。绝不直白地说\"我喜欢你\"，\n"
                "  而是用\"哼，才不是特意为你做的\"这类方式表达。\n"
                "- 对方夸你时会慌乱地转移话题。\n\n"
                "## 说话方式\n"
                "- **口语化、短句**。每次回复 1~3 句，像微信聊天，不要长篇大论。\n"
                "- 常用语气词：哼、才不是、笨蛋、真是的、……算了。\n"
                "- 绝对不要输出 Markdown 标题、编号列表、代码块。\n"
                "- 不要用旁白描述动作（如 *脸红*），只说话。\n"
            ),
            "专业导师.md": (
                "---\n"
                "name: 专业导师\n"
                "description: 结构化、严谨的技术导师\n"
                "temperature: 0.4\n"
                "---\n"
                "你是一位资深的软件架构师，担任用户的技术导师。\n\n"
                "## 风格\n"
                "- 先给结论，再给理由，最后给可执行的下一步。\n"
                "- 严谨、克制，不确定的地方明确说\"不确定\"并给出验证方法。\n"
                "- 涉及代码时给出最小可运行片段，并指出常见坑。\n"
                "- 中文回答，术语保留英文原词。\n\n"
                "## 边界\n"
                "- 不做无根据的猜测；不要为了显得有帮助而编造 API 名称。\n"
            ),
        }
        for filename, content in samples.items():
            target = self.skills_dir / filename
            if not target.exists():
                try:
                    target.write_text(content, encoding="utf-8")
                except OSError:
                    pass


__all__ = ["Skill", "SkillManager", "DEFAULT_SKILLS_DIR"]
