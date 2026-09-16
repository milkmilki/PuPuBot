import unittest
from pathlib import Path
from unittest.mock import patch

from pupu.image_context import (
    clear_recent_images,
    get_image_data,
    remember_image_data,
    resolve_native_image_context,
)
from pupu.instance_context import InstanceContext, activate_instance_context
from pupu.richmsg import download_image_as_base64


class NativeImageContextTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_recent_images()

    def tearDown(self) -> None:
        clear_recent_images()

    def test_current_turn_uses_only_current_images(self) -> None:
        resolve_native_image_context("owner", ["https://example.test/old.jpg"], "先看这个")

        selected = resolve_native_image_context(
            "owner",
            ["https://example.test/new.jpg"],
            "再看这个",
        )

        self.assertEqual(selected, ["https://example.test/new.jpg"])

    def test_explicit_followup_reuses_recent_image(self) -> None:
        resolve_native_image_context("owner", ["https://example.test/image.jpg"], "")

        selected = resolve_native_image_context("owner", None, "刚才那张图画得怎么样？")

        self.assertEqual(selected, ["https://example.test/image.jpg"])

    def test_unrelated_text_does_not_resend_recent_image(self) -> None:
        resolve_native_image_context("owner", ["https://example.test/image.jpg"], "")

        selected = resolve_native_image_context("owner", None, "晚上吃什么？")

        self.assertEqual(selected, [])

    def test_generic_image_word_does_not_reuse_recent_image(self) -> None:
        resolve_native_image_context("owner", ["https://example.test/image.jpg"], "")

        selected = resolve_native_image_context("owner", None, "图片怎么压缩？")

        self.assertEqual(selected, [])

    def test_session_cache_evicts_oldest_image_by_byte_budget(self) -> None:
        with patch(
            "pupu.image_context.DEFAULT_SESSION_IMAGE_CACHE_MAX_BYTES",
            5,
        ):
            remember_image_data("owner", "https://example.test/old.jpg", "aaaa", "image/jpeg")
            remember_image_data("owner", "https://example.test/new.jpg", "bbbb", "image/jpeg")

        self.assertIsNone(get_image_data("owner", "https://example.test/old.jpg"))
        self.assertEqual(
            get_image_data("owner", "https://example.test/new.jpg"),
            ("bbbb", "image/jpeg"),
        )

    def test_same_session_is_isolated_between_instances(self) -> None:
        def context(instance_id: str) -> InstanceContext:
            root = Path("tests/_tmp") / instance_id
            data = root / "data"
            return InstanceContext(
                instance_id=instance_id,
                instance_dir=root,
                display_name=instance_id,
                qq_mode="cli",
                config_path=root / "instance.json",
                persona_path=root / "persona.json",
                db_path=data / "pupu.db",
                data_dir=data,
                logs_dir=data / "logs",
            )

        with activate_instance_context(context("pupu")):
            resolve_native_image_context("owner", ["https://example.test/pupu.jpg"], "")
        with activate_instance_context(context("lulu")):
            resolve_native_image_context("owner", ["https://example.test/lulu.jpg"], "")
            self.assertEqual(
                resolve_native_image_context("owner", None, "刚才那张图呢"),
                ["https://example.test/lulu.jpg"],
            )
        with activate_instance_context(context("pupu")):
            self.assertEqual(
                resolve_native_image_context("owner", None, "刚才那张图呢"),
                ["https://example.test/pupu.jpg"],
            )


class ImageDownloadTests(unittest.TestCase):
    def test_download_stops_when_stream_exceeds_limit(self) -> None:
        class FakeResponse:
            headers = {"content-type": "image/png"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def raise_for_status(self):
                return None

            def iter_bytes(self):
                yield b"1234"
                yield b"5678"
                raise AssertionError("stream should stop after crossing the limit")

        with patch("pupu.richmsg.httpx.stream", return_value=FakeResponse()):
            result = download_image_as_base64(
                "https://example.test/large.png",
                max_bytes=5,
            )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
