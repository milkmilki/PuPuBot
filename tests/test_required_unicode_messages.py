import builtins
import unittest
from io import StringIO
from unittest.mock import AsyncMock, patch

import pupu.logging_utils as logging_utils
from pupu.actor.message_buffer import MessageBuffer, _Buffer
from pupu.actor.types import ActorInboundMessage
from pupu.sessions import OWNER_SESSION


class RequiredUnicodeMessageRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_shushing_face_text_message_does_not_crash_receive_buffer(self) -> None:
        """Required regression: QQ text containing U+1F92B must reach chat."""
        emoji = chr(0x1F92B)
        sink = StringIO()
        console_lines: list[str] = []

        def gbk_console_print(*args, **kwargs):
            sep = kwargs.get("sep", " ")
            end = kwargs.get("end", "\n")
            text = sep.join(str(arg) for arg in args) + end
            text.encode("gbk")
            console_lines.append(text)

        send_text = AsyncMock()
        buffer = MessageBuffer(
            send_text=send_text,
            handle_command=AsyncMock(return_value=False),
            debounce_seconds=0,
        )
        message = ActorInboundMessage(
            session_id=OWNER_SESSION,
            identity_session=OWNER_SESSION,
            user_id="111",
            user_name="Owner",
            text=emoji,
            message_id="required-unicode-emoji",
        )
        buf = _Buffer(message=message, texts=[emoji])

        with patch.object(logging_utils, "_original_print", side_effect=gbk_console_print):
            with patch.object(logging_utils, "_get_sink", return_value=sink):
                with patch.object(builtins, "print", logging_utils._patched_print):
                    with patch("pupu.actor.message_buffer.chat", return_value="ok") as mock_chat:
                        await buffer._process_buffer(buf, persist_user=True)

        mock_chat.assert_called_once()
        self.assertEqual(mock_chat.call_args.args[0], emoji)
        send_text.assert_awaited_once()
        self.assertEqual(send_text.await_args.args[1], "ok")
        self.assertIn(emoji, sink.getvalue())
        self.assertTrue(any("\\U0001f92b" in line for line in console_lines))


if __name__ == "__main__":
    unittest.main()
