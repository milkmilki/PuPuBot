"""Media tool server."""

from __future__ import annotations

import os
from typing import Any

import httpx

from ..base import BuiltinToolServer, ToolContext, ToolSpec
from ..image_cache import resolve_image_context
from ...richmsg import download_image_as_base64

DEFAULT_VISION_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_VISION_MODEL = "qwen3.6-flash"
DEFAULT_VISION_TIMEOUT = 45.0
MAX_PROMPT_CHARS = 800
DEFAULT_VISION_QUESTION = (
    "请用中文观察这张图片。说明画面主体、人物或物体、可见文字、场景氛围、"
    "风格/画法，以及用户可能想表达的信息；不确定的身份或背景要明确说不确定。"
)


def look_at_image(image_urls: list[str], index: int = 0) -> str | list[dict]:
    """Download and return an image as a Claude content block."""

    if not image_urls:
        return "没有可以看的图片"
    if index < 0 or index >= len(image_urls):
        return f"图片索引超出范围，共 {len(image_urls)} 张图"

    url = image_urls[index]
    result = download_image_as_base64(url)
    if not result:
        return "图片下载失败了"

    b64, media_type = result
    return [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": b64,
            },
        },
        {
            "type": "text",
            "text": "这是用户发的图片",
        },
    ]


def _vision_api_key() -> str:
    for name in (
        "PUPU_MEMU_EMBED_API_KEY",
        "PUPU_MEMU_API_KEY",
    ):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _vision_base_url() -> str:
    return (
        os.environ.get("PUPU_MEMU_EMBED_BASE_URL", "").strip()
        or os.environ.get("PUPU_MEMU_BASE_URL", "").strip()
        or DEFAULT_VISION_BASE_URL
    ).rstrip("/")


def _vision_model() -> str:
    return os.environ.get("PUPU_VISION_MODEL", "").strip() or DEFAULT_VISION_MODEL


def _vision_timeout() -> float:
    raw = os.environ.get("PUPU_VISION_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_VISION_TIMEOUT
    try:
        return max(5.0, min(180.0, float(raw)))
    except ValueError:
        return DEFAULT_VISION_TIMEOUT


def _extract_vision_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"].strip())
        return "\n".join(part for part in parts if part).strip()
    return ""


def describe_image_with_qwen(
    image_urls: list[str],
    index: int = 0,
    prompt: str | None = None,
) -> str:
    """Describe an image through an OpenAI-compatible Qwen vision endpoint."""

    if not image_urls:
        return "没有可以看的图片"
    if index < 0 or index >= len(image_urls):
        return f"图片索引超出范围，共 {len(image_urls)} 张图"

    api_key = _vision_api_key()
    if not api_key:
        return (
            "视觉模型 API Key 未配置。请在 pupu.yaml 的 memu.embed_api_key 中填写百炼 API Key，"
            "视觉工具会直接复用 memU embedding 的百炼配置。"
        )

    result = download_image_as_base64(image_urls[index])
    if not result:
        return "图片下载失败了"
    b64, media_type = result
    user_question = str(prompt or "").strip()
    if user_question:
        question = (
            "请用中文回答下面这个关于图片的问题。优先围绕问题作答，同时补充有助于理解图片的关键细节；"
            "不要把不确定的身份、人物关系或作者信息说成事实。\n\n"
            f"问题：{user_question}"
        )
    else:
        question = DEFAULT_VISION_QUESTION
    if len(question) > MAX_PROMPT_CHARS:
        question = question[:MAX_PROMPT_CHARS]

    payload = {
        "model": _vision_model(),
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": question,
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{b64}",
                        },
                    },
                ],
            }
        ],
    }
    try:
        response = httpx.post(
            f"{_vision_base_url()}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=_vision_timeout(),
        )
        response.raise_for_status()
        text = _extract_vision_text(response.json())
    except Exception as exc:
        return f"视觉模型调用失败：{type(exc).__name__}: {exc}"
    return text or "视觉模型没有返回可读描述"


def _handle_look_at_image(tool_input: dict, context: ToolContext):
    return look_at_image(
        resolve_image_context(context.session_id, context.image_urls),
        tool_input.get("image_index", 0),
    )


def _handle_describe_image(tool_input: dict, context: ToolContext):
    prompt = (
        tool_input.get("query")
        or tool_input.get("question")
        or tool_input.get("prompt")
    )
    return describe_image_with_qwen(
        resolve_image_context(context.session_id, context.image_urls),
        tool_input.get("image_index", 0),
        prompt,
    )


MEDIA_SERVER = BuiltinToolServer(
    name="media",
    description="Image inspection tools.",
    tools=(
        ToolSpec(
            server="media",
            name="look_at_image",
            description="查看用户当前或最近发的图片。只在你真的好奇或者觉得有必要看的时候才用，普通聊天不需要每张图都看。",
            input_schema={
                "type": "object",
                "properties": {
                    "image_index": {
                        "type": "integer",
                        "description": "要看第几张图，从 0 开始，默认 0 表示第一张",
                    }
                },
                "required": [],
            },
            handler=_handle_look_at_image,
            legacy_names=("look_at_image",),
        ),
        ToolSpec(
            server="media",
            name="describe_image",
            description=(
                "Use the configured Qwen vision model to inspect the current or recently sent user image. "
                "Pass a query/question when you want to know something specific, "
                "such as who or what is shown, what the image is about, visible text, "
                "style, drawing quality, mood, or whether a detail is present. "
                "Without a query, it returns a general observation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "image_index": {
                        "type": "integer",
                        "description": "Image index, starting from 0. Defaults to 0.",
                    },
                    "question": {
                        "type": "string",
                        "description": "Optional specific question about the image.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Optional natural-language query about what to inspect or answer in the image.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Backward-compatible alias for question/query.",
                    },
                },
                "required": [],
            },
            handler=_handle_describe_image,
            legacy_names=("describe_image",),
        ),
    ),
)
