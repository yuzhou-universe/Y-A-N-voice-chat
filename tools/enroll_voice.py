# -*- coding: utf-8 -*-
"""
tools/enroll_voice.py —— CosyVoice Zero-shot 音色复刻

用一段参考音频在 DashScope 上创建一个专属音色，返回 ``voice_id``。
把它填进 config.json 的 ``cosyvoice_voice``，之后所有语音都用这个音色。

用法::

    # 看一眼参考音频合不合格（不联网、不消耗配额）
    python tools/enroll_voice.py --check assets/reference.wav

    # 克隆（本地文件会自动转成 Base64，不需要公网 URL）
    python tools/enroll_voice.py --audio assets/reference.wav --save

    # 已经有的公网 URL 也行
    python tools/enroll_voice.py --url https://example.com/ref.wav --save

    # 管理已克隆的音色
    python tools/enroll_voice.py --list
    python tools/enroll_voice.py --delete cosyvoice-v2-myvoice-xxxxxxxx

⚠️ 参考音频决定克隆质量，别偷懒：
    * 时长 10~20 秒（至少 3 秒连续朗读，最长 60 秒）
    * 单人、无背景音乐、无混响、无环境噪音
    * WAV(16bit) / MP3 / M4A，≤ 10MB
    * 采样率 ≥ 24kHz（cosyvoice-v2 要求；v1 是 16kHz）
    * 内容用**平稳的陈述句**，别唱歌、别太夸张

⚠️ 克隆时用的模型（--model）必须和合成时 config.json 里的
    cosyvoice_model 一致，否则合成会报"音色不存在"。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许从项目根目录直接运行本脚本
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from config_manager import ConfigManager              # noqa: E402
from tts_service import (                             # noqa: E402
    check_reference,
    delete_voice,
    enroll_voice,
    list_voices,
)


def _api_key(cfg: ConfigManager) -> str | None:
    key = str(cfg.get("cosyvoice_api_key", "") or "").strip()
    if not key:
        print(
            "[错误] 未配置 cosyvoice_api_key。\n"
            "       请先在「⚙ 设置」里填入 DashScope API Key，"
            "或设置环境变量 DASHSCOPE_API_KEY。",
            file=sys.stderr,
        )
        return None
    return key


def _report_reference(path: str, model: str) -> bool:
    """体检参考音频。返回 True 表示没有硬伤。"""
    problems = check_reference(path, model)
    if not problems:
        print(f"参考音频体检通过：{path}")
        return True

    hard = [p for p in problems if "不致命" not in p]
    print(f"参考音频存在 {len(problems)} 个问题：")
    for problem in problems:
        print(f"  · {problem}")
    if hard:
        print("\n以上带硬伤的问题会导致复刻失败或效果很差，建议先处理。")
    return not hard


def main() -> int:
    parser = argparse.ArgumentParser(description="CosyVoice 音色复刻")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--audio", help="本地参考音频路径（自动转 Base64，无需公网）")
    source.add_argument("--url", help="参考音频的公网 URL")
    source.add_argument("--list", action="store_true", help="列出已克隆的音色")
    source.add_argument("--delete", metavar="VOICE_ID", help="删除指定音色")
    source.add_argument("--check", metavar="AUDIO", help="只体检音频，不联网")

    parser.add_argument("--prefix", default="myvoice", help="音色名前缀（字母数字，≤10 字符）")
    parser.add_argument("--model", default="cosyvoice-v2", help="目标模型，需与合成模型一致")
    parser.add_argument("--lang", help="语种提示，如 zh / en（选填）")
    parser.add_argument("--max-prompt", type=float, help="预处理后最大时长秒数，范围 3~30（选填）")
    parser.add_argument("--save", action="store_true", help="克隆成功后写回 config.json")
    parser.add_argument("--force", action="store_true", help="体检不过也强行克隆")
    args = parser.parse_args()

    cfg = ConfigManager().load()

    # ---- 只体检，不联网 ----
    if args.check:
        _report_reference(args.check, args.model)
        return 0

    key = _api_key(cfg)
    if key is None:
        return 1

    # ---- 列出音色 ----
    if args.list:
        try:
            voices = list_voices(key, prefix=args.prefix if args.prefix != "myvoice" else None)
        except Exception as exc:  # noqa: BLE001
            print(f"[失败] 查询音色列表出错：{exc}", file=sys.stderr)
            return 1
        if not voices:
            print("账号下还没有克隆过音色。")
            return 0
        print(f"共 {len(voices)} 个音色：\n")
        for item in voices:
            print(f"  voice_id : {item.get('voice_id')}")
            print(f"  状态     : {item.get('status')}")
            print(f"  创建时间 : {item.get('gmt_create')}")
            print()
        print("把上面的 voice_id 填到 config.json 的 cosyvoice_voice 即可使用。")
        return 0

    # ---- 删除音色 ----
    if args.delete:
        try:
            delete_voice(key, args.delete)
        except Exception as exc:  # noqa: BLE001
            print(f"[失败] 删除音色出错：{exc}", file=sys.stderr)
            return 1
        print(f"已删除音色 {args.delete}")
        return 0

    # ---- 克隆 ----
    if not args.audio and not args.url:
        parser.error("需要指定 --audio / --url / --list / --delete / --check 之一")

    if args.audio:
        if not Path(args.audio).exists():
            print(f"[错误] 找不到文件：{args.audio}", file=sys.stderr)
            return 1
        if not _report_reference(args.audio, args.model) and not args.force:
            print("\n如果确认没问题，加 --force 跳过体检。")
            return 1

    target = args.url or args.audio
    print(f"\n正在创建音色（模型 {args.model}，prefix {args.prefix}）……")

    warnings: list[str] = []
    try:
        voice_id = enroll_voice(
            key,
            target,
            prefix=args.prefix,
            target_model=args.model,
            language_hints=[args.lang] if args.lang else None,
            max_prompt_audio_length=args.max_prompt,
            on_warning=warnings.append,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"\n[失败] {exc}", file=sys.stderr)
        print(
            "\n排查方向：\n"
            "  · API Key 是否是百炼（DashScope）的 Key，且已开通语音合成服务\n"
            "  · 参考音频是否满足时长/采样率/体积要求\n"
            "  · 音色数量是否已达账号配额上限（用 --list 看看，不需要的用 --delete 删掉）\n"
            "  · prefix 是否只含字母数字且 ≤10 字符",
            file=sys.stderr,
        )
        return 1

    for warning in warnings:
        print(f"  [提示] {warning}")

    print("\n克隆成功")
    print(f"  voice_id = {voice_id}")

    configured_model = str(cfg.get("cosyvoice_model", ""))
    if configured_model and configured_model != args.model:
        print(
            f"\n  [警告] 克隆用的是 {args.model}，但 config.json 里的 cosyvoice_model "
            f"是 {configured_model}。\n"
            f"         两者必须一致，否则合成会报「音色不存在」。"
        )

    if args.save:
        cfg.update({"cosyvoice_voice": voice_id, "cosyvoice_model": args.model})
        print(f"\n  已写入 {cfg.path}，重启程序即可用新音色说话。")
    else:
        print("\n  下一步：把它填到 config.json 的 cosyvoice_voice，或重跑并加上 --save")

    return 0


if __name__ == "__main__":
    sys.exit(main())
