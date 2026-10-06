# -*- coding: utf-8 -*-
"""
tts_service.py —— 阿里云 CosyVoice 流式语音合成

设计要点
--------
TTS 的粒度是「**一轮 AI 回复 = 一个流式会话**」，而不是「一句话 = 一次请求」。

为什么不按句请求？因为每建一次连接都要重新握手（WebSocket + 鉴权），
一句 200ms 的语音要等 300ms 的握手，听起来就是一顿一顿的。
复用同一个会话、把句子连续 ``streaming_call`` 进去，音频才是连贯的。

线程模型
--------
DashScope SDK 是同步的，音频通过 **回调** 从 SDK 自己的线程吐出来。
回调线程和 asyncio 事件循环不是一回事，所以这里做了桥接：

    SDK 回调线程  --on_data-->  loop.call_soon_threadsafe  -->  asyncio.Queue
                                                                      |
    消费协程  <--async for---------------------------------------------+

``call_soon_threadsafe`` 是唯一安全的跨线程投递方式，
绝不能直接在回调线程里 ``queue.put_nowait`` 到 asyncio 队列（会丢唤醒）。

另外，不同版本的 dashscope 对 ``streaming_call`` 的返回值处理不一致
（有的走 callback，有的直接返回生成器），所以 ``_iter_audio`` 同时兼容两种，
并用 ``_callback_delivered`` 标志防止同一段音频被播两遍。
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import math
import mimetypes
import struct
import wave
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Iterable, Iterator

from audio_player import AudioChunk, BaseAudioPlayer

if TYPE_CHECKING:
    from config_manager import ConfigManager

try:
    import numpy as _np

    _HAS_NUMPY = True
except ImportError:  # pragma: no cover
    _HAS_NUMPY = False

logging.getLogger("dashscope").setLevel(logging.WARNING)
logging.getLogger("websocket").setLevel(logging.WARNING)


# =========================================================================== #
# 工具
# =========================================================================== #
def _iter_audio(result: Any) -> Iterator[bytes]:
    """把 ``streaming_call`` 的返回值统一成 bytes 迭代器。

    兼容三种形态：None（走 callback）、bytes、可迭代对象（生成器/列表）。
    """
    if result is None:
        return
    if isinstance(result, (bytes, bytearray, memoryview)):
        if len(result):
            yield bytes(result)
        return
    if isinstance(result, Iterable):
        for item in result:
            if item:
                yield bytes(item)


def pcm_to_wav(pcm: bytes, sample_rate: int, channels: int = 1, sample_width: int = 2) -> bytes:
    """给裸 PCM 套一个 WAV 头（pygame 后端需要完整容器才能解码）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm)
    return buf.getvalue()


# =========================================================================== #
# 抽象接口
# =========================================================================== #
class TTSStream(ABC):
    """一轮 AI 回复对应的音频流。

    用法::

        stream = await tts.open_stream(gen)
        relay = asyncio.create_task(consume(stream))   # 一边产出，一边播放
        await stream.push("你好。")                    # LLM 每切出一句就喂进来
        await stream.push("今天天气不错。")
        await stream.finish()                          # 全部喂完，等尾巴吐干净
    """

    @abstractmethod
    async def push(self, text: str) -> None:
        """喂入一句话（非阻塞）。"""

    @abstractmethod
    async def finish(self) -> None:
        """告知"没有更多文本了"，并等待剩余音频排空。"""

    @abstractmethod
    def abort(self) -> None:
        """立即打断：之后 push 的文本会被忽略，会话尽快关闭。"""

    @abstractmethod
    def __aiter__(self) -> AsyncIterator[bytes]:  # pragma: no cover - 协议声明
        ...


