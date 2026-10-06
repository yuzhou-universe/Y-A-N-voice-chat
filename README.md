# Y-A-N · 本地语音聊天机器人

一个类微信的桌面 AI 语音聊天客户端：**键盘输入 → Qwen 流式回复（逐字上屏）→ CosyVoice 流式合成 → 边生成边播放**，随时可以打断。

- GUI：CustomTkinter（微信浅色主题：浅灰背景 / 绿色·白色气泡）
- LLM：通义千问 Qwen（OpenAI 兼容接口，`stream=True`）
- TTS：阿里云 CosyVoice（流式合成 + Zero-shot 音色克隆）
- 全链路流式，无阻塞、无"等全部生成完再播放"

> **仓库里没有的东西**：`skills/`（个人人格库）和 `assets/` 下的真人录音/录像
> 都没有随仓库分发。`skills/` 会在你首次运行时自动生成两个示例人格，
> `assets/reference.wav` 是程序合成的电子占位音（不是任何人的声音）——
> clone 下来即可直接跑通整条链路。想用自己的音色请按第五节自行克隆。

---

## 一、项目结构

```
Y-A-N-voice-chat/
├── main.py                  # 入口：python main.py / python main.py --check
├── config_manager.py        # 配置与 API Key 的加密读写
├── skill_manager.py         # 读取 skills/*.md，解析 frontmatter
├── llm_service.py           # Qwen 流式调用 + 上下文管理 + 标点智能截断
├── tts_service.py           # CosyVoice 流式合成（含音色克隆辅助）
├── audio_player.py          # 音频播放队列 + 打断机制
├── chat_controller.py       # 流水线编排（LLM→切句→TTS→播放），额外拆出来的一层
├── main_gui.py              # CustomTkinter 主界面 + 线程安全事件回传
│
├── skills/                  # ← 人格库，丢一个 .md 进来就多一个人格
│   ├── 傲娇女友.md          #    （本目录不入库，首次运行自动生成这两个示例）
│   └── 专业导师.md
├── assets/
│   └── reference.wav        # 程序合成的占位音频，请替换成你自己的 5~15 秒录音
├── tools/
│   └── enroll_voice.py      # 一键克隆音色
├── tests/                   # 三个自检脚本，都不用联网
│   ├── audio_test.py        # 音频回调是否真在消费队列 + 打断
│   ├── input_test.py        # Enter 发送 / Shift+Enter 换行
│   └── smoke_test.py        # 端到端：流式渲染 / 中途打断 / 历史顺序
│
├── config.json              # 运行后生成（含加密后的 Key，已 gitignore）
├── .secret.key              # 加解密密钥，首次运行自动生成
├── config.example.json      # 配置示例
└── requirements.txt
```

> `chat_controller.py` 不在你给的模块清单里，是我额外拆出来的一层。
> 理由：编排逻辑（打断、generation 计数、历史回填）既不属于 UI 也不属于任何一个
> 服务，硬塞进 `main_gui.py` 会让那个文件同时承担"画界面"和"管并发"两个职责，
> 两头都难维护。

---

## 二、安装

```bash
# 建议 Python 3.10 ~ 3.13
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
```

**关于 Python 3.14**：`sounddevice`、`numpy`、`dashscope`、`customtkinter` 在 3.14 上都正常。
只有 `pygame` 没有 3.14 的预编译 wheel（会尝试源码编译并失败）—— 它只是音频兜底后端，
**不装也能完整运行**，所以 `requirements.txt` 里默认把它注释掉了。

先跑一遍自检（不联网、不弹窗）：

```bash
python main.py --check
```

会验证配置读写、人格解析、以及最容易出错的**标点截断逻辑**，并打印切句结果。

还有三个更细的测试脚本（都不联网、不调 API）：

```bash
python tests/audio_test.py    # 音频回调确实在消费 PCM、打断后旧数据被丢弃
python tests/input_test.py    # Enter 发送 / Shift+Enter 换行 / 空白不发送
python tests/smoke_test.py    # 端到端：会真的弹窗，用假 LLM 跑一遍完整流水线
```

`smoke_test.py` 会真开一个窗口、真出声（Mock 电子音），并在中途打断一轮生成，
最后断言：被打断的回复确实被截断且移除了光标、后一轮完整渲染、历史按 `u1,a1,u2,a2` 回填。

---

## 三、配置

启动后点左上角「**⚙ 设置**」：

