# -*- coding: utf-8 -*-
"""
audio_player.py —— 音频播放队列 + 打断机制

两个后端，接口完全一致，由 ``config.audio_backend`` 选择：

  * ``SoundDevicePlayer``（推荐）—— sounddevice + 回调式输出流，吃 **PCM** 裸流。
    延迟最低、天然支持"边合成边播放"。callback 由 PortAudio 实时线程驱动，
    所以回调里只做内存搬运，绝不允许阻塞。

  * ``PygameAudioPlayer``（备选）—— pygame.mixer 的 Channel 队列，吃 **MP3**。
    在 sounddevice 装不上的机器上（没有可用输出设备 / PortAudio 编译失败）兜底。

  * ``NullAudioPlayer`` —— 全部失败时的哑实现，保证 UI 还能跑（只是没声音）。

打断（interrupt）的关键设计：
  每个音频块都带一个 ``token``（就是 Controller 那一轮的 generation 号）。
  ``interrupt()`` 会清空队列 **并且** 把当前 token 置空；
  这样即使旧回合还有音频块在网络路上飘着，``submit()`` 也会因为 token 不匹配
  而直接丢弃它们 —— 不会出现"打断了半秒后旧声音又冒出来"的经典 bug。
"""

from __future__ import annotations

import io
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from config_manager import ConfigManager

try:
    import numpy as _np

    _HAS_NUMPY = True
except ImportError:  # pragma: no cover
    _HAS_NUMPY = False


# =========================================================================== #
# 音频数据块
# =========================================================================== #
@dataclass
class AudioChunk:
    """一块音频数据。

    ``fmt``:
      * ``"pcm"`` —— 裸 PCM（sounddevice 后端用），需配合 sample_rate/channels/sample_width
      * ``"mp3"`` —— MP3 字节流（pygame 后端用）
      * ``"wav"`` —— 完整 WAV 容器（pygame 后端用，MockTTS 走这条路）
    """

    data: bytes
    fmt: str = "pcm"
    sample_rate: int = 22050
    channels: int = 1
    sample_width: int = 2  # 字节数，16bit = 2

    @property
    def duration(self) -> float:
        """估算时长（秒），仅对 PCM 有意义。"""
        if self.fmt != "pcm" or not self.data:
            return 0.0
        frame = max(1, self.channels * self.sample_width)
        return len(self.data) / frame / max(1, self.sample_rate)


# =========================================================================== #
# 抽象基类
# =========================================================================== #
class BaseAudioPlayer(ABC):
    """所有播放器的统一接口。"""

    #: 该后端偏好的 TTS 输出格式（决定向 CosyVoice 请求 pcm 还是 mp3）
    preferred_format: str = "pcm"

    def __init__(self) -> None:
        self._token: int | None = None
        self._volume: float = 0.9

    # ---------------- 生命周期 ----------------
    def start(self) -> None:
        """打开音频设备。失败时抛异常，由工厂函数兜底。"""

    def stop(self) -> None:
        """释放设备。"""

    # ---------------- 数据 ----------------
    @abstractmethod
    def submit(self, chunk: AudioChunk, token: int) -> None:
        """把一块音频塞进播放队列。token 不匹配则丢弃。"""

    @abstractmethod
    def interrupt(self) -> None:
        """立即停止播放并清空队列。"""

    def set_token(self, token: int) -> None:
        """开启新一轮，之后只有携带该 token 的音频块会被接受。

        ``None`` 是保留值，含义是"全部拒收"（见 ``interrupt()``），
        所以调用方传进来的回合号必须是非 None 的 int。
        """
        self._token = token

    @property
    @abstractmethod
    def is_playing(self) -> bool:
        """当前是否正在出声（GUI 用它显示"正在说话"指示灯）。"""

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, float(volume)))

    @property
    def name(self) -> str:
        return self.__class__.__name__


