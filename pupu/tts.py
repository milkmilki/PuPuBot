"""GPT-SoVITS HTTP client helpers for optional voice replies."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv

from .storage.db import get_data_dir

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")


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
    base_url: str
    ref_audio: str
    prompt_text: str
    prompt_lang: str
    text_lang: str
    max_chars: int
    timeout: float
    media_type: str
    cache_dir: Path
    text_split_method: str
    normalize_audio: bool = False
    ffmpeg_path: str = ""
    top_k: int = 15
    top_p: float = 1.0
    temperature: float = 1.0
    repetition_penalty: float = 1.35
    speed_factor: float = 1.0
    seed: int = -1
    parallel_infer: bool = True
    sample_steps: int = 32
    super_sampling: bool = False


def _read_prompt_text(ref_audio: str) -> str:
    inline = os.environ.get("PUPU_TTS_PROMPT_TEXT", "").strip()
    if inline:
        return inline

    configured = os.environ.get("PUPU_TTS_PROMPT_TEXT_FILE", "").strip().strip('"')
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    if ref_audio:
        candidates.append(Path(ref_audio).with_suffix(".txt"))

    for path in candidates:
        try:
            if path.exists():
                return path.read_text(encoding="utf-8-sig").strip().lstrip("\ufeff")
        except Exception as exc:
            print(f"[pupu][tts] failed to read prompt text {path}: {exc}")
    return ""


def get_tts_config() -> TTSConfig:
    ref_audio = os.environ.get("PUPU_TTS_REF_AUDIO", "").strip().strip('"')
    media_type = os.environ.get("PUPU_TTS_MEDIA_TYPE", "wav").strip().lower() or "wav"
    if media_type not in {"wav", "raw", "ogg", "aac"}:
        media_type = "wav"

    cache_dir = Path(
        os.environ.get(
            "PUPU_TTS_CACHE_DIR",
            str(Path(get_data_dir()) / "tts_cache"),
        )
    )

    return TTSConfig(
        enabled=_env_bool("PUPU_TTS_ENABLED", False),
        base_url=os.environ.get("PUPU_TTS_BASE_URL", "http://127.0.0.1:9880").strip().rstrip("/"),
        ref_audio=ref_audio,
        prompt_text=_read_prompt_text(ref_audio),
        prompt_lang=os.environ.get("PUPU_TTS_PROMPT_LANG", "ja").strip().lower() or "ja",
        text_lang=os.environ.get("PUPU_TTS_TEXT_LANG", "zh").strip().lower() or "zh",
        max_chars=max(1, _env_int("PUPU_TTS_MAX_CHARS", 120)),
        timeout=max(1.0, _env_float("PUPU_TTS_TIMEOUT", 60.0)),
        media_type=media_type,
        cache_dir=cache_dir,
        text_split_method=os.environ.get("PUPU_TTS_TEXT_SPLIT_METHOD", "cut5").strip() or "cut5",
        normalize_audio=_env_bool("PUPU_TTS_NORMALIZE_AUDIO", False),
        ffmpeg_path=os.environ.get("PUPU_TTS_FFMPEG", "").strip().strip('"'),
        top_k=max(1, _env_int("PUPU_TTS_TOP_K", 5)),
        top_p=min(1.0, max(0.1, _env_float("PUPU_TTS_TOP_P", 0.85))),
        temperature=min(2.0, max(0.1, _env_float("PUPU_TTS_TEMPERATURE", 0.65))),
        repetition_penalty=min(3.0, max(0.1, _env_float("PUPU_TTS_REPETITION_PENALTY", 1.15))),
        speed_factor=min(2.0, max(0.5, _env_float("PUPU_TTS_SPEED_FACTOR", 1.0))),
        seed=_env_int("PUPU_TTS_SEED", -1),
        parallel_infer=_env_bool("PUPU_TTS_PARALLEL_INFER", False),
        sample_steps=max(4, _env_int("PUPU_TTS_SAMPLE_STEPS", 32)),
        super_sampling=_env_bool("PUPU_TTS_SUPER_SAMPLING", False),
    )


def _normalize_reply_text(text: str) -> str:
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    if not lines:
        return ""

    sentence_endings = set("。！？!?；;：:，,、…~～")
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
    bundled = _ROOT.parent / "Miniforge3" / "envs" / "GPTSoVits" / "Library" / "bin" / "ffmpeg.exe"
    if bundled.exists():
        return str(bundled)
    return None


def _normalize_audio_file(path: Path, cfg: TTSConfig) -> Path:
    if cfg.media_type != "wav" or not cfg.normalize_audio:
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
    if not cfg.ref_audio:
        print("[pupu][tts] skip: PUPU_TTS_REF_AUDIO is empty")
        return None
    if not Path(cfg.ref_audio).exists():
        print(f"[pupu][tts] skip: reference audio not found: {cfg.ref_audio}")
        return None
    if not cfg.prompt_text:
        print("[pupu][tts] skip: prompt text is empty")
        return None

    payload = {
        "text": reply_text,
        "text_lang": cfg.text_lang,
        "ref_audio_path": cfg.ref_audio,
        "prompt_text": cfg.prompt_text,
        "prompt_lang": cfg.prompt_lang,
        "text_split_method": cfg.text_split_method,
        "batch_size": 1,
        "media_type": cfg.media_type,
        "streaming_mode": False,
        "parallel_infer": cfg.parallel_infer,
        "top_k": cfg.top_k,
        "top_p": cfg.top_p,
        "temperature": cfg.temperature,
        "repetition_penalty": cfg.repetition_penalty,
        "speed_factor": cfg.speed_factor,
        "seed": cfg.seed,
        "sample_steps": cfg.sample_steps,
        "super_sampling": cfg.super_sampling,
    }

    try:
        started = time.perf_counter()
        response = httpx.post(f"{cfg.base_url}/tts", json=payload, timeout=cfg.timeout)
        elapsed = time.perf_counter() - started
    except Exception as exc:
        print(f"[pupu][tts] request failed: {exc}")
        return None

    if response.status_code != 200:
        display = response.text[:200].replace("\n", " ")
        print(f"[pupu][tts] request failed: status={response.status_code} body={display}")
        return None
    if not response.content:
        print("[pupu][tts] request failed: empty audio")
        return None

    try:
        cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        suffix = f".{cfg.media_type}"
        path = cfg.cache_dir / f"tts-{int(time.time())}-{uuid.uuid4().hex[:8]}{suffix}"
        path.write_bytes(response.content)
        path = _normalize_audio_file(path, cfg)
        print(f"[pupu][tts] generated {path.name} bytes={len(response.content)} time={elapsed:.1f}s")
        return path
    except Exception as exc:
        print(f"[pupu][tts] save failed: {exc}")
        return None
