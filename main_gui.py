# -*- coding: utf-8 -*-
"""
main_gui.py —— 类微信聊天界面（CustomTkinter）

包含三块：
  * ``MessageBubble``  —— 单条消息气泡（头像 + 可自动撑高的气泡）
  * ``SettingsDialog`` —— 左上角「⚙ 设置」弹出的模态对话框
  * ``ChatApp``        —— 主窗口，负责事件绑定与线程安全回传

线程安全铁律
------------
后台线程（LLM / TTS / 音频）**绝不直接操作任何 Tk 组件**。
所有来自后台的数据都投递到 ``self.ui_queue``，由主线程的 ``_poll()``
（每 30ms 一次 ``after()``）取出后再更新界面。
Tk 不是线程安全的，跨线程碰组件轻则花屏重则直接崩溃，这条线不能越。

字幕逐字渲染
------------
模型吐字的速度是"一阵一阵"的（网络包大小不均），直接显示会一跳一跳。
所以这里做了个**打字机层**：收到的文本先进 ``_target``，
再由 ``tick()`` 按积压量自适应地"追"上去 —— 看起来像人在打字，
又不会落后太多（积压越多，每次补的字越多）。
"""

from __future__ import annotations

import queue
from typing import TYPE_CHECKING, Callable

import customtkinter as ctk

if TYPE_CHECKING:
    from chat_controller import ChatController
    from config_manager import ConfigManager
    from skill_manager import SkillManager

# =========================================================================== #
# 配色（对齐微信浅色主题）
# =========================================================================== #
BG_CHAT = "#EDEDED"        # 聊天区背景
BG_HEADER = "#F7F7F7"      # 顶栏
BG_INPUT = "#F7F7F7"       # 输入区
BUBBLE_USER = "#95EC69"    # 自己的气泡（微信绿）
BUBBLE_AI = "#FFFFFF"      # 对方气泡（白）
BUBBLE_SYS = "#DCDCDC"     # 系统提示
TEXT_MAIN = "#111111"
TEXT_SUB = "#8A8A8A"
ACCENT = "#07C160"         # 微信主绿
DANGER = "#FA5151"

FONT_UI = ("Microsoft YaHei UI", 13)
FONT_UI_SM = ("Microsoft YaHei UI", 11)
FONT_BUBBLE = ("Microsoft YaHei UI", 14)
FONT_TITLE = ("Microsoft YaHei UI", 16, "bold")

AVATAR_SIZE = 38
CURSOR = "▌"

#: 输入框绑定的按键 —— 用 state 位判断 Shift，比单独绑 <Shift-Return> 可靠
SHIFT_MASK = 0x0001


