"""模組一測試：RBAC、activate 的唯一性、稽核、configs 輸入驗證，以及 N+1 迴歸。

N+1 那兩條是「效能迴歸測試」：不寫死查詢次數（Django 版本一升就會壞），
改成斷言「查詢數不隨資料筆數成長」——這才是 prefetch_related 真正保證的事。
"""
from __future__ import annotations

from accounts.models import AuditLog
from testutils import RoleAPITestCase

from .models import Agent, Skill, SourceConfig, SourcePlatform

AGENTS_URL = "/api/v1/agents/"
SKILLS_URL = "/api/v1/skills/"
PLATFORMS_URL = "/api/v1/source-platforms/"

NEW_AGENT = {"name": "new-agent", "system_prompt": "你是助理", "model": "gpt-4.1"}


class AgentRBACTests(RoleAPITestCase):
    """讀＝viewer 以上、寫＝editor 以上（accounts/permissions.py::RoleBasedReadWrite）。"""

    def test_anonymous_cannot_read(self):
        self.as_anonymous()

        self.assertEqual(self.client.get(AGENTS_URL).status_code, 401)

    def test_authenticated_user_without_role_is_forbidden(self):
        """登入了但沒被指派角色 → 403（有身分但沒權限），不是 401。"""
        self.as_role("roleless")

        self.assertEqual(self.client.get(AGENTS_URL).status_code, 403)

    def test_viewer_can_read_but_not_write(self):
        self.as_role("viewer")

        self.assertEqual(self.client.get(AGENTS_URL).status_code, 200)
        self.assertEqual(self.client.post(AGENTS_URL, NEW_AGENT, format="json").status_code, 403)
        self.assertFalse(Agent.objects.filter(name="new-agent").exists())

    def test_editor_can_write(self):
        self.as_role("editor")

        resp = self.client.post(AGENTS_URL, NEW_AGENT, format="json")

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertTrue(Agent.objects.filter(name="new-agent").exists())

    def test_is_active_cannot_be_set_directly(self):
        """is_active 是 read_only：只能走 activate 動作，不能從 POST 偷渡進來。"""
        self.as_role("editor")

        resp = self.client.post(AGENTS_URL, {**NEW_AGENT, "is_active": True}, format="json")

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertFalse(Agent.objects.get(name="new-agent").is_active)


class AgentActivateTests(RoleAPITestCase):
    def setUp(self):
        self.old = Agent.objects.create(name="old", system_prompt="p", model="gpt-4.1",
                                        is_active=True)
        self.new = Agent.objects.create(name="new", system_prompt="p", model="gpt-4.1")

    def test_activate_leaves_exactly_one_active_agent(self):
        self.as_role("editor")

        resp = self.client.post(f"{AGENTS_URL}{self.new.pk}/activate/")

        self.assertEqual(resp.status_code, 200, resp.data)
        self.old.refresh_from_db()
        self.new.refresh_from_db()
        self.assertFalse(self.old.is_active)
        self.assertTrue(self.new.is_active)
        self.assertEqual(Agent.objects.filter(is_active=True).count(), 1)

    def test_activate_is_audited_as_publish(self):
        self.as_role("editor")

        self.client.post(f"{AGENTS_URL}{self.new.pk}/activate/")

        log = AuditLog.objects.get(target_type="agent", action="publish")
        self.assertEqual(log.actor, self.users["editor"])
        self.assertEqual(log.target_id, str(self.new.pk))

    def test_viewer_cannot_activate(self):
        self.as_role("viewer")

        resp = self.client.post(f"{AGENTS_URL}{self.new.pk}/activate/")

        self.assertEqual(resp.status_code, 403)
        self.new.refresh_from_db()
        self.assertFalse(self.new.is_active)


