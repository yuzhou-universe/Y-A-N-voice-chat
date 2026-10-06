# -*- coding: utf-8 -*-
"""
chat_controller.py —— 流水线编排（LLM -> 切句 -> TTS -> 播放）

这个模块把四个服务粘起来，并且负责两件最难的事：**打断** 和 **线程安全**。

线程模型
--------
::

    主线程 (Tk)          pipeline 线程                 播放器线程
    ─────────────        ────────────────────          ──────────────
    Tk mainloop          asyncio event loop            PortAudio 回调
        │                        │                            │
        │ run_coroutine_         │                            │
        │ threadsafe ───────────>│                            │
        │                        │ submit(AudioChunk) ───────>│
        │ <──── ui_queue ────────│                            │
        │  after(30ms) 轮询       │                            │

  规则只有一条：**任何线程都不能直接碰 Tk 组件**。
  后台 -> UI 的数据一律走 ``queue.Queue``，由主线程用 ``after()`` 轮询取出。

打断（interrupt）的实现
-----------------------
用「**generation 计数器**」而不是 ``task.cancel()``：:

    self._gen += 1        # 所有旧协程在下一个检查点自己退出

  为什么不用 cancel？因为 ``task.cancel()`` 会在任意 await 点抛出
  ``CancelledError``，包括在 ``finally`` 里做清理 await 的时候 —— 那会让
  历史回填、WebSocket 关闭这些收尾动作被二次取消，出现状态不一致。
  协作式退出虽然"慢一个检查点"，但行为完全可预测。

  每次检查 ``if gen != self._gen: break``，成本是一次整数比较。
"""

from __future__ import annotations

import asyncio
import threading
from queue import Queue
from typing import TYPE_CHECKING, Any

from audio_player import BaseAudioPlayer, create_audio_player
from llm_service import LLMService, SentenceBuffer
from tts_service import BaseTTS, create_tts

if TYPE_CHECKING:
    from config_manager import ConfigManager
    from skill_manager import Skill, SkillManager

#: 一轮回复的收尾等待上限（秒）。超时就不再等，避免拖住下一条消息。
_TURN_DRAIN_TIMEOUT = 2.0
#: 音频 relay 协程的收尾等待上限。
_RELAY_TIMEOUT = 4.0


