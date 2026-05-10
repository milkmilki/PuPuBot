"""Provider-agnostic helpers for optional voice replies."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

from .storage.db import get_data_dir

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

_DEFAULT_PROVIDER = ""
_KNOWN_AUDIO_FORMATS = {"wav", "ogg", "mp3", "aac", "flac"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return default


@dataclass(frozen=True)
class TTSConfig:
    enabled: bool
    provider: str
    base_url: str
    voice: str
    max_chars: int
    timeout: float
    audio_format: str
    cache_dir: Path
    normalize_audio: bool = False
    ffmpeg_path: str = ""


@dataclass(frozen=True)
class TTSStatus:
    enabled: bool
    provider: str
    ready: bool
    reason: str
    installed_providers: tuple[str, ...]


SynthesizedAudio = bytes | tuple[bytes, str] | Path | None
TTSProvider = Callable[[str, TTSConfig], SynthesizedAudio]

_PROVIDERS: dict[str, TTSProvider] = {}


def register_tts_provider(name: str, provider: TTSProvider) -> None:
    normalized = (name or "").strip().lower()
    if not normalized:
        raise ValueError("provider name cannot be empty")
    _PROVIDERS[normalized] = provider


def unregister_tts_provider(name: str) -> None:
    _PROVIDERS.pop((name or "").strip().lower(), None)


def list_tts_providers() -> tuple[str, ...]:
    return tuple(sorted(_PROVIDERS))


def _normalize_audio_format(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized in _KNOWN_AUDIO_FORMATS:
        return normalized
    return "wav"


def get_tts_config() -> TTSConfig:
    cache_dir = Path(
        os.environ.get(
            "PUPU_TTS_CACHE_DIR",
            str(Path(get_data_dir()) / "tts_cache"),
        )
    )

    return TTSConfig(
        enabled=_env_bool("PUPU_TTS_ENABLED", False),
        provider=os.environ.get("PUPU_TTS_PROVIDER", _DEFAULT_PROVIDER).strip().lower(),
        base_url=os.environ.get("PUPU_TTS_BASE_URL", "").strip().rstrip("/"),
        voice=os.environ.get("PUPU_TTS_VOICE", "").strip(),
        max_chars=max(1, _env_int("PUPU_TTS_MAX_CHARS", 120)),
        timeout=max(1.0, _env_float("PUPU_TTS_TIMEOUT", 60.0)),
        audio_format=_normalize_audio_format(os.environ.get("PUPU_TTS_AUDIO_FORMAT", "wav")),
        cache_dir=cache_dir,
        normalize_audio=_env_bool("PUPU_TTS_NORMALIZE_AUDIO", False),
        ffmpeg_path=os.environ.get("PUPU_TTS_FFMPEG", "").strip().strip('"'),
    )


def get_tts_status(config: TTSConfig | None = None) -> TTSStatus:
    cfg = config or get_tts_config()
    installed = list_tts_providers()
    if not cfg.enabled:
        return TTSStatus(
            enabled=False,
            provider=cfg.provider,
            ready=False,
            reason="disabled",
            installed_providers=installed,
        )
    if not cfg.provider:
        return TTSStatus(
            enabled=True,
            provider="",
            ready=False,
            reason="provider_missing",
            installed_providers=installed,
        )
    if cfg.provider not in _PROVIDERS:
        return TTSStatus(
            enabled=True,
            provider=cfg.provider,
            ready=False,
            reason="provider_unavailable",
            installed_providers=installed,
        )
    return TTSStatus(
        enabled=True,
        provider=cfg.provider,
        ready=True,
        reason="ok",
        installed_providers=installed,
    )


def _normalize_reply_text(text: str) -> str:
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    if not lines:
        return ""

    sentence_endings = set("。！？!?~～…")
    normalized = []
    for line in lines:
        if line[-1] not in sentence_endings:
            line += "。"
        normalized.append(line)
    return "".join(normalized)


def _resolve_ffmpeg(configured: str) -> str | None:
    if configured and Path(configured).exists():
        return configured
    found = shutil.which("ffmpeg")
    if found:
        return found
    return None


def _normalize_audio_file(path: Path, cfg: TTSConfig) -> Path:
    if cfg.audio_format != "wav" or not cfg.normalize_audio:
        return path

    ffmpeg = _resolve_ffmpeg(cfg.ffmpeg_path)
    if not ffmpeg:
        print("[pupu][tts] normalize skipped: ffmpeg not found")
        return path

    normalized = path.with_name(path.stem + ".normalized.wav")
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-af",
        "loudnorm=I=-18:TP=-3:LRA=11",
        "-ac",
        "1",
        "-ar",
        "32000",
        str(normalized),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=30)
        if normalized.exists() and normalized.stat().st_size > 44:
            normalized.replace(path)
    except Exception as exc:
        print(f"[pupu][tts] normalize failed: {exc}")
        try:
            normalized.unlink(missing_ok=True)
        except Exception:
            pass
    return path


def _save_audio_bytes(content: bytes, audio_format: str, cfg: TTSConfig) -> Path | None:
    try:
        cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        suffix = f".{audio_format}"
        path = cfg.cache_dir / f"tts-{int(time.time())}-{uuid.uuid4().hex[:8]}{suffix}"
        path.write_bytes(content)
        path = _normalize_audio_file(path, cfg)
        return path
    except Exception as exc:
        print(f"[pupu][tts] save failed: {exc}")
        return None


def _materialize_audio(result: SynthesizedAudio, cfg: TTSConfig) -> Path | None:
    if result is None:
        return None
    if isinstance(result, Path):
        return result if result.exists() else None
    if isinstance(result, tuple):
        content, audio_format = result
        if not content:
            return None
        return _save_audio_bytes(content, _normalize_audio_format(audio_format), cfg)
    if not result:
        return None
    return _save_audio_bytes(result, cfg.audio_format, cfg)


def synthesize_reply_to_file(text: str, config: TTSConfig | None = None) -> Path | None:
    cfg = config or get_tts_config()
    if not cfg.enabled:
        return None

    reply_text = _normalize_reply_text(text)
    if not reply_text:
        return None
    if len(reply_text) > cfg.max_chars:
        print(f"[pupu][tts] skip: reply too long ({len(reply_text)} > {cfg.max_chars})")
        return None

    status = get_tts_status(cfg)
    if not status.ready:
        if status.reason == "provider_missing":
            print("[pupu][tts] skip: PUPU_TTS_PROVIDER is empty")
        elif status.reason == "provider_unavailable":
            print(f"[pupu][tts] skip: provider not installed: {cfg.provider}")
        return None

    provider = _PROVIDERS[cfg.provider]
    try:
        started = time.perf_counter()
        result = provider(reply_text, cfg)
        elapsed = time.perf_counter() - started
    except Exception as exc:
        print(f"[pupu][tts] provider failed: provider={cfg.provider} error={exc}")
        return None

    path = _materialize_audio(result, cfg)
    if path is None:
        print(f"[pupu][tts] provider returned no audio: provider={cfg.provider}")
        return None
    try:
        size = path.stat().st_size
    except Exception:
        size = -1
    print(
        f"[pupu][tts] generated provider={cfg.provider} file={path.name} bytes={size} time={elapsed:.1f}s"
    )
    return path


__all__ = [
    "TTSConfig",
    "TTSProvider",
    "TTSStatus",
    "get_tts_config",
    "get_tts_status",
    "list_tts_providers",
    "register_tts_provider",
    "synthesize_reply_to_file",
    "unregister_tts_provider",
]
