from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from group import invite_stats


class ApprovedInviteChatMemberTests(IsolatedAsyncioTestCase):
    async def test_chat_member_join_uses_original_link_for_approved_applicant(self):
        """Supergroups can emit only chat_member for an approved join."""
        original_link = "https://t.me/+original-personal-link"
        approved_link = "https://t.me/+one-time-approved-link"
        user = SimpleNamespace(
            id=456,
            username="new_member",
            full_name="New Member",
            is_bot=False,
        )
        update = SimpleNamespace(
            chat_member=SimpleNamespace(
                chat=SimpleNamespace(id=-100123),
                old_chat_member=SimpleNamespace(status="left"),
                new_chat_member=SimpleNamespace(status="member", user=user),
                invite_link=SimpleNamespace(invite_link=approved_link),
            )
        )
        context = SimpleNamespace()

        with patch.object(
            invite_stats,
            "get_approval_request",
            return_value={
                "status": "approved",
                "inviter_id": 789,
                "invite_link": original_link,
            },
        ), patch.object(
            invite_stats,
            "_credit_invite_join",
            new=AsyncMock(return_value=[456]),
        ) as credit_join, patch.object(
            invite_stats,
            "update_approval_request",
        ) as update_request:
            await invite_stats.handle_chat_member_join(update, context)

        credit_join.assert_awaited_once_with(
            context,
            -100123,
            [456],
            original_link,
            {456: "new_member"},
            {456: "New Member"},
        )
        update_request.assert_called_once()
        self.assertEqual(update_request.call_args.args[:2], (-100123, 456))
        self.assertEqual(
            update_request.call_args.args[2]["status"],
            "joined",
        )
        self.assertTrue(update_request.call_args.args[2]["points_processed"])

    async def test_unapproved_chat_member_join_keeps_using_actual_link(self):
        user = SimpleNamespace(
            id=456,
            username="new_member",
            full_name="New Member",
            is_bot=False,
        )
        update = SimpleNamespace(
            chat_member=SimpleNamespace(
                chat=SimpleNamespace(id=-100123),
                old_chat_member=SimpleNamespace(status="left"),
                new_chat_member=SimpleNamespace(status="member", user=user),
                invite_link=SimpleNamespace(invite_link="https://t.me/+ordinary-link"),
            )
        )
        context = SimpleNamespace()

        with patch.object(invite_stats, "get_approval_request", return_value=None), patch.object(
            invite_stats,
            "_credit_invite_join",
            new=AsyncMock(return_value=[456]),
        ) as credit_join:
            await invite_stats.handle_chat_member_join(update, context)

        credit_join.assert_awaited_once_with(
            context,
            -100123,
            [456],
            "https://t.me/+ordinary-link",
            {456: "new_member"},
            {456: "New Member"},
        )