class ChatController:
    """把用户输入变成"文字 + 语音"的编排器。"""

    def __init__(
        self,
        config: "ConfigManager",
        skills: "SkillManager",
        ui_queue: "Queue[tuple]",
    ) -> None:
        self.cfg = config
        self.skills = skills
        self.ui = ui_queue

        # --- 后台服务 ---
        self.llm = LLMService(config)
        self.audio: BaseAudioPlayer = create_audio_player(config, self._status)
        self.tts: BaseTTS = create_tts(config, self.audio, self._status)

        # --- 线程 / 事件循环 ---
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

        # --- 回合状态 ---
        self._gen = 0            # 每次打断/新消息自增，旧协程靠它判断"我过期了"
        self._msg_seq = 0        # 给 GUI 的消息编号（只在 _start_lock 内自增）
        self._turn_task: asyncio.Task | None = None
        self._stream = None
        self._start_lock: asyncio.Lock | None = None
        self._running = False

    # ================================================================== #
    # 生命周期
    # ================================================================== #
    def start(self) -> None:
        """启动后台事件循环线程。"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, name="pipeline", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._start_lock = asyncio.Lock()
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        """退出前清理：停播放器、关事件循环。"""
        self._running = False
        try:
            self.audio.interrupt()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.audio.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.tts.close()
        except Exception:  # noqa: BLE001
            pass

        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # ================================================================== #
    # 对外接口（全部是线程安全的，可从 Tk 主线程直接调）
    # ================================================================== #
    def submit(self, text: str) -> None:
        """用户发了一条消息。"""
        text = (text or "").strip()
        if not text or self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._handle(text), self._loop)

    def interrupt(self) -> None:
        """手动打断（例如点了"停止"按钮）。"""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._interrupt_now)

    def _interrupt_now(self) -> None:
        self._gen += 1
        self._cancel_audio()

    def _cancel_audio(self) -> None:
        """清空音频队列 + 关掉当前 TTS 会话。必须瞬间返回。"""
        try:
            self.audio.interrupt()
        except Exception:  # noqa: BLE001
            pass
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.abort()
            except Exception:  # noqa: BLE001
                pass

    @property
    def is_speaking(self) -> bool:
        """GUI 用它显示"正在说话"指示灯。"""
        try:
            return self.audio.is_playing
        except Exception:  # noqa: BLE001
            return False

    def reset_history(self) -> None:
        self.llm.clear_history()

    def set_skill(self, name: str) -> "Skill":
        return self.skills.select(name)

    def apply_config(self) -> None:
        """设置对话框保存后调用：让新配置立刻生效。"""
        self.llm.invalidate_client()

        def _rebuild() -> None:
            old_audio = self.audio
            try:
                old_audio.interrupt()
                old_audio.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.tts.close()
            except Exception:  # noqa: BLE001
                pass
            self.audio = create_audio_player(self.cfg, self._status)
            self.tts = create_tts(self.cfg, self.audio, self._status)

        if self._loop is not None:
            self._loop.call_soon_threadsafe(_rebuild)

    def shutdown_tts(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._cancel_audio)

    # ================================================================== #
    # 内部工具
    # ================================================================== #
    def _status(self, message: str) -> None:
        """服务层上报状态（音频后端、TTS 引擎等），转给 GUI 显示。"""
        self.ui.put(("status", message))

    def _emit(self, *event: Any) -> None:
        self.ui.put(event)

    # ================================================================== #
    # 回合调度
    # ================================================================== #
    async def _handle(self, text: str) -> None:
        """收到一条用户消息：先干净利落地结束上一轮，再开新的一轮。"""
        lock = self._start_lock
        assert lock is not None
        async with lock:
            # 1) 让所有旧协程失效 —— 它们会在下一个检查点自行退出
            self._gen += 1
            gen = self._gen

            # 2) 物理静音：清空播放队列 + 关掉 TTS WebSocket
            self._cancel_audio()

            # 3) 等上一轮把收尾动作做完（回填历史、关闭连接）
            old = self._turn_task
            if old is not None and not old.done():
                try:
                    # shield：超时只取消"等待"这个动作，不取消 old 本身
                    await asyncio.wait_for(asyncio.shield(old), timeout=_TURN_DRAIN_TIMEOUT)
                except BaseException:  # noqa: BLE001 - 超时/异常都不该影响新回合
                    pass

            # 4) 登记用户消息 + 预留 AI 槽位（顺序在这里就固定了）
            slot = self.llm.begin_turn(text)
            self.audio.set_token(gen)

            # 5) 开跑。msg_id 在锁内自增，保证同一时刻只有一个回合在写
            self._msg_seq += 1
            self._turn_task = asyncio.create_task(self._run_turn(self._msg_seq, slot, gen))

    async def _run_turn(self, msg_id: int, slot: int, gen: int) -> None:
        """一轮完整的 LLM -> TTS 流水线。"""
        self._emit("ai_start", msg_id)

        skill = self.skills.current()
        buffer = SentenceBuffer()
        pieces: list[str] = []
        stream = None
        relay: asyncio.Task | None = None

        try:
            # ---- 开一路 TTS 会话（整轮复用，音频才连贯）----
            try:
                # 把人格的声线定制传下去；None 表示沿用 config 全局值
                stream = await self.tts.open_stream(
                    gen,
                    voice=skill.voice or None,
                    speech_rate=skill.speech_rate,
                    pitch_rate=skill.pitch_rate,
                )
                self._stream = stream
                relay = asyncio.create_task(self._relay_audio(stream, gen))
            except Exception as exc:  # noqa: BLE001 - TTS 挂了也要能出字
                stream = None
                self._emit("status", f"语音合成不可用：{exc}")

            # ---- 核心循环：LLM 流式 -> 字幕 + 切句 -> TTS ----
            async for delta in self.llm.stream_reply(skill):
                if gen != self._gen:
                    break  # ← 被打断了，协作式退出
                pieces.append(delta)
                self._emit("ai_delta", msg_id, delta)

                # 这一行是整个项目的"生死线"：
                # 同一个 delta 走两条路 —— 一路进 GUI 气泡，一路进 TTS。
                # 切句只影响 TTS，字幕永远是逐字实时刷新的。
                for chunk in buffer.feed(delta):
                    if stream is not None and chunk.speak and chunk.text.strip():
                        await stream.push(chunk.text)

            # ---- 收尾：把缓冲区里剩下的半句也送出去 ----
            if gen == self._gen:
                for chunk in buffer.flush():
                    if stream is not None and chunk.speak and chunk.text.strip():
                        await stream.push(chunk.text)

        except asyncio.CancelledError:  # pragma: no cover
            raise
        except Exception as exc:  # noqa: BLE001
            self._emit("error", f"请求失败：{exc}")
        finally:
            full_text = "".join(pieces)
            # 即便被打断，也要把已生成的内容回填进历史，保持上下文连贯
            self.llm.end_turn(slot, full_text)

            if stream is not None:
                try:
                    await stream.finish()
                except Exception:  # noqa: BLE001
                    pass
                if self._stream is stream:
                    self._stream = None
            if relay is not None:
                try:
                    await asyncio.wait_for(relay, timeout=_RELAY_TIMEOUT)
                except Exception:  # noqa: BLE001
                    relay.cancel()

            self._emit("ai_end", msg_id, full_text)

    async def _relay_audio(self, stream, gen: int) -> None:
        """把 TTS 吐出的音频块搬进播放队列。"""
        try:
            async for data in stream:
                if gen != self._gen:
                    break
                if not data:
                    continue
                self.audio.submit(self.tts.audio_chunk(data), gen)
        except asyncio.CancelledError:  # pragma: no cover
            pass
        except Exception as exc:  # noqa: BLE001
            self._emit("status", f"播放异常：{exc}")


__all__ = ["ChatController"]