class BaseTTS(ABC):
    """TTS 引擎接口。"""

    name = "base"
    #: 引擎产出的音频格式，决定播放器要怎么解释字节流
    output_format: str = "pcm"

    def __init__(self, sample_rate: int = 22050) -> None:
        self._sample_rate = int(sample_rate)

    @property
    def available(self) -> bool:
        return True

    @abstractmethod
    async def open_stream(
        self,
        generation: int,
        *,
        voice: str | None = None,
        speech_rate: float | None = None,
        pitch_rate: float | None = None,
    ) -> TTSStream:
        """开启一轮合成会话。

        ``voice`` / ``speech_rate`` / ``pitch_rate`` 是**本轮覆盖值**，
        传 None 表示用 config 里的全局默认。人格（Skill）可以借它们
        定制自己的声线和说话节奏 —— 比如活泼的人格语速快一点。
        """

    def audio_chunk(self, data: bytes) -> AudioChunk:
        """把引擎吐出的裸字节，包装成播放器认识的 AudioChunk。"""
        return AudioChunk(
            data=data,
            fmt=self.output_format,
            sample_rate=self._sample_rate,
            channels=1,
            sample_width=2,
        )

    def close(self) -> None:
        """释放引擎级资源（连接池等）。"""


# =========================================================================== #
# 实现一：CosyVoice（DashScope 云端）
# =========================================================================== #
class _DashScopeCallback:
    """DashScope SDK 回调 -> asyncio 队列 的桥。

    SDK 在**自己的线程**里调用这些方法，所以这里**绝对不能**碰 tkinter，
    也不能直接操作 asyncio 队列，只能通过 call_soon_threadsafe 投递。
    """

    def __init__(self, put_threadsafe: Callable[[bytes | None], None]) -> None:
        self._put = put_threadsafe
        self.error: str | None = None

    def on_open(self) -> None:  # noqa: D102
        pass

    def on_data(self, data: Any) -> None:  # noqa: D102
        if data:
            try:
                self._put(bytes(data))
            except Exception:  # noqa: BLE001
                pass

    def on_complete(self) -> None:  # noqa: D102
        # 注意：不在这里放"结束哨兵"。
        # 我们自己调用 streaming_complete() 之后再放，时序才可控。
        pass

    def on_error(self, message: Any) -> None:  # noqa: D102
        self.error = str(message)
        self._put(None)  # 出错就结束这一路音频

    def on_close(self) -> None:  # noqa: D102
        pass


class CosyVoiceStream(TTSStream):
    """一次 CosyVoice 流式会话。"""

    def __init__(self, loop: asyncio.AbstractEventLoop, sample_rate: int, fmt: str) -> None:
        self._loop = loop
        self._sample_rate = sample_rate
        self._fmt = fmt  # "pcm" | "mp3"

        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._synth: Any = None
        self._aborted = False
        self._finished = False
        self._callback_delivered = False   # SDK 是否已经通过回调给过数据
        self._bg_tasks: set[asyncio.Future] = set()
        self.error: str | None = None

    # ------------------------------------------------------------------ #
    def bind(self, synth: Any) -> None:
        self._synth = synth

    # -- 供 SDK 回调线程调用 ------------------------------------------- #
    def put_threadsafe(self, item: bytes | None) -> None:
        if item is not None:
            self._callback_delivered = True
        try:
            self._loop.call_soon_threadsafe(self._emit, item)
        except RuntimeError:
            pass  # 事件循环已关闭，丢弃即可

    def _emit(self, item: bytes | None) -> None:
        """只能在事件循环线程里调用。"""
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:  # pragma: no cover
            pass

    # ------------------------------------------------------------------ #
    async def push(self, text: str) -> None:
        if self._aborted or self._finished or self._synth is None or not text.strip():
            return
        try:
            # streaming_call 是同步网络写，扔到线程池里跑，别堵住事件循环
            # （事件循环还负责轮询"是否被打断"，堵住会拖慢打断响应）
            result = await self._loop.run_in_executor(None, self._synth.streaming_call, text)
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self._emit(None)
            return
        # 兼容"直接返回生成器"的 SDK 版本；若回调已经给过数据就不重复播
        if not self._callback_delivered:
            for data in _iter_audio(result):
                self._emit(data)

    async def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        tail: Any = None
        if self._synth is not None:
            try:
                tail = await self._loop.run_in_executor(None, self._safe_complete)
            except Exception as exc:  # noqa: BLE001
                self.error = self.error or str(exc)
        if tail and not self._callback_delivered:
            for data in _iter_audio(tail):
                self._emit(data)
        self._emit(None)   # 结束哨兵

    def abort(self) -> None:
        """打断。必须立刻返回，不能等网络 —— 用户体验上"按下即静音"。"""
        if self._aborted:
            return
        self._aborted = True
        self._finished = True
        self._emit(None)   # 先让消费协程退出

        synth, self._synth = self._synth, None
        if synth is not None:
            # 关闭 WebSocket 是阻塞操作，丢到线程池里 fire-and-forget，
            # 否则"打断"这个动作本身就会卡住 UI 线程几百毫秒。
            fut = self._loop.run_in_executor(None, self._safe_close, synth)
            self._bg_tasks.add(fut)
            fut.add_done_callback(self._bg_tasks.discard)

    # ------------------------------------------------------------------ #
    def _safe_complete(self) -> Any:
        try:
            return self._synth.streaming_complete() if self._synth else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _safe_close(synth: Any) -> None:
        try:
            synth.streaming_complete()
        except Exception:  # noqa: BLE001
            pass
        try:
            synth.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            item = await self._queue.get()
            if item is None:
                break
            if self._aborted:
                break
            yield item


