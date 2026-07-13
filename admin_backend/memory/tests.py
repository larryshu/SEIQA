"""模組三測試：軟刪、purge 的 admin 限制、分頁，以及 messages/export。

軟刪那組是最容易改壞的地方：列表要看不到、但單筆仍要撈得到（清理前要能檢視），
這條規則寫在 ConversationViewSet.get_queryset() 的 action 判斷裡。
"""
from __future__ import annotations

from datetime import timedelta

from accounts.models import AuditLog, EndUser
from django.utils import timezone
from testutils import RoleAPITestCase

from .models import Conversation, Message

CONVERSATIONS_URL = "/api/v1/conversations/"
PURGE_URL = f"{CONVERSATIONS_URL}purge/"


def make_conversation(sid: str, **kwargs) -> Conversation:
    return Conversation.objects.create(sid=sid, title=f"對話 {sid}", **kwargs)


class SoftDeleteTests(RoleAPITestCase):
    def setUp(self):
        self.conv = make_conversation("s1")
        self.as_role("editor")

    def test_delete_soft_deletes_and_hides_from_list(self):
        resp = self.client.delete(f"{CONVERSATIONS_URL}{self.conv.pk}/")

        self.assertEqual(resp.status_code, 204)
        self.conv.refresh_from_db()  # 還在，只是被標記
        self.assertTrue(self.conv.is_deleted)

        listing = self.client.get(CONVERSATIONS_URL)
        self.assertEqual(listing.data["count"], 0)

    def test_soft_deleted_conversation_is_still_retrievable(self):
        """列表看不到，但單筆要撈得到——後台在硬刪前需要能檢視內容。"""
        self.client.delete(f"{CONVERSATIONS_URL}{self.conv.pk}/")

        resp = self.client.get(f"{CONVERSATIONS_URL}{self.conv.pk}/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["sid"], "s1")

    def test_soft_delete_is_audited(self):
        self.client.delete(f"{CONVERSATIONS_URL}{self.conv.pk}/")

        log = AuditLog.objects.get(target_type="conversation")
        self.assertEqual(log.action, "delete")
        self.assertEqual(log.changes, {"soft": True})

    def test_viewer_cannot_delete(self):
        self.as_role("viewer")

        resp = self.client.delete(f"{CONVERSATIONS_URL}{self.conv.pk}/")

        self.assertEqual(resp.status_code, 403)
        self.conv.refresh_from_db()
        self.assertFalse(self.conv.is_deleted)


class PurgeTests(RoleAPITestCase):
    def setUp(self):
        now = timezone.now()
        self.soft_deleted = make_conversation("gone", is_deleted=True)
        self.expired = make_conversation("old", expires_at=now - timedelta(days=1))
        self.live = make_conversation("keep", expires_at=now + timedelta(days=30))

    def test_purge_requires_admin(self):
        self.as_role("editor")  # editor 寫得了一般資料，但硬刪只有 admin 能做

        resp = self.client.post(PURGE_URL)

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Conversation.objects.count(), 3)

    def test_purge_removes_soft_deleted_and_expired_only(self):
        self.as_role("admin")

        resp = self.client.post(PURGE_URL)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["purged"], 2)
        self.assertEqual([c.sid for c in Conversation.objects.all()], ["keep"])

    def test_purge_cascades_to_messages(self):
        Message.objects.create(conversation=self.soft_deleted, role="user", content="hi")
        self.as_role("admin")

        self.client.post(PURGE_URL)

        self.assertEqual(Message.objects.count(), 0)


