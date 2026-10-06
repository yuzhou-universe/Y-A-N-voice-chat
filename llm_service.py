# -*- coding: utf-8 -*-
"""
llm_service.py —— 通义千问（Qwen）流式调用 + 上下文管理 + 标点智能截断

本模块是三件事的集合：

  1. ``SentenceBuffer``  —— 把 LLM 吐出的 token 增量，拼成"适合朗读的整句"。
     这是"全链路流式"的核心：GUI 要的是**每个 token 立刻显示**，
     TTS 要的是**语义完整的句子**（把"你好，今天天"喂给 TTS 会读出奇怪的重音）。
     两者节奏不同，所以必须有一个缓冲层做拆包。

  2. ``clean_for_tts``   —— 把 Markdown 洗成纯文本，并丢掉代码块 / URL。
     没人想听模型把 ``` 和 https://... 念出来。

  3. ``LLMService``      —— AsyncOpenAI 客户端 + 对话历史管理。
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator

try:  # 延迟到实例化时才需要，方便没装 openai 时跑 --check 自检
    from openai import AsyncOpenAI

    _HAS_OPENAI = True
except ImportError:  # pragma: no cover
    AsyncOpenAI = None  # type: ignore[assignment,misc]
    _HAS_OPENAI = False

try:
    import markdown as _markdown

    _HAS_MARKDOWN = True
except ImportError:  # pragma: no cover
    _HAS_MARKDOWN = False

if TYPE_CHECKING:
    from config_manager import ConfigManager
    from skill_manager import Skill


# =========================================================================== #
# 一、文本清洗
# =========================================================================== #
_FENCED_RE = re.compile(r"```.*?```", re.DOTALL)          # 整段围栏代码块
_STRAY_FENCE_RE = re.compile(r"^\s{0,3}`{3,}.*$", re.MULTILINE)  # 落单的 ``` 行
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")      # [文字](链接) / ![alt](src)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t　]+")
_MULTI_NL_RE = re.compile(r"\n{2,}")


def clean_for_tts(text: str) -> str:
    """把一段可能是 Markdown 的文本，洗成适合语音朗读的纯文本。

    处理顺序很关键：
      先去围栏代码块 -> 再去 markdown 标记 -> 再转 HTML 剥标签 -> 最后去 URL。
    反过来的话，代码块里的 ``#include`` 会被 markdown 解析器当成标题。
    """
    if not text:
        return ""

    t = _FENCED_RE.sub(" ", text)
    t = _STRAY_FENCE_RE.sub(" ", t)
    t = _MD_LINK_RE.sub(r"\1", t)      # 保留链接文字，丢掉 URL

    if _HAS_MARKDOWN:
        # 借助 markdown 库把 **粗体** / *斜体* / `代码` / # 标题 等统一转成 HTML，
        # 然后一次性剥掉标签 —— 比手写一堆正则可靠得多。
        try:
            t = _markdown.markdown(t)
        except Exception:  # noqa: BLE001 - 库出问题时不能让整条流水线挂掉
            pass
        t = _HTML_TAG_RE.sub("", t)
        t = _html.unescape(t)
    else:
        t = re.sub(r"[*_`#>~|]", "", t)

    t = _URL_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t)
    t = _MULTI_NL_RE.sub("\n", t)
    return t.strip()


# =========================================================================== #
# 二、标点智能截断
# =========================================================================== #
@dataclass
class Chunk:
    """一个待朗读的片段。"""

    text: str
    speak: bool = True   # False 表示"这段别念"（例如代码块内容）


class SentenceBuffer:
    """流式 token -> 朗读片段 的缓冲拆包器。

    切分规则（按优先级）：

    1. **强标点立即切**：``。！？!?；;…`` 以及换行。
       这是最自然的语音停顿点，切在这里 TTS 的韵律最好。

    2. **弱标点够长才切**：``，,、：:`` 只有在缓冲区已超过 ``soft_max`` 时才切。
       否则模型说"嗯，那个，我觉得"会被切成三段，语音听起来一顿一顿的。

    3. **硬上限兜底**：达到 ``hard_max`` 无条件切。
       模型列清单、说英文、或者写代码时可能长时间不打标点；
       没有这条兜底，TTS 会一直不出声，用户会以为程序卡死了。

    4. **首句优先**：第一句的门槛用 ``first_min`` 而不是 ``soft_max``。
       首包延迟（首个字到第一声语音的时间）是语音交互体感的关键，
       宁可在第一句切得碎一点，也要让声音尽快响起来。

    5. **代码块标记为不朗读**：用三反引号状态机跟踪，块内文本 ``speak=False``。

    6. **收尾符号回填**：切分点在 ``。`` 上，但后面紧跟着 ``"`` 或 ``）``
       （如 ``他说："好的。"``），这两个符号应该补到上一片段，而不是
       变成下一片段开头的孤儿字符。
    """

    #: 强标点 —— 出现即切
    STRONG = frozenset("。！？!?；;…\n")
    #: 弱标点 —— 缓冲区够长时才切
    WEAK = frozenset("，,、：:")
    #: 收尾符号 —— 若上一片段刚被切走，则回填到上一片段
    TRAILING = frozenset("”’」』）)】］》\"'…")

    def __init__(self, hard_max: int = 80, soft_max: int = 32, first_min: int = 6) -> None:
        self.hard_max = hard_max
        self.soft_max = soft_max
        self.first_min = first_min
        self._buf: list[str] = []
        self._first = True      # 是否还没吐出过任何片段
        self._in_code = False   # 是否处于 ``` 代码块内部
        self._fence_run = 0     # 连续反引号计数

    # ------------------------------------------------------------------ #
    def feed(self, delta: str) -> list[Chunk]:
        """喂入一段增量文本，返回本次能切出的所有片段（可能为空列表）。"""
        out: list[Chunk] = []

        for ch in delta:
            # ---- 代码围栏状态机：连续三个反引号翻转 in_code ----
            if ch == "`":
                self._fence_run += 1
                if self._fence_run >= 3:
                    self._in_code = not self._in_code
                    self._fence_run = 0
            else:
                self._fence_run = 0

            # ---- 收尾符号回填 ----
            if not self._buf and ch in self.TRAILING and out:
                out[-1].text += ch
                continue

            self._buf.append(ch)
            if self._should_cut():
                chunk = self._take()
                if chunk.text:      # 纯标点/空白被清洗掉后会是空串，丢掉
                    out.append(chunk)

        return out

    def flush(self) -> list[Chunk]:
        """流结束时调用，把缓冲区里剩下的半句也吐出来（不能丢字）。"""
        if not self._buf:
            return []
        chunk = self._take()
        return [chunk] if chunk.text else []

    def reset(self) -> None:
        self._buf.clear()
        self._first = True
        self._in_code = False
        self._fence_run = 0

    # ------------------------------------------------------------------ #
    def _should_cut(self) -> bool:
        last = self._buf[-1]
        size = len(self._buf)

        # 规则 1：强标点，立即切
        if last in self.STRONG:
            return True

        # 规则 3：硬上限兜底
        if size >= self.hard_max:
            return True

        # 规则 2 / 4：弱标点，首句用更低门槛
        threshold = self.first_min if self._first else self.soft_max
        if last in self.WEAK and size >= threshold:
            return True

        return False

    def _take(self) -> Chunk:
        """取出缓冲区内容，产出一个片段。"""
        raw = "".join(self._buf)
        self._buf.clear()
        speak = not self._in_code
        self._first = False
        return Chunk(text=clean_for_tts(raw), speak=speak)


# =========================================================================== #
# 三、LLM 服务
# =========================================================================== #
class LLMService:
    """封装 Qwen 的流式对话 + 历史管理。

    历史提交采用「**占位 - 回填**」策略，这是为了支持"边生成边被打断"：

        begin_turn("你好")  ->  history = [u1, a1(空占位)]
        流式生成中…… 用户又发了一条 -> 新回合 begin_turn("在吗")
                              history = [u1, a1(空), u2, a2(空)]
        旧回合收尾 end_turn()  ->  把 a1 的内容填回去

    因为槽位下标在 begin_turn 时就固定了，后到的回合即使先完成，
    也不会把旧回合的回复插到错误的位置。顺序永远是 u1,a1,u2,a2。
    """

    def __init__(self, config: "ConfigManager") -> None:
        self._cfg = config
        self.history: list[dict[str, str]] = []
        self._client: Any = None
        self._client_key: str = ""

    # ------------------------------------------------------------------ #
    # 客户端
    # ------------------------------------------------------------------ #
    @property
    def api_key(self) -> str:
        return str(self._cfg.get("qwen_api_key", "") or "").strip()

    def _ensure_client(self) -> "AsyncOpenAI":
        if not _HAS_OPENAI:
            raise RuntimeError("缺少 openai 依赖，请执行：pip install -r requirements.txt")
        key = self.api_key
        if not key:
            raise RuntimeError(
                "尚未配置 Qwen API Key。\n"
                "请点击左上角「⚙️ 设置」填入，或设置环境变量 DASHSCOPE_API_KEY。"
            )
        # Key 变了就重建客户端（用户在设置里改完立刻生效）
        if self._client is None or self._client_key != key:
            self._client = AsyncOpenAI(
                api_key=key,
                base_url=str(self._cfg.get("qwen_base_url")),
                timeout=60.0,
                max_retries=1,
            )
            self._client_key = key
        return self._client

    def invalidate_client(self) -> None:
        """配置变更后调用，强制下次请求重建连接。"""
        self._client = None
        self._client_key = ""

    # ------------------------------------------------------------------ #
    # 历史
    # ------------------------------------------------------------------ #
    def begin_turn(self, user_text: str) -> int:
        """登记一条用户消息，并为 AI 回复预留槽位。返回该槽位的下标。"""
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": ""})
        # 兜底：极端长的会话才会触发（400 条 ≈ 200 轮），不会影响进行中的回合
        if len(self.history) > 400:
            del self.history[:200]
        return len(self.history) - 1

    def end_turn(self, slot: int, text: str) -> None:
        """把 AI 回复回填到预留槽位。"""
        if 0 <= slot < len(self.history):
            self.history[slot]["content"] = text or ""

    def clear_history(self) -> None:
        self.history.clear()

    def _build_messages(self, skill: "Skill") -> list[dict[str, str]]:
        turns = max(1, int(self._cfg.get("history_turns", 12)))
        recent = self.history[-(turns * 2):]
        messages: list[dict[str, str]] = [
            {"role": "system", "content": skill.system_prompt}
        ]
        for item in recent:
            content = (item.get("content") or "").strip()
            if not content:
                continue  # 被打断且一个字都没吐出来的占位，直接跳过
            messages.append({"role": item["role"], "content": content})
        return messages

    # ------------------------------------------------------------------ #
    # 流式请求
    # ------------------------------------------------------------------ #
    async def stream_reply(self, skill: "Skill") -> AsyncIterator[str]:
        """以异步生成器的方式，逐个 delta 吐出模型回复。

        ``stream=True`` 是整条流水线的源头：只有它是流式的，
        后面的字幕逐字渲染、句子切分、TTS 合成才能全部流水起来。
        """
        client = self._ensure_client()

        temperature = skill.temperature
        if temperature is None:
            temperature = float(self._cfg.get("qwen_temperature", 0.85))

        stream = await client.chat.completions.create(
            model=str(self._cfg.get("qwen_model", "qwen-plus")),
            messages=self._build_messages(skill),  # type: ignore[arg-type]
            temperature=float(temperature),
            max_tokens=int(self._cfg.get("qwen_max_tokens", 1024)),
            stream=True,          # <<< 全链路流式的起点
        )

        try:
            async for event in stream:
                if not event.choices:
                    continue
                delta = event.choices[0].delta
                piece = getattr(delta, "content", None)
                if piece:
                    yield piece
        finally:
            # 用户打断时我们会 break 出上面的循环；这里确保 HTTP 连接被关掉，
            # 否则 Qwen 那边还会继续计费生成。
            try:
                await stream.close()
            except Exception:  # noqa: BLE001
                pass


__all__ = ["LLMService", "SentenceBuffer", "Chunk", "clean_for_tts"]
