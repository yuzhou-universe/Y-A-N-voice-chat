# -*- coding: utf-8 -*-
"""端到端冒烟测试：真 GUI + 真流水线 + 真音频，只把 LLM 换成假流。

    python tests/smoke_test.py
"""
import asyncio
import queue
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 控制台编码：Windows 默认 GBK，遇到 ⚙ 这类字符会直接抛 UnicodeEncodeError
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import customtkinter as ctk

from chat_controller import ChatController
from config_manager import ConfigManager
from main_gui import ChatApp
from skill_manager import SkillManager

# A 故意写得很长 + 吐字很慢，保证它一定会在生成到一半时被打断
TEXT_A = (
    "哼，才不是特意为你做的呢。不过既然你都这么说了，那我就勉为其难收下吧！"
    "先说好，这可不是因为我喜欢你。只是看你一个人怪可怜的，"
    "而且这个东西放着也是放着。你可别想多了啊，笨蛋。"
)
TEXT_B = "喂，你怎么突然不说话了？真是的……笨蛋。"

config = ConfigManager().load()
config.set("tts_provider", "mock")     # 离线电子音，不联网
skills = SkillManager.create()
ui_q: queue.Queue = queue.Queue()
ctrl = ChatController(config, skills, ui_q)

state = {"n": 0}


async def fake_stream(skill):
    text = TEXT_A if state["n"] == 0 else TEXT_B
    state["n"] += 1
    for ch in text:
        await asyncio.sleep(0.06)   # 吐字慢一点，方便中途打断
        yield ch


ctrl.llm.stream_reply = fake_stream
ctrl.start()
app = ChatApp(config, skills, ctrl, ui_q)

log = []


def snap(tag):
    rows = []
    for b in app._bubbles:
        try:
            rows.append(("用户" if b.is_user else " AI ", b.label.cget("text")))
        except Exception as exc:  # noqa: BLE001
            rows.append(("??", f"<dead:{exc}>"))
    log.append((tag, rows, ctrl.is_speaking, len(app._typing)))


def t_send_a():
    app.input_box.insert("1.0", "在吗？")
    app.on_send()
    snap("发送 A 之后")


def t_mid_a():
    snap("A 流式中途")


def t_send_b():
    app.input_box.insert("1.0", "喂！")
    app.on_send()
    snap("发送 B（打断 A）之后")


def t_end():
    snap("最终状态")
    print("=" * 72)
    for tag, rows, speaking, typing in log:
        print(f"\n--- {tag}  (speaking={speaking}, typing={typing}) ---")
        for role, text in rows:
            print(f"  [{role}] {text!r}")

    ok = True
    try:
        assert state["n"] == 2, f"LLM 只被调用 {state['n']} 次"
        users = [b for b in app._bubbles if b.is_user]
        ais = [b for b in app._bubbles if not b.is_user]
        assert len(users) == 2, f"用户气泡应为 2 条，实际 {len(users)}"
        assert len(ais) == 3, f"AI 气泡应为 3 条（问候+A+B），实际 {len(ais)}"

        a_text = ais[1].label.cget("text")
        b_text = ais[2].label.cget("text")

        # 1) A 必须被真的打断 —— 只吐出一部分
        assert len(a_text) < len(TEXT_A), (
            f"A 没有被截断（{len(a_text)} vs {len(TEXT_A)}），打断测试无效"
        )
        assert a_text == TEXT_A[: len(a_text)], "A 的文本前缀对不上"
        # 2) 打断后光标必须移除
        assert not a_text.endswith("▌"), "A 被打断后光标没有移除"
        # 3) B 必须完整渲染（不能因为打断 A 而丢字）
        assert b_text == TEXT_B, f"B 不完整:\n  got={b_text!r}\n  exp={TEXT_B!r}"
        # 4) 历史顺序必须是 u1,a1,u2,a2，且 a1 是被截断的那段
        roles = [m["role"] for m in ctrl.llm.history]
        assert roles == ["user", "assistant", "user", "assistant"], f"历史顺序错乱: {roles}"
        assert ctrl.llm.history[1]["content"] == a_text, "a1 回填内容与界面不一致"
        assert ctrl.llm.history[3]["content"] == TEXT_B, "a2 回填内容不对"
    except AssertionError as exc:
        ok = False
        print("\n冒烟测试失败")
        import traceback

        traceback.print_exc()

    if ok:
        print("\n" + "=" * 72)
        print("冒烟测试通过   （流式渲染 / 中途打断 / 历史回填 全部正常）")

    app.after(250, app.destroy)


app.after(400, t_send_a)
app.after(1400, t_mid_a)
app.after(1100, t_send_b)
app.after(5000, t_end)
ctk.set_appearance_mode("light")
app.mainloop()
ctrl.stop()

print("\n历史记录（注意 A 被打断后仍按 u1,a1,u2,a2 顺序回填）：")
for m in ctrl.llm.history:
    print(f"  {m['role']:<9} {m['content'][:44]!r}")