| 字段 | 说明 |
|---|---|
| Qwen API Key | 阿里云百炼控制台获取，`sk-` 开头 |
| Qwen Base URL | 默认 `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| Qwen 模型 | `qwen-plus` / `qwen-max` / `qwen-turbo` |
| CosyVoice API Key | 与 Qwen 共用同一个 DashScope Key 即可 |
| CosyVoice 模型 | `cosyvoice-v2` |
| 音色 ID | 预置音色如 `longxiaochun_v2`，或克隆得到的 `voice_id` |
| 参考音频路径 | Zero-shot 克隆用，默认 `assets/reference.wav` |
| 语音引擎 | `auto` / `cosyvoice` / `mock` / `none` |
| 音频后端 | `auto` / `sounddevice` / `pygame` |

也可以不用 GUI —— 复制 `config.example.json` 为 `config.json` 手填，或者用环境变量：

```bash
set DASHSCOPE_API_KEY=sk-xxxx        # Windows
export DASHSCOPE_API_KEY=sk-xxxx     # macOS / Linux
```

优先级：**环境变量 / `.env` > `config.json`**。

### 密钥是怎么存的

`config.json` 里的 Key 会被 Fernet 加密成 `enc:gAAAAA...` 的形式，密钥放在同目录的
`.secret.key`。没装 `cryptography` 时自动降级为 Base64 混淆。

说清楚边界：这是**防误传**（防止 Key 被提交进 git、被截图、被云盘同步走），
**不是防本机攻击者** —— 能读 `config.json` 的人同样能读 `.secret.key`。
真要对抗本机攻击者，得换系统凭据库（Windows Credential Manager / macOS Keychain）。

---

## 四、运行

```bash
python main.py
```

- `Enter` 发送，`Shift+Enter` 换行
- 右上角下拉框随时切换人格
- 发送新消息会**立刻打断**上一轮的语音（清空播放队列 + 关闭 TTS 连接）
- 「停止」按钮手动打断；「清空」清空对话与上下文

**没有任何 API Key 也能先跑起来**：此时语音引擎自动降级为 `mock`，
会发出电子音而不是人声，但"流式 → 切句 → 排队 → 打断"的时序行为和真实 TTS 完全一致，
方便你先验证整条链路。

---

## 五、克隆音色 & 使用音色

### 第 0 步：先搞清楚两件事

**① 参考音频的质量决定一切。** 官方要求：

| 项目 | 要求 |
|---|---|
| 时长 | 推荐 **10~20 秒**，至少 3 秒连续朗读，最长 60 秒 |
| 格式 | WAV(16bit) / MP3 / M4A |
| 采样率 | **≥ 24kHz**（cosyvoice-v2；v1 是 16kHz） |
| 体积 | ≤ 10MB |
| 内容 | 单人、正常语速的**平稳陈述句**；无背景音乐、无混响、无环境噪音、无其他人声 |

> `assets/reference.wav` 是**占位音（一段电子嗡鸣，不是人声）**，只用来保证程序能跑起来。
> 你必须把它换成自己的录音，否则克隆出来的音色毫无意义。
> 用手机录一段就行 —— 安静房间里朗读十几秒，导出成 wav/mp3。

**② 克隆用的模型必须和合成用的模型一致。** 用 `cosyvoice-v2` 克隆，
`config.json` 里的 `cosyvoice_model` 就必须也是 `cosyvoice-v2`，
否则合成时会报「音色不存在」。脚本会自动检查并提醒。

### 第 1 步：体检参考音频（不联网、不消耗配额）

```bash
python tools/enroll_voice.py --check assets/reference.wav
```

会检查采样率、位深、时长、体积、格式，有问题会明确指出。例如：

```
参考音频存在 3 个问题：
  · WAV 位深为 8bit，官方要求 16bit
  · 采样率为 16000Hz，低于 cosyvoice-v2 要求的 24000Hz
  · 时长仅 1.0 秒，至少要 3 秒连续朗读
```

不放心的话可以先用 `ffmpeg` 转换：

```bash
ffmpeg -i 我的录音.m4a -ar 24000 -ac 1 -sample_fmt s16 assets/reference.wav
```

### 第 2 步：克隆

```bash
python tools/enroll_voice.py --audio assets/reference.wav --prefix myvoice --save
```

**本地文件不需要公网 URL。** `create_voice` 的 `url` 参数除了 http(s) 之外
也接受 `data:audio/wav;base64,...` 形式的内联数据，脚本会自动帮你把本地文件编码成
Data URI —— 不用上传到 OSS、不用内网穿透、不用图床。

成功后输出：

```
克隆成功
  voice_id = cosyvoice-v2-myvoice-xxxxxxxx
  已写入 config.json，重启程序即可用新音色说话。
