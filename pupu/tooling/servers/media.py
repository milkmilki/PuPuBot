"""Media tool server."""

from __future__ import annotations

from ..base import BuiltinToolServer, ToolContext, ToolSpec
from ...richmsg import download_image_as_base64


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


def _handle_look_at_image(tool_input: dict, context: ToolContext):
    return look_at_image(context.image_urls or [], tool_input.get("image_index", 0))


MEDIA_SERVER = BuiltinToolServer(
    name="media",
    description="Image inspection tools.",
    tools=(
        ToolSpec(
            server="media",
            name="look_at_image",
            description="查看用户刚刚发的图片。只在你真的好奇或者觉得有必要看的时候才用，普通聊天不需要每张图都看。",
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
    ),
)