class AgentSkillWiringTests(RoleAPITestCase):
    """skill_ids 是 write_only 的輸入、skills 是唯讀的輸出——一進一出要對得起來。"""

    def setUp(self):
        self.search = Skill.objects.create(name="community_search", description="查",
                                           handler_key="community_search")
        self.stance = Skill.objects.create(name="stance_breakdown", description="算",
                                           handler_key="stance_breakdown")
        self.as_role("editor")

    def test_create_with_skill_ids_returns_skills(self):
        resp = self.client.post(
            AGENTS_URL, {**NEW_AGENT, "skill_ids": [self.search.pk, self.stance.pk]},
            format="json",
        )

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual({s["name"] for s in resp.data["skills"]},
                         {"community_search", "stance_breakdown"})
        self.assertNotIn("skill_ids", resp.data)  # write_only，不該回吐

    def test_update_replaces_the_skill_set(self):
        agent = Agent.objects.create(name="a", system_prompt="p", model="gpt-4.1")
        agent.skills.set([self.search, self.stance])

        resp = self.client.patch(f"{AGENTS_URL}{agent.pk}/",
                                 {"skill_ids": [self.search.pk]}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual([s.name for s in agent.skills.all()], ["community_search"])

    def test_unknown_skill_id_returns_400(self):
        resp = self.client.post(AGENTS_URL, {**NEW_AGENT, "skill_ids": [9999]}, format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("skill_ids", resp.data["errors"])


class SourceConfigUpsertTests(RoleAPITestCase):
    """PUT /source-platforms/{id}/configs/ 的輸入驗證（原本是直接讀 request.data）。"""

    def setUp(self):
        self.platform = SourcePlatform.objects.create(
            name="dcard", display_name="Dcard", adapter_key="dcard", kind="live_crawl")
        self.url = f"{PLATFORMS_URL}{self.platform.pk}/configs/"
        self.as_role("editor")

    def test_upsert_creates_then_updates_by_key(self):
        self.client.put(self.url, [{"key": "top_k", "value": 5, "value_type": "int"}],
                        format="json")
        resp = self.client.put(self.url, [{"key": "top_k", "value": 8, "value_type": "int"}],
                               format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        configs = SourceConfig.objects.filter(platform=self.platform)
        self.assertEqual(configs.count(), 1)  # 同 key 是覆寫，不是再插一筆
        self.assertEqual(configs.get().value, "8")

    def test_missing_key_returns_400_not_500(self):
        """少送 key 以前會在迴圈裡 item["key"] 撞 KeyError → 500。"""
        resp = self.client.put(self.url, [{"value": "5", "value_type": "int"}], format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(SourceConfig.objects.exists())

    def test_value_that_contradicts_value_type_returns_400(self):
        """說是 int 就得真的轉得成 int——不然壞值會寫進 DB，等 runtime 讀出來才靜靜地爛掉。"""
        resp = self.client.put(self.url, [{"key": "top_k", "value": "abc", "value_type": "int"}],
                               format="json")

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(SourceConfig.objects.exists())

    def test_unknown_value_type_returns_400(self):
        resp = self.client.put(self.url, [{"key": "top_k", "value": "5", "value_type": "decimal"}],
                               format="json")

        self.assertEqual(resp.status_code, 400)

    def test_one_bad_item_rejects_the_whole_batch(self):
        resp = self.client.put(
            self.url,
            [{"key": "top_k", "value": 5, "value_type": "int"},
             {"key": "min_score", "value": "abc", "value_type": "float"}],
            format="json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(SourceConfig.objects.exists())  # 好的那筆也不該寫進去

    def test_boolean_is_stored_in_the_form_runtime_can_read(self):
        resp = self.client.put(self.url, [{"key": "headless", "value": False, "value_type": "bool"}],
                               format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        # runtime 的 caster 是 value.lower() in ("1","true","yes","on")，所以要存成小寫字面值
        self.assertEqual(SourceConfig.objects.get(key="headless").value, "false")

    def test_viewer_cannot_write_configs(self):
        self.as_role("viewer")

        resp = self.client.put(self.url, [{"key": "top_k", "value": 5, "value_type": "int"}],
                               format="json")

        self.assertEqual(resp.status_code, 403)


class AuditTests(RoleAPITestCase):
    def test_create_writes_an_audit_row(self):
        self.as_role("editor")

        self.client.post(SKILLS_URL, {"name": "s1", "description": "d", "handler_key": "h"},
                         format="json")

        log = AuditLog.objects.get(target_type="skill")
        self.assertEqual(log.action, "create")
        self.assertEqual(log.actor, self.users["editor"])

    def test_sensitive_fields_are_stripped_from_the_audit_trail(self):
        self.as_role("editor")

        self.client.post(
            SKILLS_URL,
            {"name": "s1", "description": "d", "handler_key": "h", "password": "hunter2"},
            format="json",
        )

        log = AuditLog.objects.get(target_type="skill")
        self.assertNotIn("password", log.changes)
        self.assertEqual(log.changes["name"], "s1")


class QueryCountRegressionTests(RoleAPITestCase):
    """N+1 迴歸：拿掉 prefetch_related 這兩條就會紅。"""

    def setUp(self):
        self.skills = [
            Skill.objects.create(name=f"skill_{i}", description="d", handler_key=f"h{i}")
            for i in range(3)
        ]
        self.as_role("viewer")

    def _make_agents(self, n: int, offset: int = 0) -> None:
        for i in range(offset, offset + n):
            agent = Agent.objects.create(name=f"agent_{i}", system_prompt="p", model="gpt-4.1")
            agent.skills.set(self.skills)

    def _make_platforms(self, n: int, offset: int = 0) -> None:
        for i in range(offset, offset + n):
            platform = SourcePlatform.objects.create(
                name=f"p_{i}", display_name=f"P{i}", adapter_key=f"a{i}", kind="vector")
            for k in range(3):
                SourceConfig.objects.create(platform=platform, key=f"k{k}", value="1",
                                            value_type="int")

    def test_agent_list_query_count_does_not_grow_with_agents(self):
        self._make_agents(3)
        baseline = self.count_queries(AGENTS_URL)

        self._make_agents(20, offset=3)

        self.assertEqual(self.count_queries(AGENTS_URL), baseline)

    def test_platform_list_query_count_does_not_grow_with_platforms(self):
        self._make_platforms(3)
        baseline = self.count_queries(PLATFORMS_URL)

        self._make_platforms(20, offset=3)

        self.assertEqual(self.count_queries(PLATFORMS_URL), baseline)