```

`--save` 会把 `voice_id` 和对应的 `cosyvoice_model` 一起写回 `config.json`。
不加 `--save` 就自己把 `voice_id` 填到配置的 `cosyvoice_voice` 字段。

### 第 3 步：使用

设置里确认两件事，然后在界面上随便说句话就能听到：

- **音色 ID** = 刚才拿到的 `voice_id`
- **CosyVoice 模型** = 克隆时用的那个模型

> 也可以不克隆，直接用官方预置音色：`longxiaochun_v2`（龙小淳）、
> `longwan_v2`、`longcheng_v2` 等，填进「音色 ID」即可。

### 调声线：让它更活泼（音色不变）

CosyVoice 把「怎么说」和「是谁在说」分成了不同的旋钮。实测数据（同一个克隆音色，同一句话）：

| 设置 | 语速(音节/秒) | 基频均值 | **音色相似度** |
|---|---|---|---|
| 原速 `speech_rate=1.0` | 2.31 | 216.8Hz | 1.000（基准） |
| **`speech_rate=1.20`** | **2.78** | 216.3Hz | **1.000** |
| `speech_rate=1.30` | 3.01 | 217.5Hz | **1.000** |
| `speech_rate=1.20` + `pitch_rate=1.05` | 2.70 | 226.1Hz | 0.992 |
| 只加 `pitch_rate=1.08` | 2.44 | 234.6Hz | 0.983 |

结论：

- **`speech_rate` 是唯一"零代价"的活泼度旋钮** —— 语速上去了，基频和音色指纹纹丝不动。
- **`pitch_rate` 会真的改变音色**（升 8% 音高时相似度掉到 0.983）。想要更年轻的感觉可以
  微调到 1.03~1.05，但别超过 1.06。
- **`instruction`（自然语言风格控制，如"用活泼的语气说"）在 cosyvoice-v2 上不可用** ——
  传了会直接返回 `InvalidParameter`，不是被忽略而是报错。这是实测结论。

三个值都能在「⚙ 设置」里用滑块实时调整，保存即生效，不用重启。
也可以在人格文件里覆盖（见下），让不同人格有不同的说话节奏：

```yaml
---
name: 傲娇女友
speech_rate: 1.25    # 这个角色说话急一点
pitch_rate: 1.03
---
```

想自己量化对比，用这个工具：

```bash
python tools/voice_metrics.py a.wav b.wav c.wav
```

它会输出基频均值/标准差、能量调制、语速、以及相对第一个样本的**音色相似度**。

> 另一条路是**换一段更活泼的参考音频重新克隆** —— 克隆会继承参考音频的情绪基线。
> 但我实测了你那 42 秒录音的各个窗口，最活泼的段落也只比现在高约 6%，
> 不值得为这点差别多占一个音色配额。

### 管理已克隆的音色

音色数量有配额上限，不用了记得删：

```bash
python tools/enroll_voice.py --list                      # 列出全部音色
python tools/enroll_voice.py --delete <voice_id>         # 删掉某个音色
```

### 常见报错

| 报错 | 原因 |
|---|---|
| 音色不存在 / voice not found | 克隆模型和 `cosyvoice_model` 不一致，或音色已被删 |
| 参考音频不合法 | 用 `--check` 查具体哪一项不达标 |
| 音色数量超限 | 用 `--list` 看看，`--delete` 清掉不用的 |
| 未配置 cosyvoice_api_key | 设置里填 DashScope Key（和 Qwen 是同一个） |

> 如果你更习惯图形界面，阿里云百炼控制台里也有音色复刻相关的功能，
> 可以去找找 —— 但我没有实际验证过控制台的当前入口，所以上面这条命令行路径
> 才是本文档确认可用的方式。

---

## 六、实现要点

### 1. 全链路流式 + 标点智能截断

`llm_service.py` 的 `SentenceBuffer` 是整个项目的核心。它解决的问题是：
**GUI 要"每个字立刻显示"，TTS 要"语义完整的句子"**，两者节奏不同，必须有缓冲层拆包。

切分规则：

| 规则 | 说明 |
|---|---|
| 强标点立即切 | `。！？!?；;…` 是最自然的语音停顿点 |
| 弱标点够长才切 | `，、：` 只在缓冲区超过 32 字符时才切，否则"嗯，那个，我觉得"会被切成三段 |
| 硬上限兜底 | 80 字符无条件切 —— 模型列清单/写英文时可能长时间不打标点，没这条兜底 TTS 会一直不出声 |
| 首句优先 | 第一句用 6 字符的低门槛，压低"首包延迟"，让声音尽快响 |
| 代码块不朗读 | 三反引号状态机跟踪，块内文本标记 `speak=False` |
| 收尾符号回填 | `他说："好的。"` 切在 `。` 上时，后面的 `"` 补回上一片段而不是成为孤儿字符 |