class PaginationTests(RoleAPITestCase):
    """對話列表是唯一會無限長大的清單，所以只有它分頁（memory/pagination.py）。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        Conversation.objects.bulk_create(
            [Conversation(sid=f"s{i}", title=f"t{i}") for i in range(60)]
        )

    def setUp(self):
        self.as_role("viewer")

    def test_default_page_size_is_50(self):
        resp = self.client.get(CONVERSATIONS_URL)

        self.assertEqual(resp.data["count"], 60)
        self.assertEqual(len(resp.data["results"]), 50)
        self.assertIsNotNone(resp.data["next"])

    def test_page_size_can_be_overridden(self):
        resp = self.client.get(CONVERSATIONS_URL, {"page_size": 10})

        self.assertEqual(len(resp.data["results"]), 10)

    def test_page_size_is_capped(self):
        resp = self.client.get(CONVERSATIONS_URL, {"page_size": 1000})

        self.assertEqual(len(resp.data["results"]), 60)  # 上限 200，這裡只有 60 筆全上


class FilteringTests(RoleAPITestCase):
    """?end_user= / ?search= / ?created_after= / ?ordering=（memory/filters.py）。"""

    def setUp(self):
        self.alice = EndUser.objects.create(username="alice")
        now = timezone.now()
        self.hers = make_conversation("s1", end_user=self.alice, message_count=10,
                                      last_active_at=now)
        self.hers.title = "遠距離戀愛可以維持嗎"
        self.hers.save(update_fields=["title"])
        self.anon = make_conversation("s2", message_count=2,
                                      last_active_at=now - timedelta(hours=1))
        self.as_role("viewer")

    def _sids(self, params=None) -> list[str]:
        resp = self.client.get(CONVERSATIONS_URL, params or {})
        self.assertEqual(resp.status_code, 200, resp.data)
        return [row["sid"] for row in resp.data["results"]]

    def test_filter_by_end_user(self):
        self.assertEqual(self._sids({"end_user": self.alice.pk}), ["s1"])

    def test_filter_anonymous_conversations(self):
        """沒登入就發問的對話：end_user 是 NULL。"""
        self.assertEqual(self._sids({"anonymous": "true"}), ["s2"])

    def test_search_matches_title(self):
        self.assertEqual(self._sids({"search": "遠距離"}), ["s1"])

    def test_filter_by_created_after(self):
        tomorrow = (timezone.now() + timedelta(days=1)).isoformat()

        self.assertEqual(self._sids({"created_after": tomorrow}), [])

    def test_ordering_can_be_overridden(self):
        self.assertEqual(self._sids({"ordering": "message_count"}), ["s2", "s1"])
        self.assertEqual(self._sids({"ordering": "-message_count"}), ["s1", "s2"])

    def test_default_ordering_is_by_last_active(self):
        """預設排序對齊 conv_list_idx——不指定 ordering 時要維持這個順序。"""
        self.assertEqual(self._sids(), ["s1", "s2"])

    def test_filters_do_not_resurrect_soft_deleted_conversations(self):
        self.hers.is_deleted = True
        self.hers.save(update_fields=["is_deleted"])

        self.assertEqual(self._sids({"end_user": self.alice.pk}), [])


class MessagesAndExportTests(RoleAPITestCase):
    def setUp(self):
        self.conv = make_conversation("s1")
        Message.objects.create(conversation=self.conv, role="user", content="遠距離戀愛可以維持嗎")
        Message.objects.create(
            conversation=self.conv, role="assistant", content="鄉民說…",
            used_tools=["community_search"],
            chart={"counts": {"正面": 3, "負面": 2}},
        )
        self.as_role("viewer")

    def test_messages_returns_the_turns_in_order(self):
        resp = self.client.get(f"{CONVERSATIONS_URL}{self.conv.pk}/messages/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual([m["role"] for m in resp.data], ["user", "assistant"])

    def test_message_payload_carries_the_chart(self):
        """圖是答案的一部分：後台檢視/匯出時要看得到那一輪畫了什麼。"""
        resp = self.client.get(f"{CONVERSATIONS_URL}{self.conv.pk}/messages/")

        self.assertEqual(resp.data[1]["chart"]["counts"], {"正面": 3, "負面": 2})
        self.assertEqual(resp.data[1]["used_tools"], ["community_search"])

    def test_export_bundles_sid_title_and_messages(self):
        resp = self.client.get(f"{CONVERSATIONS_URL}{self.conv.pk}/export/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["sid"], "s1")
        self.assertEqual(len(resp.data["messages"]), 2)