# =========================================================================== #
# 工具：PCM 音量缩放
# =========================================================================== #
def _scale_pcm16(data: bytes, gain: float) -> bytes:
    """对 16bit PCM 做音量缩放。gain >= 1 时原样返回。"""
    if not data or gain >= 0.999:
        return data
    if _HAS_NUMPY:
        samples = _np.frombuffer(data, dtype="<i2").astype(_np.float32)
        samples *= gain
        return _np.clip(samples, -32768, 32767).astype("<i2").tobytes()
    # 没有 numpy 时用 array 兜底：每 100ms 音频也就 ~2205 个样本，开销可接受
    import array
    import sys

    buf = array.array("h")
    buf.frombytes(data[: len(data) // 2 * 2])
    if sys.byteorder == "big":
        buf.byteswap()
    for i in range(len(buf)):
        buf[i] = int(buf[i] * gain)
    if sys.byteorder == "big":
        buf.byteswap()
    return buf.tobytes()


# =========================================================================== #
# 后端一：sounddevice（PCM 流式）
# =========================================================================== #
class SoundDevicePlayer(BaseAudioPlayer):
    """基于 sounddevice.RawOutputStream 的实时 PCM 播放器。

    工作方式：
      * 输出流在 ``start()`` 后就一直开着，回调以固定块大小（默认 1024 帧）
        不停向我们要数据。
      * ``submit()`` 只是把字节追加到内部 deque；回调负责搬运。
      * 队列空了就输出静音 —— 句与句之间的自然停顿正需要这个。
    """

    preferred_format = "pcm"

    def __init__(
        self,
        sample_rate: int = 22050,
        channels: int = 1,
        blocksize: int = 1024,
        volume: float = 0.9,
        device: int | str | None = None,
    ) -> None:
        super().__init__()
        self._sr = int(sample_rate)
        self._ch = int(channels)
        self._blocksize = int(blocksize)
        self._device = device
        self._volume = float(volume)

        self._queue: deque[bytes] = deque()
        self._pending = bytearray()   # 不足一个 block 的残留字节
        self._lock = threading.Lock()
        self._active = False          # 是否还有音频在播
        self._stream = None

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        import sounddevice as sd

        self._stream = sd.RawOutputStream(
            samplerate=self._sr,
            channels=self._ch,
            dtype="int16",
            blocksize=self._blocksize,
            device=self._device,
            callback=self._callback,
            finished_callback=None,
        )
        self._stream.start()

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    # PortAudio 实时回调 —— 严禁阻塞、严禁分配大对象、严禁碰 UI
    # ------------------------------------------------------------------ #
    def _callback(self, outdata, frames, time_info, status) -> None:  # noqa: ANN001
        need = frames * self._ch * 2  # 16bit -> 每样本 2 字节

        with self._lock:
            # 从队列里尽量凑满一个 block
            while len(self._pending) < need and self._queue:
                self._pending += self._queue.popleft()
            take = min(len(self._pending), need)
            if take:
                piece = bytes(self._pending[:take])
                del self._pending[:take]
            else:
                piece = b""
            # 队列彻底空了 -> 标记为"不在播放"，GUI 的说话指示灯会熄灭
            if len(piece) < need and not self._queue and not self._pending:
                self._active = False

        if len(piece) < need:
            piece += b"\x00" * (need - len(piece))  # 欠载补静音
        outdata[:] = piece

    # ------------------------------------------------------------------ #
    def submit(self, chunk: AudioChunk, token: int) -> None:
        if chunk.fmt != "pcm" or not chunk.data:
            return
        data = _scale_pcm16(chunk.data, self._volume)
        with self._lock:
            if token != self._token:
                return  # 旧回合的残响，丢弃
            self._queue.append(data)
            self._active = True

    def interrupt(self) -> None:
        """静音并作废当前 token —— 之后所有旧 token 的数据都会被 submit 拒绝。

        注意 ``_token`` 的语义是"当前唯一有效的回合号"，``None`` 表示
        **什么都不接受**（而不是"什么都能进"）。打断之后到下一轮
        ``set_token()`` 之前的这段窗口，正是旧音频在网络上飘着的时刻，
        必须一律拒收，否则会出现"打断了半秒后旧声音又冒出来"。
        """
        with self._lock:
            self._token = None
            self._queue.clear()
            self._pending.clear()
            self._active = False

    @property
    def is_playing(self) -> bool:
        return self._active

    @property
    def sample_rate(self) -> int:
        return self._sr


# =========================================================================== #
# 后端二：pygame.mixer（MP3 队列）
# =========================================================================== #
class PygameAudioPlayer(BaseAudioPlayer):
    """基于 pygame.mixer.Channel 的队列播放器。

    用 ``Channel.queue()`` 把多个 Sound 串起来，天然形成播放队列；
    打断就是 ``stop()`` + ``unqueue()``。
    """

    preferred_format = "mp3"

    def __init__(self, sample_rate: int = 22050, channels: int = 1, volume: float = 0.9) -> None:
        super().__init__()
        self._sr = int(sample_rate)
        self._ch = int(channels)
        self._volume = float(volume)
        self._channel = None
        self._lock = threading.Lock()

    def start(self) -> None:
        import pygame

        pygame.mixer.pre_init(frequency=self._sr, size=-16, channels=self._ch, buffer=512)
        pygame.mixer.init()
        self._channel = pygame.mixer.Channel(0)

    def stop(self) -> None:
        try:
            import pygame

            pygame.mixer.quit()
        except Exception:  # noqa: BLE001
            pass
        self._channel = None

    # ------------------------------------------------------------------ #
    def _make_sound(self, chunk: AudioChunk):
        import pygame

        if chunk.fmt == "pcm":
            return pygame.mixer.Sound(buffer=chunk.data)
        # mp3 / wav：交给 SDL_mixer 自己去解码
        return pygame.mixer.Sound(file=io.BytesIO(chunk.data))

    def submit(self, chunk: AudioChunk, token: int) -> None:
        if self._channel is None or not chunk.data:
            return
        with self._lock:
            if token != self._token:
                return
        try:
            sound = self._make_sound(chunk)
        except Exception:  # noqa: BLE001 - 解码失败不应该让整个应用崩掉
            return
        sound.set_volume(self._volume)
        try:
            if self._channel.get_busy():
                self._channel.queue(sound)
            else:
                self._channel.play(sound)
        except Exception:  # noqa: BLE001
            pass

    def interrupt(self) -> None:
        with self._lock:
            self._token = None
        if self._channel is None:
            return
        try:
            self._channel.stop()
            self._channel.unqueue()
        except Exception:  # noqa: BLE001
            pass

    @property
    def is_playing(self) -> bool:
        if self._channel is None:
            return False
        try:
            return bool(self._channel.get_busy())
        except Exception:  # noqa: BLE001
            return False


# =========================================================================== #
# 后端三：哑实现（保命用）
# =========================================================================== #
class NullAudioPlayer(BaseAudioPlayer):
    """没有任何可用音频后端时的占位实现 —— 文字照常显示，只是没声音。"""

    preferred_format = "pcm"

    def submit(self, chunk: AudioChunk, token: int) -> None:  # noqa: ARG002
        return

    def interrupt(self) -> None:
        return

    @property
    def is_playing(self) -> bool:
        return False


# =========================================================================== #
# 工厂
# =========================================================================== #
def create_audio_player(
    config: "ConfigManager",
    on_status: Callable[[str], None] | None = None,
) -> BaseAudioPlayer:
    """按配置创建播放器；自动降级，永远不会抛异常。

    降级链：``指定后端`` -> ``sounddevice`` -> ``pygame`` -> ``NullAudioPlayer``
    """
    backend = str(config.get("audio_backend", "auto") or "auto").lower()
    sample_rate = int(config.get("sample_rate", 22050))
    volume = float(config.get("volume", 0.9))

    def notify(msg: str) -> None:
        print(f"[audio] {msg}")
        if on_status:
            try:
                on_status(msg)
            except Exception:  # noqa: BLE001
                pass

    candidates: list[str]
    if backend in ("sounddevice", "pygame"):
        candidates = [backend]
    else:
        candidates = ["sounddevice", "pygame"]

    for name in candidates:
        try:
            if name == "sounddevice":
                player: BaseAudioPlayer = SoundDevicePlayer(
                    sample_rate=sample_rate, volume=volume
                )
            else:
                player = PygameAudioPlayer(sample_rate=sample_rate, volume=volume)
            player.start()
            notify(f"音频后端：{player.name}（采样率 {sample_rate}Hz）")
            return player
        except Exception as exc:  # noqa: BLE001
            notify(f"{name} 初始化失败：{exc}")

    notify("没有可用的音频后端，将只显示文字（TTS 静音）")
    return NullAudioPlayer()


__all__ = [
    "AudioChunk",
    "BaseAudioPlayer",
    "SoundDevicePlayer",
    "PygameAudioPlayer",
    "NullAudioPlayer",
    "create_audio_player",
]
