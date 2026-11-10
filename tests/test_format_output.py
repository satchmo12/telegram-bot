"""Regression coverage for formatter report-link generation."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import format_output


class FormatOutputReportTests(unittest.TestCase):
    @staticmethod
    def _rule() -> dict:
        return {
            "output_template": "✨城市 #成都\n\n{content}",
            "output_entities": [],
            "cut_words": [],
            "cut_includes_word": False,
            "personal_report_link_enabled": True,
            "personal_report_link_field": "",
            "replacements": [
                {"id": 1, "from": "位置", "to": "区域"},
                {"id": 2, "from": "t.me/", "to": "@"},
            ],
        }

    @staticmethod
    def _message() -> SimpleNamespace:
        return SimpleNamespace(
            text="位置：成都\n联系：t.me/ExampleUser",
            entities=[],
            caption=None,
            caption_entities=[],
            forward_from_chat=SimpleNamespace(id=-1002003699777),
        )

    def test_report_id_is_read_after_replacements(self):
        """A t.me URL becomes a report username before report generation."""
        transformed = format_output._transformed_source_content(
            self._message(), self._rule()
        )

        self.assertIn("联系：@ExampleUser", transformed)
        self.assertEqual(
            format_output._personal_report_id(self._rule(), transformed),
            "exampleuser",
        )

    def test_formatting_passes_replaced_content_to_report_generation(self):
        """Report links are generated only after all content rules complete."""
        report_link = "https://t.me/example_bot?start=report_exampleuser"

        async def format_message():
            rule = {**self._rule(), "id": 12, "enabled": True, "channel_id": "-1002003699777"}
            with (
                patch.object(
                    format_output,
                    "load_format_output_config",
                    return_value={"next_id": 13, "rules": [rule]},
                ),
                patch.object(
                    format_output,
                    "_personal_report_link",
                    new=AsyncMock(return_value=report_link),
                ) as build_link,
            ):
                output_text, _entities, result_rule = (
                    await format_output.format_message_for_publish(
                        None, self._message()
                    )
                )
            return output_text, result_rule, build_link

        output_text, result_rule, build_link = asyncio.run(format_message())

        self.assertEqual(build_link.await_count, 1)
        self.assertEqual(build_link.call_args.args[2], "区域：成都\n联系：@ExampleUser")
        self.assertEqual(build_link.call_args.args[3], "exampleuser")
        self.assertEqual(result_rule["_personal_report_id"], "exampleuser")
        self.assertIn(report_link, output_text)


if __name__ == "__main__":
    unittest.main()