class CosyVoiceTTS(BaseTTS):
    """DashScope CosyVoice 语音合成（支持 Zero-shot 音色克隆）。

    ``config.cosyvoice_voice`` 填什么，就用什么音色：

      * **克隆音色 ID** —— ``cosyvoice-v2-myvoice-xxxxxxxx``，
        由 ``enroll_voice()`` / ``tools/enroll_voice.py`` 生成；
      * **官方预置音色** —— ``longxiaochun_v2`` / ``longwan_v2`` / ``longcheng_v2`` 等。

    ⚠️ 用克隆音色时，``config.cosyvoice_model`` 必须和克隆时的 ``target_model``
    一致（例如都是 ``cosyvoice-v2``），否则服务端会返回「音色不存在」。
    """

    name = "CosyVoice"

    def __init__(self, config: "ConfigManager", player: BaseAudioPlayer) -> None:
        super().__init__(int(config.get("sample_rate", 22050)))
        import dashscope  # 延迟导入：没装 dashscope 也不该让程序起不来

        key = str(config.get("cosyvoice_api_key", "") or "").strip()
        if not key:
            raise RuntimeError("未配置 CosyVoice API Key")

        dashscope.api_key = key
        self._dashscope = dashscope
        self._cfg = config
        self._player = player
        self._model = str(config.get("cosyvoice_model", "cosyvoice-v2"))
        self._voice = str(config.get("cosyvoice_voice", "longxiaochun_v2")) or "longxiaochun_v2"
        self._speech_rate = float(config.get("speech_rate", 1.0))
        self._pitch_rate = float(config.get("pitch_rate", 1.0))
        self._fmt = "pcm" if player.preferred_format == "pcm" else "mp3"
        self.output_format = self._fmt

    # ------------------------------------------------------------------ #
    async def open_stream(
        self,
        generation: int,  # noqa: ARG002
        *,
        voice: str | None = None,
        speech_rate: float | None = None,
        pitch_rate: float | None = None,
    ) -> TTSStream:
        loop = asyncio.get_running_loop()
        stream = CosyVoiceStream(loop, self._sample_rate, self._fmt)
        params = {
            "voice": voice or self._voice,
            "speech_rate": self._speech_rate if speech_rate is None else float(speech_rate),
            "pitch_rate": self._pitch_rate if pitch_rate is None else float(pitch_rate),
        }
        # 构造函数里会建立 WebSocket 连接 —— 阻塞操作，放线程池
        synth = await loop.run_in_executor(None, self._create_synth, stream, params)
        stream.bind(synth)
        return stream

    def _create_synth(self, stream: CosyVoiceStream, params: dict) -> Any:
        from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer

        audio_format = (
            AudioFormat.PCM_22050HZ_MONO_16BIT
            if self._fmt == "pcm"
            else AudioFormat.MP3_22050HZ_MONO_256KBPS
        )
        # 注意：不要传 instruction。cosyvoice-v2 不支持指令控制，
        # 传了会直接 InvalidParameter（实测 http_code 44）而不是降级忽略。
        return SpeechSynthesizer(
            model=self._model,
            voice=params["voice"],
            format=audio_format,
            speech_rate=params["speech_rate"],
            pitch_rate=params["pitch_rate"],
            callback=_DashScopeCallback(stream.put_threadsafe),
        )