# =========================================================================== #
# 一、消息气泡
# =========================================================================== #
class MessageBubble:
    """一条消息（头像 + 气泡 + 文字）。

    气泡高度自动撑开：CTkLabel 设了 ``wraplength`` 之后，文字变多会自动换行、
    请求更高的尺寸，外层 pack 会跟着重新布局 —— 不需要手动算高度。
    """

    def __init__(
        self,
        parent: ctk.CTkScrollableFrame,
        text: str,
        *,
        is_user: bool,
        wraplength: int = 420,
        streaming: bool = False,
        placeholder: str = "",
    ) -> None:
        self.is_user = is_user
        self._streaming = streaming
        self._target = text          # 已收到的完整文本
        self._shown = 0              # 已渲染的字符数（打字机进度）
        self._finished = not streaming

        # ---- 整行容器 ----
        self.row = ctk.CTkFrame(parent, fg_color="transparent")
        self.row.pack(fill="x", padx=12, pady=6)

        # ---- 头像（占位符：纯色圆 + 一个汉字，避免 emoji 在 Tk 上渲染成方块）----
        avatar_text = "我" if is_user else "燕"
        avatar_bg = ACCENT if is_user else "#4A90D9"
        self.avatar = ctk.CTkLabel(
            self.row,
            text=avatar_text,
            width=AVATAR_SIZE,
            height=AVATAR_SIZE,
            corner_radius=AVATAR_SIZE // 2,
            fg_color=avatar_bg,
            text_color="#FFFFFF",
            font=("Microsoft YaHei UI", 14, "bold"),
        )

        # ---- 气泡 ----
        bubble_bg = BUBBLE_USER if is_user else BUBBLE_AI
        text_color = TEXT_MAIN
        self.bubble = ctk.CTkFrame(self.row, fg_color=bubble_bg, corner_radius=10)

        initial = placeholder if (streaming and not text) else text
        self.label = ctk.CTkLabel(
            self.bubble,
            text=initial,
            wraplength=wraplength,
            justify="left",
            anchor="w",
            font=FONT_BUBBLE,
            text_color=text_color,
        )
        self.label.pack(padx=12, pady=9)

        # ---- 左右对齐 ----
        if is_user:
            self.avatar.pack(side="right", anchor="n", padx=(8, 0))
            self.bubble.pack(side="right", anchor="n")
        else:
            self.avatar.pack(side="left", anchor="n", padx=(0, 8))
            self.bubble.pack(side="left", anchor="n")

    # ------------------------------------------------------------------ #
    # 打字机
    # ------------------------------------------------------------------ #
    def append(self, delta: str) -> None:
        """收到新的 token 增量。"""
        self._target += delta

    def tick(self) -> bool:
        """推进打字机。返回 True 表示还在动（主循环据此决定是否继续调度）。"""
        backlog = len(self._target) - self._shown
        if backlog <= 0:
            return False

        # 自适应步长：积压越多补得越快，保证既像打字又不会落后半分钟
        step = max(2, backlog // 5)
        self._shown = min(len(self._target), self._shown + step)
        shown = self._target[: self._shown]
        if self._streaming:
            shown += CURSOR
        self.label.configure(text=shown)
        return True

    def finish(self, full_text: str | None = None) -> None:
        """流结束：一次性把剩下的全部补上，去掉光标。"""
        if full_text is not None:
            self._target = full_text
        self._streaming = False
        self._finished = True
        self._shown = len(self._target)
        self.label.configure(text=self._target or "（没有收到回复）")

    def set_wraplength(self, px: int) -> None:
        self.label.configure(wraplength=px)

    def destroy(self) -> None:
        try:
            self.row.destroy()
        except Exception:  # noqa: BLE001
            pass


# =========================================================================== #
# 二、设置对话框
# =========================================================================== #
class SettingsDialog(ctk.CTkToplevel):
    """模态设置窗口：API Key / 模型 / 音频后端。"""

    #: (配置键, 显示名, 是否密码, 提示文字)
    FIELDS: list[tuple[str, str, bool, str]] = [
        ("qwen_api_key", "Qwen API Key", True, "阿里云百炼控制台获取，sk- 开头"),
        ("qwen_base_url", "Qwen Base URL", False, "兼容模式端点，一般不用改"),
        ("qwen_model", "Qwen 模型", False, "qwen-plus / qwen-max / qwen-turbo"),
        ("cosyvoice_api_key", "CosyVoice API Key", True, "可与 Qwen 共用同一个 DashScope Key"),
        ("cosyvoice_model", "CosyVoice 模型", False, "cosyvoice-v2"),
        ("cosyvoice_voice", "音色 ID", False, "预置音色如 longxiaochun_v2，或克隆得到的 voice_id"),
        ("reference_audio", "参考音频路径", False, "Zero-shot 克隆用，相对项目根目录"),
        ("reference_text", "参考音频文本", False, "选填。填了能提升克隆相似度"),
    ]

    CHOICES: list[tuple[str, str, list[str]]] = [
        ("tts_provider", "语音引擎", ["auto", "cosyvoice", "mock", "none"]),
        ("audio_backend", "音频后端", ["auto", "sounddevice", "pygame"]),
    ]

    #: (配置键, 显示名, 最小, 最大, 步长, 说明)
    SLIDERS: list[tuple[str, str, float, float, float, str]] = [
        (
            "speech_rate",
            "语速",
            0.5,
            2.0,
            0.05,
            "调大 = 更活泼。实测 1.15~1.25 语速明显变快，而音色相似度仍是 1.000（完全没变）",
        ),
        (
            "pitch_rate",
            "音调",
            0.85,
            1.30,
            0.01,
            "调大 = 更年轻活泼。但它会抬高基频（+6% 约等于 +10Hz），音色相似度会掉到 ~0.994，"
            "建议不超过 1.06",
        ),
        ("volume", "音量", 0.0, 1.0, 0.05, "播放音量，不影响音色"),
    ]

    def __init__(self, master: "ChatApp", config: "ConfigManager") -> None:
        super().__init__(master)
        self.cfg = config
        self.on_saved: Callable[[], None] = master.on_settings_saved

        self.title("设置")
        self.geometry("600x680")
        self.resizable(False, True)
        self.configure(fg_color="#FAFAFA")
        self.transient(master)

        self._entries: dict[str, ctk.CTkEntry] = {}
        self._choices: dict[str, ctk.StringVar] = {}
        self._sliders: dict[str, ctk.DoubleVar] = {}
        self._build()

        # 模态：阻止操作主窗口
        self.after(60, self._grab)
        self.bind("<Escape>", lambda _e: self.destroy())

    def _grab(self) -> None:
        try:
            self.grab_set()
            self.focus_force()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        head = ctk.CTkLabel(
            self, text="API 与语音设置", font=FONT_TITLE, text_color=TEXT_MAIN
        )
        head.pack(anchor="w", padx=24, pady=(20, 2))

        hint = ctk.CTkLabel(
            self,
            text="密钥会加密保存在项目目录的 config.json 中（密钥文件为 .secret.key）。",
            font=FONT_UI_SM,
            text_color=TEXT_SUB,
            justify="left",
            wraplength=540,
        )
        hint.pack(anchor="w", padx=24, pady=(0, 12))

        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=14)

        for key, label, secret, tip in self.FIELDS:
            self._add_entry(body, key, label, secret, tip)

        for key, label, options in self.CHOICES:
            self._add_choice(body, key, label, options)

        ctk.CTkLabel(
            body,
            text="声线微调（只改「怎么说」，不改「是谁在说」）",
            font=FONT_UI,
            text_color=TEXT_MAIN,
        ).pack(anchor="w", padx=10, pady=(16, 0))
        for key, label, lo, hi, step, tip in self.SLIDERS:
            self._add_slider(body, key, label, lo, hi, step, tip)

        # ---- 底部按钮 ----
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=24, pady=16)

        ctk.CTkButton(
            footer,
            text="取消",
            width=90,
            fg_color="#E5E5E5",
            hover_color="#D8D8D8",
            text_color=TEXT_MAIN,
            command=self.destroy,
        ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            footer,
            text="保存",
            width=110,
            fg_color=ACCENT,
            hover_color="#06AD56",
            command=self._save,
        ).pack(side="right")

    def _add_entry(
        self, parent, key: str, label: str, secret: bool, tip: str
    ) -> None:
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.pack(fill="x", pady=(6, 2), padx=10)

        ctk.CTkLabel(wrap, text=label, font=FONT_UI, text_color=TEXT_MAIN).pack(anchor="w")

        line = ctk.CTkFrame(wrap, fg_color="transparent")
        line.pack(fill="x", pady=(3, 0))

        entry = ctk.CTkEntry(
            line,
            font=FONT_UI,
            height=34,
            show="●" if secret else "",
        )
        entry.insert(0, str(self.cfg.get(key, "") or ""))
        entry.pack(side="left", fill="x", expand=True)
        self._entries[key] = entry

        if secret:
            def toggle(e=entry) -> None:
                e.configure(show="" if e.cget("show") else "●")

            ctk.CTkButton(
                line,
                text="显示",
                width=52,
                height=34,
                fg_color="#E5E5E5",
                hover_color="#D8D8D8",
                text_color=TEXT_MAIN,
                font=FONT_UI_SM,
                command=toggle,
            ).pack(side="left", padx=(6, 0))

        if tip:
            ctk.CTkLabel(
                wrap, text=tip, font=FONT_UI_SM, text_color=TEXT_SUB, justify="left"
            ).pack(anchor="w", pady=(2, 0))

    def _add_choice(self, parent, key: str, label: str, options: list[str]) -> None:
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.pack(fill="x", pady=(10, 2), padx=10)

        ctk.CTkLabel(wrap, text=label, font=FONT_UI, text_color=TEXT_MAIN).pack(anchor="w")

        var = ctk.StringVar(value=str(self.cfg.get(key, options[0]) or options[0]))
        self._choices[key] = var
        ctk.CTkOptionMenu(
            wrap,
            values=options,
            variable=var,
            width=180,
            height=32,
            fg_color="#FFFFFF",
            button_color="#E5E5E5",
            button_hover_color="#D8D8D8",
            text_color=TEXT_MAIN,
            dropdown_fg_color="#FFFFFF",
            dropdown_text_color=TEXT_MAIN,
            font=FONT_UI,
        ).pack(anchor="w", pady=(3, 0))

    def _add_slider(
        self, parent, key: str, label: str, lo: float, hi: float, step: float, tip: str
    ) -> None:
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.pack(fill="x", pady=(8, 2), padx=10)

        head = ctk.CTkFrame(wrap, fg_color="transparent")
        head.pack(fill="x")
        ctk.CTkLabel(head, text=label, font=FONT_UI, text_color=TEXT_MAIN).pack(side="left")

        try:
            current = float(self.cfg.get(key, 1.0))
        except (TypeError, ValueError):
            current = 1.0
        current = max(lo, min(hi, current))

        readout = ctk.CTkLabel(
            head, text=f"{current:.2f}", font=FONT_UI_SM, text_color=ACCENT, width=44
        )
        readout.pack(side="right")

        var = ctk.DoubleVar(value=current)
        ctk.CTkSlider(
            wrap,
            from_=lo,
            to=hi,
            number_of_steps=int(round((hi - lo) / step)),
            variable=var,
            progress_color=ACCENT,
            button_color=ACCENT,
            button_hover_color="#06AD56",
            command=lambda v, r=readout: r.configure(text=f"{float(v):.2f}"),
        ).pack(fill="x", pady=(3, 0))

        self._sliders[key] = var
        if tip:
            ctk.CTkLabel(
                wrap, text=tip, font=FONT_UI_SM, text_color=TEXT_SUB,
                justify="left", wraplength=520,
            ).pack(anchor="w", pady=(2, 0))

    # ------------------------------------------------------------------ #
    def _save(self) -> None:
        values: dict[str, object] = {}
        for key, entry in self._entries.items():
            values[key] = entry.get().strip()
        for key, var in self._choices.items():
            values[key] = var.get()
        for key, var in self._sliders.items():
            values[key] = float(var.get())

        # TTS 引擎被关掉时同步关掉总开关，避免"设了 none 但还是想出声"的困惑
        values["tts_enabled"] = values.get("tts_provider") not in ("none",)
        values["qwen_api_key"] = str(values.get("qwen_api_key", "")).strip()

        self.cfg.update(values)          # 自动加密落盘
        try:
            self.on_saved()
        finally:
            self.destroy()


