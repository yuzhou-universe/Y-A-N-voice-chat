# -*- coding: utf-8 -*-
"""验证 SoundDevicePlayer 的 PortAudio 回调确实在消费队列（而不只是入队）。

判据：``is_playing`` 只有在**回调把队列排空之后**才会被置回 False。
如果回调没跑起来（设备打不开 / 回调异常），这个标志会永远停在 True。
"""
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from audio_player import AudioChunk, SoundDevicePlayer

SR = 22050
player = SoundDevicePlayer(sample_rate=SR, volume=0.5)
player.start()
player.set_token(1)

# 造 1 秒 440Hz 正弦，按 100ms 切片入队
tone = b"".join(
    struct.pack("<h", int(12000 * __import__("math").sin(2 * 3.14159265 * 440 * n / SR)))
    for n in range(SR)
)
block = SR // 10 * 2
for i in range(0, len(tone), block):
    player.submit(AudioChunk(tone[i:i + block], "pcm", SR), 1)

print(f"入队完成，is_playing={player.is_playing}")

# 1 秒音频 + 缓冲，最多等 3 秒
deadline = time.time() + 3.0
while player.is_playing and time.time() < deadline:
    time.sleep(0.05)

drained = not player.is_playing
print(f"3 秒后 is_playing={player.is_playing}  -> 回调{'已消费完毕 ✓' if drained else '没有消费 ✗'}")

# 打断：入队一大堆，立刻打断，必须马上静音
for i in range(0, len(tone), block):
    player.submit(AudioChunk(tone[i:i + block], "pcm", SR), 1)
player.interrupt()
print(f"打断后 is_playing={player.is_playing}（应为 False）")

# 旧 token 的数据必须被丢弃
player.submit(AudioChunk(tone[:block], "pcm", SR), 1)
print(f"旧 token 入队后 is_playing={player.is_playing}（应为 False，说明被丢弃）")

player.stop()
ok = drained and not player.is_playing
print("\n音频回调测试 " + ("通过 ✓" if ok else "失败 ✗"))
sys.exit(0 if ok else 1)