# =========================================================================== #
# 实现二：Mock（离线跑通全链路用）
# =========================================================================== #
class MockStream(TTSStream):
    """不联网的假 TTS：按字数生成一段"电子音"，用来验证流水线是否通畅。

    它的价值在于：**没有任何 API Key 也能把"流式 -> 切句 -> 排队 -> 打断"
    整条链路跑一遍**。你听到的是嗡嗡声而不是人声，但时序行为和真实 TTS 一致。
    """

    def __init__(self, sample_rate: int, fmt: str, speed: float = 0.09) -> None:
        self._sr = sample_rate
        self._fmt = fmt
        self._speed = speed
        self._text_q: asyncio.Queue[str | None] = asyncio.Queue()
        self._audio_q: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._aborted = False
        self._task = asyncio.create_task(self._worker())

    async def push(self, text: str) -> None:
        if not self._aborted:
            self._text_q.put_nowait(text)

    async def finish(self) -> None:
        if not self._aborted:
            self._text_q.put_nowait(None)

    def abort(self) -> None:
        self._aborted = True
        self._task.cancel()
        self._audio_q.put_nowait(None)

    async def _worker(self) -> None:
        try:
            while True:
                text = await self._text_q.get()
                if text is None:
                    break
                for piece in self._render(text):
                    await asyncio.sleep(0.10)   # 模拟合成/网络延迟
                    if self._aborted:
                        break
                    self._audio_q.put_nowait(piece)
                if self._aborted:
                    break
            self._audio_q.put_nowait(None)
        except asyncio.CancelledError:  # pragma: no cover
            pass

    def _render(self, text: str) -> list[bytes]:
        """按字数生成正弦波，音高随字符抖动，听起来像在"说话"。"""
        duration = max(0.25, min(4.0, len(text) * self._speed))
        total = int(self._sr * duration)
        freqs = [180 + (ord(c) % 7) * 22 for c in text] or [200]

        if _HAS_NUMPY:
            t = _np.arange(total, dtype=_np.float32) / self._sr
            wave_arr = _np.zeros(total, dtype=_np.float32)
            seg = max(1, total // max(1, len(freqs)))
            for i, f in enumerate(freqs):
                a, b = i * seg, min(total, (i + 1) * seg)
                if a >= b:
                    break
                wave_arr[a:b] = _np.sin(2 * math.pi * f * t[a:b])
            # 整体加一个淡入淡出包络，避免爆音
            env = _np.ones(total, dtype=_np.float32)
            ramp = min(400, total // 4) or 1
            env[:ramp] = _np.linspace(0, 1, ramp)
            env[-ramp:] = _np.linspace(1, 0, ramp)
            pcm = (wave_arr * env * 0.25 * 32767).astype("<i2").tobytes()
        else:
            frames = bytearray()
            seg = max(1, total // max(1, len(freqs)))
            for n in range(total):
                f = freqs[min(len(freqs) - 1, n // seg)]
                amp = 0.25 * 32767
                frames += struct.pack("<h", int(math.sin(2 * math.pi * f * n / self._sr) * amp))
            pcm = bytes(frames)

        # 按 ~200ms 切片，模拟真实的流式分块
        block = int(self._sr * 0.2) * 2
        chunks = [pcm[i:i + block] for i in range(0, len(pcm), block)] or [b""]

        if self._fmt == "pcm":
            return [c for c in chunks if c]
        return [pcm_to_wav(pcm, self._sr)]

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            item = await self._audio_q.get()
            if item is None:
                break
            yield item


class MockTTS(BaseTTS):
    name = "Mock（离线测试）"

    def __init__(self, config: "ConfigManager", player: BaseAudioPlayer) -> None:
        super().__init__(int(config.get("sample_rate", 22050)))
        self._fmt = "pcm" if player.preferred_format == "pcm" else "wav"
        self.output_format = self._fmt
        self._speed = float(config.get("speech_rate", 1.0))

    async def open_stream(
        self,
        generation: int,  # noqa: ARG002
        *,
        voice: str | None = None,  # noqa: ARG002 - Mock 没有音色概念，但保持接口一致
        speech_rate: float | None = None,
        pitch_rate: float | None = None,  # noqa: ARG002
    ) -> TTSStream:
        speed = self._speed if speech_rate is None else float(speech_rate)
        # Mock 用"每个字占多少秒"来模拟语速，正好和 speech_rate 反向对应
        return MockStream(self._sample_rate, self._fmt, speed=0.09 / max(speed, 0.1))


# =========================================================================== #
# 实现三：Null（彻底关闭 TTS）
# =========================================================================== #
class _NullStream(TTSStream):
    async def push(self, text: str) -> None:
        return

    async def finish(self) -> None:
        return

    def abort(self) -> None:
        return

    async def __aiter__(self) -> AsyncIterator[bytes]:
        return
        yield  # pragma: no cover - 让函数成为异步生成器


class NullTTS(BaseTTS):
    """关闭语音。纯文字模式。"""

    name = "关闭"

    def __init__(self, reason: str = "") -> None:
        super().__init__(22050)
        self.reason = reason

    @property
    def available(self) -> bool:
        return False

    async def open_stream(self, generation: int, **kwargs) -> TTSStream:  # noqa: ARG002
        return _NullStream()


# =========================================================================== #
# 工厂
# =========================================================================== #
def create_tts(
    config: "ConfigManager",
    player: BaseAudioPlayer,
    on_status: Callable[[str], None] | None = None,
) -> BaseTTS:
    """按配置创建 TTS 引擎，自动降级。"""

    def notify(msg: str) -> None:
        print(f"[tts] {msg}")
        if on_status:
            try:
                on_status(msg)
            except Exception:  # noqa: BLE001
                pass

    provider = str(config.get("tts_provider", "auto") or "auto").lower()
    enabled = bool(config.get("tts_enabled", True))
    key = str(config.get("cosyvoice_api_key", "") or "").strip()

    if not enabled or provider == "none":
        notify("语音已关闭，仅文字模式")
        return NullTTS("语音已关闭")

    if provider == "mock":
        notify("使用 Mock 语音（离线测试，非人声）")
        return MockTTS(config, player)

    if key:
        try:
            engine = CosyVoiceTTS(config, player)
            notify(f"CosyVoice 就绪，音色={config.get('cosyvoice_voice')}")
            return engine
        except Exception as exc:  # noqa: BLE001
            notify(f"CosyVoice 初始化失败：{exc}")

    if provider == "cosyvoice":
        notify("未配置 CosyVoice API Key，语音已停用")
        return NullTTS("未配置 CosyVoice API Key")

    # auto 且没有 Key —— 用 Mock 先把链路跑通，用户能立刻看到完整效果
    notify("未配置 CosyVoice API Key，暂时使用 Mock 语音。配置后自动切换为真人音色。")
    return MockTTS(config, player)


# =========================================================================== #
# 音色克隆（Zero-shot 声音复刻）
# =========================================================================== #
#: 参考音频体积上限（官方限制 10MB）
REFERENCE_MAX_BYTES = 10 * 1024 * 1024
#: 官方推荐的参考音频时长区间（秒）
REFERENCE_IDEAL_SECONDS = (10.0, 20.0)
#: 不同合成模型对参考音频采样率的下限要求
_MIN_SAMPLE_RATE = {"cosyvoice-v1": 16000, "cosyvoice-v2": 24000}


def probe_wav(path: str | Path) -> dict | None:
    """读 WAV 头，返回采样率/时长/声道等信息。非 WAV 返回 None。"""
    path = Path(path)
    if path.suffix.lower() != ".wav":
        return None
    try:
        with wave.open(str(path), "rb") as reader:
            rate = reader.getframerate()
            frames = reader.getnframes()
            return {
                "channels": reader.getnchannels(),
                "sample_rate": rate,
                "sample_width": reader.getsampwidth(),
                "duration": frames / rate if rate else 0.0,
            }
    except Exception:  # noqa: BLE001 - 读不出来就当没探测到
        return None


def check_reference(path: str | Path, target_model: str = "cosyvoice-v2") -> list[str]:
    """体检参考音频，返回问题列表（空列表 = 没问题）。

    这里只报"确定的硬伤"，不做主观判断 —— 最终能不能复刻成功由服务端说了算。
    """
    path = Path(path)
    problems: list[str] = []

    if not path.exists():
        return [f"文件不存在：{path}"]

    size = path.stat().st_size
    if size > REFERENCE_MAX_BYTES:
        problems.append(
            f"文件 {size / 1024 / 1024:.1f}MB 超过 10MB 上限（且 Base64 编码后还会再涨 1/3）"
        )
    elif size == 0:
        problems.append("文件是空的")

    if path.suffix.lower() not in (".wav", ".mp3", ".m4a"):
        problems.append(f"格式 {path.suffix} 不受支持，仅支持 WAV(16bit) / MP3 / M4A")

    info = probe_wav(path)
    if info:
        if info["sample_width"] != 2:
            problems.append(f"WAV 位深为 {info['sample_width'] * 8}bit，官方要求 16bit")
        need = _MIN_SAMPLE_RATE.get(target_model)
        if need and info["sample_rate"] < need:
            problems.append(
                f"采样率为 {info['sample_rate']}Hz，低于 {target_model} 要求的 {need}Hz"
            )
        low, high = REFERENCE_IDEAL_SECONDS
        if info["duration"] < 3.0:
            problems.append(f"时长仅 {info['duration']:.1f} 秒，至少要 3 秒连续朗读")
        elif not (low <= info["duration"] <= high):
            problems.append(
                f"时长 {info['duration']:.1f} 秒不在推荐的 {low:.0f}~{high:.0f} 秒区间"
                "（不致命，但克隆相似度可能下降）"
            )

    return problems


def audio_to_data_uri(path: str | Path) -> str:
    """把本地音频转成 Base64 Data URI。

    这是绕开"参考音频必须是公网 URL"的关键：DashScope 的 ``create_voice``
    除了 http(s):// 之外，也接受 ``data:audio/wav;base64,xxxx`` 形式的内联数据。
    有了它，本地文件不用上传到任何地方就能直接复刻。
    """
    path = Path(path)
    mime = mimetypes.guess_type(str(path))[0] or "audio/wav"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def enroll_voice(
    api_key: str,
    reference: str | Path,
    prefix: str = "myvoice",
    target_model: str = "cosyvoice-v2",
    language_hints: list[str] | None = None,
    max_prompt_audio_length: float | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> str:
    """用参考音频创建一个专属音色，返回 ``voice_id``。

    ``reference`` 可以是三种东西：

      * 本地文件路径  -> 自动转成 Base64 Data URI（推荐，无需公网）
      * ``http(s)://`` 公网 URL
      * 已经是 ``data:`` 开头的 Data URI

    ⚠️ ``target_model`` 必须和后续语音合成用的模型一致，
    否则合成时会报"音色不存在"。config.json 里的 ``cosyvoice_model``
    必须填成同一个值。
    """
    import dashscope
    from dashscope.audio.tts_v2 import VoiceEnrollmentService

    dashscope.api_key = api_key

    ref = str(reference)
    if ref.startswith(("http://", "https://", "data:", "oss://")):
        url = ref
    else:
        for problem in check_reference(ref, target_model):
            if on_warning:
                on_warning(problem)
        url = audio_to_data_uri(ref)

    if not prefix.isalnum() or len(prefix) > 10:
        raise ValueError(f"prefix 只能包含字母和数字且不超过 10 个字符，当前为 {prefix!r}")

    service = VoiceEnrollmentService(api_key=api_key)
    voice_id = service.create_voice(
        target_model=target_model,
        prefix=prefix,
        url=url,
        language_hints=language_hints,
        max_prompt_audio_length=max_prompt_audio_length,
    )
    return str(voice_id)


def list_voices(api_key: str, prefix: str | None = None, page_size: int = 50) -> list[dict]:
    """列出账号下已克隆的音色。"""
    import dashscope
    from dashscope.audio.tts_v2 import VoiceEnrollmentService

    dashscope.api_key = api_key
    return list(VoiceEnrollmentService(api_key=api_key).list_voices(prefix=prefix, page_size=page_size))


def delete_voice(api_key: str, voice_id: str) -> None:
    """删除一个克隆音色（音色数量有配额上限，不用了记得删）。"""
    import dashscope
    from dashscope.audio.tts_v2 import VoiceEnrollmentService

    dashscope.api_key = api_key
    VoiceEnrollmentService(api_key=api_key).delete_voice(voice_id=voice_id)


__all__ = [
    "BaseTTS",
    "TTSStream",
    "CosyVoiceTTS",
    "CosyVoiceStream",
    "MockTTS",
    "NullTTS",
    "create_tts",
    "enroll_voice",
    "list_voices",
    "delete_voice",
    "check_reference",
    "audio_to_data_uri",
    "probe_wav",
    "pcm_to_wav",
]
