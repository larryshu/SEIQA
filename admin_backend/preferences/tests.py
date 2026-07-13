"""模組四測試：system-settings（以 key 為路徑）與每使用者偏好的 upsert。

最重要的是 test_manual_edit_flips_source_to_manual：runtime 登出時的偏好推論靠
source='manual' 來「不覆寫人工設定」，後台改值時若沒把 source 標回 manual，
人工調整下次登出就會被 LLM 蓋掉——這條測試把那個規則釘住。
"""
from __future__ import annotations

import json

from accounts.models import AuditLog, EndUser
from testutils import RoleAPITestCase

from .models import SystemSetting, UserPreference

SETTINGS_URL = "/api/v1/system-settings/"


class SystemSettingTests(RoleAPITestCase):
    def setUp(self):
        self.setting = SystemSetting.objects.create(key="chat_model", value="gpt-4.1",
                                                    group_name="llm")

    def test_setting_is_addressed_by_key_not_id(self):
        self.as_role("viewer")

        resp = self.client.get(f"{SETTINGS_URL}chat_model/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["value"], "gpt-4.1")

    def test_update_records_who_changed_it(self):
        self.as_role("editor")

        resp = self.client.patch(f"{SETTINGS_URL}chat_model/", {"value": "gpt-4o"}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        self.setting.refresh_from_db()
        self.assertEqual(self.setting.value, "gpt-4o")
        self.assertEqual(self.setting.updated_by, self.users["editor"])

    def test_viewer_cannot_change_settings(self):
        self.as_role("viewer")

        resp = self.client.patch(f"{SETTINGS_URL}chat_model/", {"value": "gpt-4o"}, format="json")

        self.assertEqual(resp.status_code, 403)


class EndUserPreferenceTests(RoleAPITestCase):
    def setUp(self):
        self.end_user = EndUser.objects.create(username="alice")
        self.url = f"/api/v1/end-users/{self.end_user.pk}/preferences/"
        self.as_role("editor")

    def test_upsert_creates_then_overwrites_by_key(self):
        self.client.put(self.url, [{"key": "tone", "value": "casual"}], format="json")
        resp = self.client.put(self.url, [{"key": "tone", "value": "formal"}], format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        prefs = UserPreference.objects.filter(end_user=self.end_user)
        self.assertEqual(prefs.count(), 1)
        self.assertEqual(prefs.get().value, "formal")

    def test_manual_edit_flips_source_to_manual(self):
        """後台改過的偏好一定是 manual——否則 runtime 下次推論會把人工設定蓋回去。"""
        UserPreference.objects.create(end_user=self.end_user, key="tone", value="casual",
                                      source="inferred", confidence=0.8)

        self.client.put(self.url, [{"key": "tone", "value": "formal"}], format="json")

        pref = UserPreference.objects.get(end_user=self.end_user, key="tone")
        self.assertEqual(pref.source, "manual")
        self.assertIsNone(pref.confidence)  # 人工設定沒有「信心」可言

    def test_json_value_is_stored_so_runtime_can_parse_it(self):
        """list 要用 json.dumps 存；用 str() 會存成 "['dcard']"，runtime 的 json.loads 解不開。"""
        resp = self.client.put(
            self.url,
            [{"key": "included_platforms", "value": ["dcard", "ptt"], "value_type": "json"}],
            format="json",
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        stored = UserPreference.objects.get(key="included_platforms").value
        self.assertEqual(json.loads(stored), ["dcard", "ptt"])

    def test_missing_key_returns_400_not_500(self):
        resp = self.client.put(self.url, [{"value": "casual"}], format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(UserPreference.objects.exists())

    def test_one_bad_item_rejects_the_whole_batch(self):
        resp = self.client.put(
            self.url,
            [{"key": "tone", "value": "casual"},
             {"key": "max_items", "value": "abc", "value_type": "int"}],
            format="json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(UserPreference.objects.exists())  # 好的那筆也不該落地

    def test_upsert_is_audited(self):
        self.client.put(self.url, [{"key": "tone", "value": "casual"}], format="json")

        log = AuditLog.objects.get(target_type="user_preference")
        self.assertEqual(log.action, "update")
        self.assertEqual(log.target_id, str(self.end_user.pk))

    def test_unknown_end_user_returns_404(self):
        resp = self.client.put("/api/v1/end-users/9999/preferences/",
                               [{"key": "tone", "value": "casual"}], format="json")

        self.assertEqual(resp.status_code, 404)

    def test_get_only_returns_this_users_preferences(self):
        other = EndUser.objects.create(username="bob")
        UserPreference.objects.create(end_user=self.end_user, key="tone", value="casual")
        UserPreference.objects.create(end_user=other, key="tone", value="formal")

        resp = self.client.get(self.url)

        self.assertEqual(len(resp.data), 1)
        self.assertEqual(resp.data[0]["value"], "casual")

    def test_viewer_cannot_write_preferences(self):
        self.as_role("viewer")

        resp = self.client.put(self.url, [{"key": "tone", "value": "casual"}], format="json")

        self.assertEqual(resp.status_code, 403)
