# -*- coding: utf-8 -*-
"""
config_manager.py —— 配置与 API Key 管理

职责：
  1. 读写项目根目录下的 ``config.json``；
  2. 对 API Key 做**本地加密**存储（优先 cryptography.Fernet，缺失时降级为 Base64 混淆）；
  3. 支持 ``.env`` 文件 / 系统环境变量覆盖配置（方便开发时不必把 Key 写进 json）。

关于加密（请务必了解其边界）：
  Fernet 的密钥保存在同目录的 ``.secret.key`` 里。这是"本地加密"，目的是防止
  明文 Key 被误提交进 git、被截图、被云盘自动同步走 —— **不是**用来对抗已经能
  读取你本机文件系统的攻击者（他能读 config.json，就能读 .secret.key）。
  真要对抗本机攻击者，请改用系统凭据库（Windows Credential Manager / macOS Keychain）。
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# 路径常量
# --------------------------------------------------------------------------- #
APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
SECRET_KEY_PATH = APP_DIR / ".secret.key"
ENV_PATH = APP_DIR / ".env"

# 加密前缀：用于区分"密文"和用户手写的"明文"
_ENC_PREFIX = "enc:"

try:  # pragma: no cover - 取决于环境是否装了 cryptography
    from cryptography.fernet import Fernet, InvalidToken

    _HAS_FERNET = True
except Exception:  # noqa: BLE001
    Fernet = None  # type: ignore[assignment]

    class InvalidToken(Exception):  # type: ignore[no-redef]
        """cryptography 缺失时的占位异常。"""

    _HAS_FERNET = False


# --------------------------------------------------------------------------- #
# 默认配置
# --------------------------------------------------------------------------- #
DEFAULTS: dict[str, Any] = {
    # ---------------- LLM：通义千问 ----------------
    "qwen_api_key": "",
    "qwen_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "qwen_model": "qwen-plus",
    "qwen_temperature": 0.85,
    "qwen_max_tokens": 1024,
    "history_turns": 12,          # 送入模型的最近 N 轮对话

    # ---------------- TTS：CosyVoice ----------------
    "tts_provider": "auto",       # auto | cosyvoice | mock | none
    "tts_enabled": True,
    "cosyvoice_api_key": "",
    "cosyvoice_model": "cosyvoice-v2",
    "cosyvoice_voice": "longxiaochun_v2",   # 预置音色 或 create_voice() 返回的克隆音色 ID
    "cosyvoice_reference_url": "",          # 若已克隆，可把 voice_id 填到上一项，这里可留空
    "reference_audio": "assets/reference.wav",   # Zero-shot 克隆用的参考音频（本地路径）
    "reference_text": "",                        # 仅本地 CosyVoice 模型用；云端 create_voice 无此参数
    # 声线微调：这两个只改"怎么说"，不改"是谁在说"
    "speech_rate": 1.0,      # 语速。1.15~1.25 明显更活泼，音色完全不变
    "pitch_rate": 1.0,       # 音调。>1 更年轻活泼，但会轻微改动音色（建议 ≤1.06）

    # ---------------- 音频播放 ----------------
    "audio_backend": "auto",      # auto | sounddevice | pygame
    "sample_rate": 22050,         # CosyVoice PCM_22050HZ_MONO_16BIT
    "volume": 0.9,

    # ---------------- 界面 ----------------
    "current_skill": "",          # 空 = 使用 skills/ 下第一个
    "theme": "light",
}

# 需要加密落盘的字段
SECRET_FIELDS: tuple[str, ...] = ("qwen_api_key", "cosyvoice_api_key")

# 环境变量覆盖表： 环境变量名 -> 配置键
ENV_OVERRIDES: dict[str, str] = {
    "QWEN_API_KEY": "qwen_api_key",
    "DASHSCOPE_API_KEY": "qwen_api_key",
    "QWEN_BASE_URL": "qwen_base_url",
    "QWEN_MODEL": "qwen_model",
    "COSYVOICE_API_KEY": "cosyvoice_api_key",
    "COSYVOICE_MODEL": "cosyvoice_model",
    "COSYVOICE_VOICE": "cosyvoice_voice",
    "REFERENCE_AUDIO": "reference_audio",
    "REFERENCE_TEXT": "reference_text",
    "TTS_PROVIDER": "tts_provider",
    "AUDIO_BACKEND": "audio_backend",
}


def _parse_dotenv(path: Path) -> dict[str, str]:
    """极简 .env 解析器（避免为了一个功能引入 python-dotenv）。

    支持 ``KEY=VALUE``、``# 注释``、以及值两侧的单/双引号。
    """
    result: dict[str, str] = {}
    if not path.exists():
        return result
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                result[key] = value
    except OSError:
        pass
    return result


class ConfigManager:
    """线程安全的配置管理器。

    典型用法::

        cfg = ConfigManager().load()
        cfg.get("qwen_api_key")
        cfg.update({"qwen_api_key": "sk-xxx"})   # 自动加密落盘
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else CONFIG_PATH
        self._lock = threading.RLock()
        self._data: dict[str, Any] = dict(DEFAULTS)
        self._fernet: Any = None
        self._init_cipher()

    # ------------------------------------------------------------------ #
    # 加密
    # ------------------------------------------------------------------ #
    def _init_cipher(self) -> None:
        """初始化 Fernet。密钥不存在则生成一个（仅当前用户可读）。"""
        if not _HAS_FERNET:
            return
        try:
            if SECRET_KEY_PATH.exists():
                key = SECRET_KEY_PATH.read_bytes().strip()
            else:
                key = Fernet.generate_key()
                SECRET_KEY_PATH.write_bytes(key)
                try:  # Windows 上 chmod 基本无效，但在 POSIX 上能收紧权限
                    os.chmod(SECRET_KEY_PATH, 0o600)
                except OSError:
                    pass
            self._fernet = Fernet(key)
        except Exception:  # noqa: BLE001 - 密钥损坏时不应该让程序起不来
            self._fernet = None

    def _encrypt(self, plain: str) -> str:
        if not plain:
            return ""
        if self._fernet is None:
            # 降级方案：Base64 混淆（不是加密，仅防止肉眼直接看到 / 防 grep）
            return _ENC_PREFIX + "b64:" + base64.b64encode(plain.encode("utf-8")).decode("ascii")
        token = self._fernet.encrypt(plain.encode("utf-8")).decode("ascii")
        return _ENC_PREFIX + token

    def _decrypt(self, stored: str) -> str:
        if not stored or not isinstance(stored, str):
            return ""
        if not stored.startswith(_ENC_PREFIX):
            return stored  # 用户手写的明文，直接放行
        payload = stored[len(_ENC_PREFIX):]
        if payload.startswith("b64:"):
            try:
                return base64.b64decode(payload[4:]).decode("utf-8")
            except Exception:  # noqa: BLE001
                return ""
        if self._fernet is None:
            return ""
        try:
            return self._fernet.decrypt(payload.encode("ascii")).decode("utf-8")
        except InvalidToken:
            # 换过机器 / 删过 .secret.key —— 让用户重新填一次即可
            return ""
        except Exception:  # noqa: BLE001
            return ""

    @property
    def encryption_enabled(self) -> bool:
        """True 表示使用了真正的加密，False 表示只是 Base64 混淆。"""
        return self._fernet is not None

    # ------------------------------------------------------------------ #
    # 读写
    # ------------------------------------------------------------------ #
    def load(self) -> "ConfigManager":
        """读取 config.json + .env + 环境变量，返回自身以便链式调用。"""
        with self._lock:
            data: dict[str, Any] = dict(DEFAULTS)

            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        data.update(raw)
                except (OSError, json.JSONDecodeError) as exc:
                    print(f"[config] config.json 解析失败，使用默认值：{exc}")

            # 解密敏感字段
            for field in SECRET_FIELDS:
                data[field] = self._decrypt(data.get(field, ""))

            # .env 覆盖
            dotenv = _parse_dotenv(ENV_PATH)
            for env_name, cfg_key in ENV_OVERRIDES.items():
                value = os.environ.get(env_name) or dotenv.get(env_name)
                if value:
                    data[cfg_key] = self._coerce(cfg_key, value)

            self._data = data
            return self

    def save(self) -> None:
        """原子写入 config.json（敏感字段加密）。"""
        with self._lock:
            payload: dict[str, Any] = {}
            for key, value in self._data.items():
                if key in SECRET_FIELDS:
                    payload[key] = self._encrypt(str(value or ""))
                else:
                    payload[key] = value

            text = json.dumps(payload, ensure_ascii=False, indent=2)
            # 原子写：避免写一半崩溃导致配置损坏
            tmp_fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                os.replace(tmp_name, self.path)
            except Exception:  # noqa: BLE001
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise

    # ------------------------------------------------------------------ #
    # 访问器
    # ------------------------------------------------------------------ #
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            if key in self._data:
                return self._data[key]
            return DEFAULTS.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def update(self, values: dict[str, Any], *, persist: bool = True) -> None:
        with self._lock:
            self._data.update(values)
            if persist:
                self.save()

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def refresh(self) -> "ConfigManager":
        """外部改过 config.json 后重新加载。"""
        return self.load()

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _coerce(key: str, value: str) -> Any:
        """把 .env 里的字符串转成和默认值同类型的对象。"""
        default = DEFAULTS.get(key)
        if isinstance(default, bool):
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        if isinstance(default, int):
            try:
                return int(float(value))
            except ValueError:
                return default
        if isinstance(default, float):
            try:
                return float(value)
            except ValueError:
                return default
        return value

    def resolve_path(self, key: str) -> Path:
        """把配置里的相对路径解析成绝对路径（相对于项目根目录）。"""
        raw = str(self.get(key, "") or "")
        path = Path(raw)
        return path if path.is_absolute() else (APP_DIR / path)


__all__ = ["ConfigManager", "DEFAULTS", "SECRET_FIELDS", "APP_DIR", "CONFIG_PATH"]
