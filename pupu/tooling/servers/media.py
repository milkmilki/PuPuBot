"""Media tool server."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from ..base import BuiltinToolServer, ToolContext, ToolSpec
from ..image_cache import (
    get_image_data,
    get_vision_text,
    remember_image_data,
    remember_vision_text,
    resolve_image_context,
)
from ...richmsg import download_image_as_base64

DEFAULT_VISION_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_VISION_MODEL = "qwen3.6-flash"
DEFAULT_VISION_TIMEOUT = 45.0
DEFAULT_VISION_RETRY_ATTEMPTS = 3
MAX_DASHSCOPE_BASE64_SOURCE_BYTES = 7 * 1024 * 1024
MAX_PROMPT_CHARS = 800
DEFAULT_VISION_QUESTION = (
    "请用中文观察这张图片。说明画面主体、人物或物体、可见文字、场景氛围、"
    "风格/画法，以及用户可能想表达的信息；不确定的身份或背景要明确说不确定。"
)


def _image_data_or_download(session_id: str | None, url: str) -> tuple[str, str] | None:
    cached = get_image_data(session_id, url)
    if cached:
        return cached
    result = download_image_as_base64(url)
    if result:
        b64, media_type = result
        remember_image_data(session_id, url, b64, media_type)
    return result


def _is_transient_vision_error(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status in {408, 409, 425, 429, 500, 502, 503, 504}
    text = str(exc).lower()
    return any(term in text for term in ("timeout", "timed out", "eof", "connection", "temporarily"))


def _vision_retry_delay_seconds(attempt: int) -> float:
    return min(4.0, float(2 ** max(0, attempt - 1)))


def look_at_image(
    image_urls: list[str],
    index: int = 0,
    session_id: str | None = None,
) -> str | list[dict]:
    """Download and return an image as a Claude content block."""

    if not image_urls:
        return "没有可以看的图片"
    if index < 0 or index >= len(image_urls):
        return f"图片索引超出范围，共 {len(image_urls)} 张图"

    url = image_urls[index]
    result = _image_data_or_download(session_id, url)
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


def _vision_image_mode() -> str:
    mode = os.environ.get("PUPU_VISION_IMAGE_MODE", "").strip().lower()
    return mode if mode in {"auto", "url", "base64"} else "auto"


def _vision_timeout() -> float:
    raw = os.environ.get("PUPU_VISION_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_VISION_TIMEOUT
    try:
        return max(5.0, min(180.0, float(raw)))
    except ValueError:
        return DEFAULT_VISION_TIMEOUT


def _vision_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # DashScope requires this for temporary image URLs, which QQ/NapCat commonly returns.
        "X-DashScope-OssResourceResolve": "enable",
    }


def _vision_error_preview(exc: Exception, limit: int = 500) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text.strip()
        if body:
            body = body.replace("\r", " ").replace("\n", " ")
            if len(body) > limit:
                body = body[:limit] + "...(truncated)"
            return f"{exc}; response={body}"
    return str(exc)


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


def _data_image_url(media_type: str, b64: str) -> str:
    return f"data:{media_type};base64,{b64}"


def _base64_source_size(b64: str) -> int:
    value = str(b64 or "").strip().rstrip("=")
    return max(0, (len(value) * 3) // 4)


def _vision_image_url_variants(url: str, media_type: str, b64: str) -> list[str]:
    data_url = _data_image_url(media_type, b64)
    mode = _vision_image_mode()
    is_http_url = str(url or "").startswith(("http://", "https://"))
    base64_allowed = _base64_source_size(b64) <= MAX_DASHSCOPE_BASE64_SOURCE_BYTES
    if mode == "base64" or not is_http_url:
        if not base64_allowed:
            return []
        return [data_url]
    if mode == "url":
        return [url]
    if not base64_allowed:
        return [url]
    return [url, data_url]


def describe_image_with_qwen(
    image_urls: list[str],
    index: int = 0,
    prompt: str | None = None,
    session_id: str | None = None,
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

    url = image_urls[index]
    result = _image_data_or_download(session_id, url)
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
    model = _vision_model()

    cached_text = get_vision_text(session_id, url, model, question)
    if cached_text:
        return cached_text

    last_error: Exception | None = None
    image_variants = _vision_image_url_variants(url, media_type, b64)
    if not image_variants:
        size_mb = _base64_source_size(b64) / 1024 / 1024
        return f"视觉模型调用失败：图片过大（约 {size_mb:.1f} MB），超过百炼 base64 输入限制"
    for variant_index, image_url in enumerate(image_variants, start=1):
        payload = {
            "model": model,
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
                                "url": image_url,
                            },
                        },
                    ],
                },
            ],
        }
        for attempt in range(1, DEFAULT_VISION_RETRY_ATTEMPTS + 1):
            try:
                response = httpx.post(
                    f"{_vision_base_url()}/chat/completions",
                    headers=_vision_headers(api_key),
                    json=payload,
                    timeout=_vision_timeout(),
                )
                response.raise_for_status()
                text = _extract_vision_text(response.json())
                if text:
                    remember_vision_text(session_id, url, model, question, text)
                if attempt > 1 or variant_index > 1:
                    print(
                        "[pupu][vision] retry recovered "
                        f"variant={variant_index}/{len(image_variants)} "
                        f"attempt={attempt}/{DEFAULT_VISION_RETRY_ATTEMPTS}"
                    )
                return text or "视觉模型没有返回可读描述"
            except Exception as exc:
                last_error = exc
                if attempt >= DEFAULT_VISION_RETRY_ATTEMPTS or not _is_transient_vision_error(exc):
                    break
                delay = _vision_retry_delay_seconds(attempt)
                print(
                    "[pupu][vision] retry "
                    f"variant={variant_index}/{len(image_variants)} "
                    f"attempt={attempt}/{DEFAULT_VISION_RETRY_ATTEMPTS} delay_seconds={delay:.1f} "
                    f"error={type(exc).__name__}: {_vision_error_preview(exc)}"
                )
                time.sleep(delay)
        if variant_index < len(image_variants):
            print(
                "[pupu][vision] retry fallback "
                f"variant={variant_index}/{len(image_variants)} "
                f"error={type(last_error).__name__}: {_vision_error_preview(last_error) if last_error else '<none>'}"
            )
    return f"视觉模型调用失败：{type(last_error).__name__}: {_vision_error_preview(last_error) if last_error else '<none>'}"


def _handle_look_at_image(tool_input: dict, context: ToolContext):
    return describe_image_with_qwen(
        resolve_image_context(context.session_id, context.image_urls),
        tool_input.get("image_index", 0),
        tool_input.get("query") or tool_input.get("question") or tool_input.get("prompt"),
        session_id=context.session_id,
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
        session_id=context.session_id,
    )


MEDIA_SERVER = BuiltinToolServer(
    name="media",
    description="Image inspection tools.",
    tools=(
        ToolSpec(
            server="media",
            name="look_at_image",
            description="查看用户当前或最近发的图片，并返回中文文字描述。只在你真的好奇或者觉得有必要看的时候才用，普通聊天不需要每张图都看。",
            input_schema={
                "type": "object",
                "properties": {
                    "image_index": {
                        "type": "integer",
                        "description": "要看第几张图，从 0 开始，默认 0 表示第一张",
                    },
                    "question": {
                        "type": "string",
                        "description": "可选，关于图片的具体问题。",
                    },
                    "query": {
                        "type": "string",
                        "description": "可选，想让视觉模型重点看的内容。",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "兼容字段，等同于 question/query。",
                    },
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
