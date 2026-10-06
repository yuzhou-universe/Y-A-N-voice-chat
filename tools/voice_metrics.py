# -*- coding: utf-8 -*-
"""
tools/voice_metrics.py —— 语音客观指标（用来判断"活泼度"和"音色有没有变"）

调音色时最怕凭感觉，这个模块把两件事量化：

  【活泼度】—— 越大越活泼
    * 基频标准差 : 语调起伏。平铺直叙的语调 F0 标准差很小。
    * 能量调制   : 2~8Hz 的响度起伏（音节节奏感）。
    * 语速       : 每秒音节数（用能量包络的峰个数估）。

  【音色是否变了】—— 越接近 1 越像同一个人
    * 频谱包络相关性 : 把每帧的 log 幅度谱平均成一条"音色指纹"，
      两条指纹的相关系数。改变音高（pitch_rate）会把指纹整体平移，
      相关性立刻掉下来；只改语速则几乎不动。
    * 基频均值 : 直接看音高有没有被改动。

用法::

    python tools/voice_metrics.py a.wav b.wav c.wav
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def load_pcm(path: str | Path) -> tuple[np.ndarray, int]:
    """读 wav -> (float32 单声道 [-1,1], 采样率)。"""
    with wave.open(str(path), "rb") as reader:
        sr = reader.getframerate()
        channels = reader.getnchannels()
        width = reader.getsampwidth()
        raw = reader.readframes(reader.getnframes())
    dtype = {1: np.int8, 2: "<i2", 4: "<i4"}[width]
    data = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    peak = float(2 ** (8 * width - 1))
    data /= peak
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, sr


def estimate_f0(frame: np.ndarray, sr: int, fmin: int = 70, fmax: int = 400) -> float | None:
    """自相关法估基频。返回 Hz，判断不了就返回 None。"""
    frame = frame - frame.mean()
    if np.sqrt((frame ** 2).mean()) < 2e-3:
        return None
    corr = np.correlate(frame, frame, mode="full")[len(frame) - 1:]
    if corr[0] <= 0:
        return None
    corr = corr / corr[0]
    lo, hi = int(sr / fmax), int(sr / fmin)
    if hi >= len(corr) or lo >= hi:
        return None
    idx = int(np.argmax(corr[lo:hi])) + lo
    if corr[idx] < 0.30:      # 周期性不够强，多半是清音/噪声
        return None
    return sr / idx


def spectral_envelope(pcm: np.ndarray, sr: int, nfft: int = 512) -> np.ndarray:
    """音色指纹：所有发声帧的平均 log 幅度谱（已归一到单位长度）。"""
    hop = nfft // 2
    frames = [pcm[i:i + nfft] for i in range(0, len(pcm) - nfft, hop)]
    specs = []
    for fr in frames:
        if np.sqrt((fr ** 2).mean()) < 3e-3:
            continue
        win = fr * np.hanning(len(fr))
        mag = np.abs(np.fft.rfft(win, nfft))
        specs.append(np.log1p(mag))
    if not specs:
        return np.zeros(nfft // 2 + 1)
    env = np.mean(specs, axis=0)
    norm = np.linalg.norm(env)
    return env / norm if norm else env


def analyze(path: str | Path) -> dict:
    pcm, sr = load_pcm(path)
    dur = len(pcm) / sr

    # --- 基频 ---
    nfft = int(sr * 0.04)
    hop = nfft // 2
    f0s = [f for i in range(0, len(pcm) - nfft, hop)
           if (f := estimate_f0(pcm[i:i + nfft], sr)) is not None]

    # --- 能量包络（20ms 帧）---
    fr = int(sr * 0.02)
    env = np.sqrt((pcm[:len(pcm) // fr * fr].reshape(-1, fr) ** 2).mean(axis=1))
    env_db = 20 * np.log10(np.maximum(env, 1e-9))

    # --- 能量调制（2~8Hz，音节节奏）---
    e = env - env.mean()
    spec = np.abs(np.fft.rfft(e * np.hanning(len(e)))) if len(e) > 8 else np.zeros(4)
    freqs = np.fft.rfftfreq(len(e), d=0.02) if len(e) > 8 else np.zeros(4)
    band = (freqs >= 2) & (freqs <= 8)
    modulation = float(spec[band].sum() / max(spec[1:].sum(), 1e-9))

    # --- 语速：能量包络的峰个数 / 秒 ---
    thr = env_db.max() - 12
    above = env_db > thr
    peaks = int(np.sum(above[1:-1] & ~above[:-2] & (above[2:] >= above[1:-1]))) if len(above) > 2 else 0
    syllables_per_sec = peaks / dur if dur else 0.0

    return {
        "name": Path(path).stem,
        "duration": dur,
        "f0_mean": float(np.mean(f0s)) if f0s else 0.0,
        "f0_std": float(np.std(f0s)) if f0s else 0.0,
        "energy_std": float(env_db.std()),
        "modulation": modulation,
        "syllables_per_sec": syllables_per_sec,
        "envelope": spectral_envelope(pcm, sr),
    }


def main() -> int:
    paths = sys.argv[1:]
    if len(paths) < 2:
        print(__doc__)
        return 1

    results = [analyze(p) for p in paths]
    base = results[0]

    print("=" * 78)
    print(f"{'样本':<22}{'时长':>7}{'基频均值':>9}{'基频std':>9}{'能量std':>9}{'调制':>8}{'语速':>8}{'音色相似':>9}")
    print("-" * 78)
    for i, r in enumerate(results):
        sim = float(np.dot(r["envelope"], base["envelope"]))
        print(f"{r['name']:<22}{r['duration']:>6.2f}s{r['f0_mean']:>8.1f}Hz"
              f"{r['f0_std']:>8.1f}{r['energy_std']:>9.1f}{r['modulation']:>8.2f}"
              f"{r['syllables_per_sec']:>7.2f}{sim:>9.3f}")
    print("=" * 78)
    print("\n'音色相似' 是与第一个样本比：>0.99 基本是同一音色，<0.95 说明音色被改动了")
    print("'基频std / 调制 / 语速' 越大越活泼")
    return 0


if __name__ == "__main__":
    sys.exit(main())