# =========================================================================== #
# 三、主窗口
# =========================================================================== #
class ChatApp(ctk.CTk):
    """类微信聊天主界面。"""

    POLL_MS = 30        # UI 队列轮询间隔
    TICK_MS = 40        # 打字机刷新间隔

    def __init__(
        self,
        config: "ConfigManager",
        skills: "SkillManager",
        controller: "ChatController",
        ui_queue: "queue.Queue[tuple]",
    ) -> None:
        super().__init__()
        self.cfg = config
        self.skills = skills
        self.ctrl = controller
        self.ui_queue = ui_queue

        self._bubbles: list[MessageBubble] = []
        self._by_id: dict[int, MessageBubble] = {}
        self._typing: set[MessageBubble] = set()
        self._last_wrap = 0
        self._speaking = False

        ctk.set_appearance_mode("light")
        self.title("Y-A-N · 语音聊天")
        self.geometry("900x720")
        self.minsize(680, 520)
        self.configure(fg_color=BG_CHAT)

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self._build_header()
        self._build_chat_area()
        self._build_composer()
        self._build_statusbar()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Configure>", self._on_resize)

        self._greeting()
        self.after(self.POLL_MS, self._poll)
        self.after(self.TICK_MS, self._tick_typewriter)

    # ------------------------------------------------------------------ #
    # 顶栏
    # ------------------------------------------------------------------ #
    def _build_header(self) -> None:
        bar = ctk.CTkFrame(self, height=58, corner_radius=0, fg_color=BG_HEADER)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_propagate(False)
        bar.grid_columnconfigure(2, weight=1)

        ctk.CTkButton(
            bar,
            text="⚙ 设置",
            width=84,
            height=34,
            corner_radius=8,
            fg_color="transparent",
            hover_color="#E4E4E4",
            text_color=TEXT_MAIN,
            font=FONT_UI,
            command=self.open_settings,
        ).grid(row=0, column=0, padx=(14, 6), pady=12)

        # 状态圆点：正在说话时变绿
        self.dot = ctk.CTkLabel(
            bar, text="", width=10, height=10, corner_radius=5, fg_color="#C8C8C8"
        )
        self.dot.grid(row=0, column=1, padx=(2, 8))

        title_box = ctk.CTkFrame(bar, fg_color="transparent")
        title_box.grid(row=0, column=2, sticky="w")
        ctk.CTkLabel(title_box, text="Y-A-N", font=FONT_TITLE, text_color=TEXT_MAIN).pack(
            anchor="w"
        )
        self.subtitle = ctk.CTkLabel(
            title_box, text="", font=FONT_UI_SM, text_color=TEXT_SUB
        )
        self.subtitle.pack(anchor="w")

        # 人格切换
        ctk.CTkLabel(bar, text="人格", font=FONT_UI_SM, text_color=TEXT_SUB).grid(
            row=0, column=3, padx=(0, 6)
        )
        names = self.skills.names or ["通用助手"]
        current = self.skills.current().name
        self.skill_menu = ctk.CTkOptionMenu(
            bar,
            values=names,
            width=150,
            height=34,
            corner_radius=8,
            fg_color="#FFFFFF",
            button_color="#E4E4E4",
            button_hover_color="#D8D8D8",
            text_color=TEXT_MAIN,
            dropdown_fg_color="#FFFFFF",
            dropdown_text_color=TEXT_MAIN,
            dropdown_hover_color="#EFEFEF",
            font=FONT_UI,
            command=self.on_skill_change,
        )
        self.skill_menu.set(current)
        self.skill_menu.grid(row=0, column=4, padx=6)

        ctk.CTkButton(
            bar,
            text="清空",
            width=64,
            height=34,
            corner_radius=8,
            fg_color="transparent",
            hover_color="#E4E4E4",
            text_color=TEXT_SUB,
            font=FONT_UI,
            command=self.on_clear,
        ).grid(row=0, column=5, padx=(6, 14))

        self._refresh_subtitle()

    # ------------------------------------------------------------------ #
    # 聊天区
    # ------------------------------------------------------------------ #
    def _build_chat_area(self) -> None:
        self.chat_frame = ctk.CTkScrollableFrame(self, fg_color=BG_CHAT, corner_radius=0)
        self.chat_frame.grid(row=1, column=0, sticky="nsew")

    # ------------------------------------------------------------------ #
    # 输入区
    # ------------------------------------------------------------------ #
    def _build_composer(self) -> None:
        wrap = ctk.CTkFrame(self, corner_radius=0, fg_color=BG_INPUT)
        wrap.grid(row=2, column=0, sticky="ew")
        wrap.grid_columnconfigure(0, weight=1)

        self.input_box = ctk.CTkTextbox(
            wrap,
            height=76,
            corner_radius=10,
            fg_color="#FFFFFF",
            text_color=TEXT_MAIN,
            font=FONT_BUBBLE,
            border_width=1,
            border_color="#E0E0E0",
            wrap="word",
        )
        self.input_box.grid(row=0, column=0, sticky="ew", padx=(14, 10), pady=12)

        # Enter 发送 / Shift+Enter 换行。
        # Tk 的事件绑定里 <Return> 会先于 Text 的类绑定执行，
        # 返回 "break" 就能阻止默认的"插入换行"行为。
        self.input_box.bind("<Return>", self._on_return_key)

        side = ctk.CTkFrame(wrap, fg_color="transparent")
        side.grid(row=0, column=1, padx=(0, 14), pady=12, sticky="ns")

        self.send_btn = ctk.CTkButton(
            side,
            text="发送",
            width=88,
            height=38,
            corner_radius=8,
            fg_color=ACCENT,
            hover_color="#06AD56",
            font=FONT_UI,
            command=self.on_send,
        )
        self.send_btn.pack(side="top")

        self.stop_btn = ctk.CTkButton(
            side,
            text="停止",
            width=88,
            height=26,
            corner_radius=8,
            fg_color="transparent",
            hover_color="#E4E4E4",
            text_color=DANGER,
            font=FONT_UI_SM,
            command=self.on_stop,
        )
        self.stop_btn.pack(side="top", pady=(4, 0))

    # ------------------------------------------------------------------ #
    # 状态栏
    # ------------------------------------------------------------------ #
    def _build_statusbar(self) -> None:
        bar = ctk.CTkFrame(self, height=26, corner_radius=0, fg_color=BG_HEADER)
        bar.grid(row=3, column=0, sticky="ew")
        bar.grid_propagate(False)
        self.status_label = ctk.CTkLabel(
            bar, text="就绪", font=FONT_UI_SM, text_color=TEXT_SUB, anchor="w"
        )
        self.status_label.pack(side="left", padx=14)

    # ================================================================== #
    # 事件
    # ================================================================== #
    def _on_return_key(self, event) -> str | None:
        """Enter 发送，Shift+Enter 换行。"""
        if event.state & SHIFT_MASK:
            return None          # 放行给 Tk 默认行为 -> 插入换行
        self.on_send()
        return "break"           # 阻止插入换行

    def on_send(self) -> None:
        text = self.input_box.get("1.0", "end").strip()
        if not text:
            return
        self.input_box.delete("1.0", "end")

        self._add_bubble(text, is_user=True)
        self._scroll_to_bottom()

        self.ctrl.submit(text)   # 跨线程投递（内部走 run_coroutine_threadsafe）

    def on_stop(self) -> None:
        self.ctrl.interrupt()
        self._set_status("已打断")

    def on_clear(self) -> None:
        self.ctrl.interrupt()
        self.ctrl.reset_history()
        for bubble in self._bubbles:
            bubble.destroy()
        self._bubbles.clear()
        self._by_id.clear()
        self._typing.clear()
        self._greeting()
        self._set_status("对话已清空")

    def on_skill_change(self, name: str) -> None:
        skill = self.ctrl.set_skill(name)
        self.cfg.update({"current_skill": skill.name})
        self._refresh_subtitle()
        self._set_status(f"已切换人格：{skill.name}")

    def on_settings_saved(self) -> None:
        """设置对话框点了保存。"""
        self.ctrl.apply_config()
        self._set_status("设置已保存并生效")
        # 引擎重建是在后台线程做的，稍等一下再读名字，否则拿到的是旧的
        self.after(600, self._refresh_subtitle)

    def open_settings(self) -> None:
        SettingsDialog(self, self.cfg)

    def on_close(self) -> None:
        try:
            self.ctrl.stop()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()

    # ================================================================== #
    # 线程安全：轮询后台事件
    # ================================================================== #
    def _poll(self) -> None:
        """主线程专属：把后台线程投递的事件取出来更新 UI。"""
        try:
            while True:
                event = self.ui_queue.get_nowait()
                self._dispatch(event)
        except queue.Empty:
            pass
        except Exception as exc:  # noqa: BLE001 - 单个事件出错不该让轮询停摆
            print(f"[gui] 处理事件失败: {exc}")

        self._update_speaking()
        self.after(self.POLL_MS, self._poll)

    def _dispatch(self, event: tuple) -> None:
        kind = event[0]

        if kind == "ai_start":
            msg_id = event[1]
            bubble = self._add_bubble(
                "", is_user=False, streaming=True, placeholder="正在思考…"
            )
            self._by_id[msg_id] = bubble
            self._typing.add(bubble)
            self._scroll_to_bottom()

        elif kind == "ai_delta":
            bubble = self._by_id.get(event[1])
            if bubble is not None:
                bubble.append(event[2])

        elif kind == "ai_end":
            bubble = self._by_id.pop(event[1], None)
            if bubble is not None:
                bubble.finish(event[2])
                self._typing.discard(bubble)
            self._scroll_to_bottom()

        elif kind == "error":
            # 前缀用纯中文标点：⚠ 这类符号不在 YaHei 字体里，render 出来可能是方块
            self._add_bubble(f"（出错了）{event[1]}", is_user=False)
            self._scroll_to_bottom()

        elif kind == "status":
            self._set_status(str(event[1]))

    # ================================================================== #
    # 打字机循环
    # ================================================================== #
    def _tick_typewriter(self) -> None:
        grew = False
        for bubble in list(self._typing):
            if bubble.tick():
                grew = True
            else:
                self._typing.discard(bubble)
        if grew:
            self._scroll_to_bottom()
        self.after(self.TICK_MS, self._tick_typewriter)

    # ================================================================== #
    # 界面辅助
    # ================================================================== #
    def _wrap_px(self) -> int:
        """气泡换行宽度：随窗口大小自适应。"""
        try:
            width = self.winfo_width()
        except Exception:  # noqa: BLE001
            width = 900
        if width < 200:          # 窗口还没真正显示出来
            width = 900
        return max(200, min(560, int(width * 0.62)))

    def _on_resize(self, event) -> None:
        if event.widget is not self:
            return
        px = self._wrap_px()
        if abs(px - self._last_wrap) < 16:
            return
        self._last_wrap = px
        for bubble in self._bubbles:
            bubble.set_wraplength(px)

    def _scroll_to_bottom(self) -> None:
        """滚动到底。CTkScrollableFrame 不会在子控件变高时自动滚，得手动来。"""
        def do() -> None:
            canvas = getattr(self.chat_frame, "_parent_canvas", None)
            if canvas is None:
                return
            try:
                canvas.update_idletasks()
                box = canvas.bbox("all")
                if box:
                    canvas.configure(scrollregion=box)
                canvas.yview_moveto(1.0)
            except Exception:  # noqa: BLE001
                pass

        self.after_idle(do)

    def _add_bubble(
        self,
        text: str,
        *,
        is_user: bool,
        streaming: bool = False,
        placeholder: str = "",
    ) -> MessageBubble:
        """创建气泡并登记到列表（列表用于窗口缩放时统一调整换行宽度）。"""
        bubble = MessageBubble(
            self.chat_frame,
            text,
            is_user=is_user,
            wraplength=self._wrap_px(),
            streaming=streaming,
            placeholder=placeholder,
        )
        self._bubbles.append(bubble)
        return bubble

    def _set_status(self, text: str) -> None:
        try:
            self.status_label.configure(text=text)
        except Exception:  # noqa: BLE001
            pass

    def _refresh_subtitle(self) -> None:
        skill = self.skills.current()
        desc = skill.description or "本地人格"
        self.subtitle.configure(text=f"{desc} · {self.ctrl.tts.name}")

    def _update_speaking(self) -> None:
        speaking = self.ctrl.is_speaking
        if speaking != self._speaking:
            self._speaking = speaking
            try:
                self.dot.configure(fg_color=ACCENT if speaking else "#C8C8C8")
            except Exception:  # noqa: BLE001
                pass

    def _greeting(self) -> None:
        skill = self.skills.current()
        hint = "Enter 发送 · Shift+Enter 换行"
        if not str(self.cfg.get("qwen_api_key", "") or "").strip():
            hint = "还没配置 API Key，点左上角「⚙ 设置」填入后即可开聊"
        self._add_bubble(f"我是「{skill.name}」。{hint}", is_user=False)
        self._scroll_to_bottom()


__all__ = ["ChatApp", "MessageBubble", "SettingsDialog"]
