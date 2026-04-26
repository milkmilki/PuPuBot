"""Shared Anthropic client and response helpers."""

from __future__ import annotations

import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MODEL = "claude-opus-4-6"
JUDGE_MODEL = "claude-haiku-4-5-20251001"

_client = None


def get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            base_url=os.environ.get("ANTHROPIC_BASE_URL"),
        )
    return _client


def join_text_blocks(content_blocks) -> str:
    return "".join(
        block.text for block in content_blocks if block.type == "text"
    )


def collect_reason_hint(content_blocks) -> str:
    return " ".join(
        block.text.strip()
        for block in content_blocks
        if block.type == "text" and getattr(block, "text", "").strip()
    )
