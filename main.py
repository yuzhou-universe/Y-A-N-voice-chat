# -*- coding: utf-8 -*-
"""
main.py —— 程序入口

用法::

    python main.py            # 启动聊天客户端
    python main.py --check    # 不启动 GUI，自检配置 / 人格 / 切句逻辑
"""

from __future__ import annotations

import argparse
import queue
import sys
import traceback


def _setup_console() -> None:
    """让中文在 Windows 控制台里正常显示（默认 cp936 容易出乱码）。"""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:  # noqa: BLE001
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def _fail(message: str, exc: BaseException | None = None) -> int:
    print(f"\n[启动失败] {message}\n", file=sys.stderr)
    if exc is not None:
        traceback.print_exception(type(exc), exc, exc.__traceback__)
    return 1


def run_check() -> int:
    """离线自检：不弹窗、不联网，验证最容易出错的那部分逻辑。"""
    from config_manager import ConfigManager
    from llm_service import SentenceBuffer, clean_for_tts
    from skill_manager import SkillManager

    print("=" * 62)
    print("Y-A-N 自检")
    print("=" * 62)

    # ---- 1. 配置 ----
    cfg = ConfigManager().load()
    print(f"[1/3] 配置加载成功  加密方式："
          f"{'Fernet 加密' if cfg.encryption_enabled else 'Base64 混淆（装 cryptography 可升级）'}")
    print(f"      Qwen Key      : {'已配置' if cfg.get('qwen_api_key') else '未配置'}")
    print(f"      CosyVoice Key : {'已配置' if cfg.get('cosyvoice_api_key') else '未配置'}")
    print(f"      模型          : {cfg.get('qwen_model')} / {cfg.get('cosyvoice_model')}")

    # ---- 2. 人格 ----
    skills = SkillManager.create()
    print(f"\n[2/3] 人格加载成功，共 {len(skills)} 个：")
    for skill in skills:
        print(f"      · {skill.name:<10} {skill.description[:36]}")

    # ---- 3. 标点截断 ----
    print("\n[3/3] 标点截断测试（这是全链路流式的核心逻辑）")
    sample = (
        "嗯，这个嘛，我觉得可以。你要不要试试看？"
        "其实原理很简单：先分词，再判断标点。"
        "```python\nprint('这段代码不会被念出来')\n```"
        "总之就是这样啦！"
    )
    print(f"      输入：{sample}")
    print("      切分结果：")
    buffer = SentenceBuffer()
    chunks = []
    # 模拟 LLM 逐字吐字：一次只有 3 个字符
    for i in range(0, len(sample), 3):
        chunks.extend(buffer.feed(sample[i:i + 3]))
    chunks.extend(buffer.flush())
    for idx, chunk in enumerate(chunks, 1):
        flag = "[读]" if chunk.speak else "[跳过]"
        print(f"        {flag} [{idx:>2}] {chunk.text}")

    assert chunks, "切句失败：没有任何输出"
    assert "".join(c.text for c in chunks if c.speak), "切句失败：没有可朗读内容"
    assert not any("print" in c.text for c in chunks if c.speak), "代码块没有被正确屏蔽"

    print("\n      Markdown 清洗示例：")
    src = "**重点**：看 [文档](https://example.com) 和 `code`"
    print(f"      输入：{src}")
    print(f"      输出：{clean_for_tts(src)}")

    print("\n" + "=" * 62)
    print("自检通过")
    print("=" * 62)
    return 0


def run_app() -> int:
    try:
        import customtkinter as ctk
    except ImportError:
        return _fail("缺少 customtkinter，请先执行：pip install -r requirements.txt")

    from chat_controller import ChatController
    from config_manager import ConfigManager
    from main_gui import ChatApp
    from skill_manager import SkillManager

    config = ConfigManager().load()
    skills = SkillManager.create()

    # 恢复上次用的人格
    wanted = str(config.get("current_skill", "") or "")
    if wanted:
        skills.select(wanted)

    # 后台 -> UI 的事件队列。这是唯一合法的跨线程通道。
    ui_queue: "queue.Queue[tuple]" = queue.Queue()

    controller = ChatController(config, skills, ui_queue)
    controller.start()

    try:
        app = ChatApp(config, skills, controller, ui_queue)
    except Exception as exc:  # noqa: BLE001
        controller.stop()
        return _fail("界面初始化失败", exc)

    ctk.set_appearance_mode("light")
    try:
        app.mainloop()
    finally:
        controller.stop()
    return 0


def main() -> int:
    _setup_console()
    parser = argparse.ArgumentParser(
        prog="yan-voice-chat", description="类微信本地语音聊天机器人"
    )
    parser.add_argument(
        "--check", action="store_true", help="只做离线自检，不启动图形界面"
    )
    args = parser.parse_args()

    if args.check:
        return run_check()
    return run_app()


if __name__ == "__main__":
    sys.exit(main())