流式管道长这样：

```
LLM stream ──┬─→ 每个 delta 直接投递 UI 队列 ──→ 气泡逐字渲染（打字机层）
             │
             └─→ SentenceBuffer.feed(delta) ──→ 完整句子 ──→ CosyVoice streaming_call
                                                                    │
                                                          音频块 ──→ 播放队列
```

### 2. 打断：用 generation 计数器，不用 `task.cancel()`

```python
self._gen += 1                        # 所有旧协程在下一个检查点自行退出
async for delta in self.llm.stream_reply(skill):
    if gen != self._gen:
        break                         # ← 协作式退出
```

不用 `task.cancel()` 的原因：它会在**任意** await 点抛 `CancelledError`，
包括 `finally` 里做清理 await 的时候 —— 那会让历史回填、WebSocket 关闭这些收尾动作
被二次取消，留下状态不一致。

音频侧的打断更直接：每个音频块都带一个 `token`（就是那一刻的 generation），
`interrupt()` 清空队列并把 token 置空。这样即使旧回合还有音频块在网络路上飘着，
`submit()` 也会因 token 不匹配直接丢弃 —— 不会出现"打断了半秒后旧声音又冒出来"。

### 3. 线程安全

```
主线程 (Tk)          pipeline 线程               播放器线程
─────────────        ────────────────────        ──────────────
Tk mainloop          asyncio event loop          PortAudio 回调
    │                        │                          │
    │ run_coroutine_         │                          │
    │ threadsafe ───────────>│                          │
    │                        │ submit(AudioChunk) ─────>│
    │ <──── ui_queue ────────│                          │
    │  after(30ms) 轮询       │                          │
```

规则只有一条：**任何后台线程都不直接碰 Tk 组件**。
后台 → UI 的数据一律走 `queue.Queue`，主线程用 `after(30, self._poll)` 取出后再更新界面。
Tk 不是线程安全的，跨线程碰组件轻则花屏重则崩溃。

DashScope 的音频回调也走同样的原则：SDK 在自己的线程里回调，
我们用 `loop.call_soon_threadsafe` 把数据投递进 asyncio 队列 ——
这是唯一安全的跨线程唤醒方式（直接 `put_nowait` 到 asyncio 队列会丢唤醒）。

### 4. 气泡自动撑开

`CTkLabel` 设了 `wraplength` 后，文字变多会自动换行并请求更大的尺寸，
外层 `pack` 跟着重新布局，不需要手动算高度。`wraplength` 还会随窗口宽度自适应
（窗口变窄时气泡跟着变窄）。

### 5. 打字机层

模型吐字速度是"一阵一阵"的（网络包大小不均），直接显示会一跳一跳。
所以收到的文本先进 `_target`，再由 `tick()` 按积压量自适应地追上去：
积压越多每次补的字越多 —— 看起来像人在打字，又不会落后太多。

---

## 七、常见问题

**Q：没有声音，但文字正常显示。**
先看底部状态栏的提示。若显示"没有可用的音频后端"，说明 `sounddevice` 没找到输出设备：
检查系统默认播放设备，或在设置里把音频后端切到 `pygame`（需先 `pip install pygame`）。

**Q：声音一顿一顿的。**
通常是网络往返太慢。可以把 `SentenceBuffer` 的 `soft_max` 调小（切得更碎、更早出声），
或把 `hard_max` 调小。参数在 `llm_service.py` 的 `SentenceBuffer.__init__`。

**Q：切换人格后，之前的对话还记得。**
这是有意为之 —— 历史保留可以让新人格知道上下文。想彻底重来点「清空」。

**Q：换电脑后 Key 读不出来了。**
`.secret.key` 换了，`config.json` 里的密文解不开。重新在设置里填一次即可
（程序不会崩，只是读出来是空串）。

**Q：代码块会被念出来吗？**
不会。`SentenceBuffer` 用三反引号状态机跟踪代码块，块内内容标记为不朗读，
`clean_for_tts` 还会额外剥掉 Markdown 标记和 URL。

---

## 八、许可证

[MIT](LICENSE) © 2026 yuzhou-universe
