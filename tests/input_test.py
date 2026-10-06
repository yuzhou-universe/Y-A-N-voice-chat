# -*- coding: utf-8 -*-
"""验证按键绑定（Enter 发送 / Shift+Enter 换行）与滚动容器。

前者是最容易被 Tk 默认行为吃掉的功能，后者用到了 CTk 的私有属性，
都值得单独测一遍。

    python tests/input_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import customtkinter as ctk

from config_manager import ConfigManager
from main_gui import ChatApp

config = ConfigManager().load()
config.set("tts_provider", "mock")

sent = []
skills = None


class StubCtrl:
    """顶掉真 Controller，只记录 submit 了什么。"""

    class _TTS:
        name = "stub"

    tts = _TTS()

    def __init__(self):
        self.skills = None

    def submit(self, text):
        sent.append(text)

    def interrupt(self):
        pass

    def reset_history(self):
        pass

    def set_skill(self, name):
        return self.skills.select(name)

    def apply_config(self):
        pass

    def stop(self):
        pass

    @property
    def is_speaking(self):
        return False


from skill_manager import SkillManager  # noqa: E402

skills = SkillManager.create()
ctrl = StubCtrl()
ctrl.skills = skills
app = ChatApp(config, skills, ctrl, __import__("queue").Queue())

results = []


def run():
    # --- 1. 私有属性是否还在（CTk 升级时最容易断的地方）---
    canvas = getattr(app.chat_frame, "_parent_canvas", None)
    results.append(("CTkScrollableFrame._parent_canvas 存在", canvas is not None))

    # --- 2. Enter 发送 ---
    app.input_box.delete("1.0", "end")
    app.input_box.insert("1.0", "你好")
    app.input_box.focus_set()
    app.input_box.event_generate("<Return>")
    app.update()

    results.append(("Enter 触发了发送", len(sent) == 1 and sent[0] == "你好"))
    results.append((
        "Enter 后输入框被清空",
        app.input_box.get("1.0", "end").strip() == "",
    ))

    # --- 3. Shift+Enter 换行 ---
    app.input_box.delete("1.0", "end")
    app.input_box.insert("1.0", "第一行")
    app.input_box.event_generate("<Return>", state=0x0001)  # Shift 按下
    app.update()
    content = app.input_box.get("1.0", "end")
    results.append(("Shift+Enter 没有触发发送", len(sent) == 1))
    results.append((
        "Shift+Enter 插入了换行",
        content.count("\n") >= 2,   # 文本末尾 Tk 自带一个 \n
    ))

    # --- 4. 空白不发送 ---
    app.input_box.delete("1.0", "end")
    app.input_box.insert("1.0", "   \n  ")
    app.input_box.event_generate("<Return>")
    app.update()
    results.append(("纯空白不发送", len(sent) == 1))

    # --- 5. 清空 ---
    app.on_clear()
    app.update()
    results.append(("清空后只剩问候气泡", len(app._bubbles) == 1))

    # --- 6. 设置对话框能构建出来（滑块/选项/输入框）---
    from main_gui import SettingsDialog

    try:
        dlg = SettingsDialog(app, config)
        app.update()
        results.append(("设置对话框构建成功", True))
        results.append((
            "声线滑块存在（语速/音调/音量）",
            set(dlg._sliders) == {"speech_rate", "pitch_rate", "volume"},
        ))
        results.append(("语速滑块读数取自配置",
                        abs(dlg._sliders["speech_rate"].get() - float(config.get("speech_rate", 1.0))) < 1e-6))
        results.append(("选项菜单存在（引擎/后端）",
                        set(dlg._choices) == {"tts_provider", "audio_backend"}))
        dlg.destroy()
        app.update()
    except Exception as exc:  # noqa: BLE001
        results.append((f"设置对话框构建成功（异常: {exc}）", False))

    ok = all(passed for _, passed in results)
    print("=" * 56)
    for label, passed in results:
        print(f"  [{'✓' if passed else '✗'}] {label}")
    print("=" * 56)
    print("按键绑定测试 " + ("通过" if ok else "失败"))
    app.after(150, app.destroy)


app.after(600, run)
ctk.set_appearance_mode("light")
app.mainloop()
sys.exit(0 if all(p for _, p in results) else 1)
