import unittest

from pupu.command_registry import (
    command_aliases,
    command_usage,
    render_help,
    resolve_command,
)


class CommandRegistryTests(unittest.TestCase):
    def test_resolve_cli_aliases(self) -> None:
        self.assertEqual(resolve_command("/events", surface="cli").command_id, "important")
        self.assertEqual(resolve_command("/帮助", surface="cli").command_id, "help")
        self.assertEqual(resolve_command("/q", surface="cli").command_id, "quit")

    def test_surface_filtering(self) -> None:
        self.assertIsNone(resolve_command("/voice", surface="cli"))
        self.assertEqual(resolve_command("/voice", surface="qq").command_id, "voice")
        self.assertIsNone(resolve_command("/quit", surface="qq"))

    def test_help_is_rendered_from_registered_commands(self) -> None:
        cli_help = render_help(surface="cli")
        qq_help = render_help(surface="qq")

        self.assertIn("/events", cli_help)
        self.assertIn("/debug", cli_help)
        self.assertIn("/tidy", cli_help)
        self.assertNotIn("/voice", cli_help)
        self.assertIn("/voice", qq_help)
        self.assertIn("/debug", qq_help)
        self.assertIn("/silence", qq_help)
        self.assertIn("（管理员）", qq_help)

    def test_aliases_and_usage_helpers(self) -> None:
        self.assertIn("events", command_aliases("important"))
        self.assertEqual(command_usage("tidy"), "/tidy [check|apply]")


if __name__ == "__main__":
    unittest.main()
