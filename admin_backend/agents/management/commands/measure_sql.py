"""量測 SQL 優化效果：N+1 查詢次數（改前/改後）+ Conversation 列表 EXPLAIN。

用法：  python manage.py measure_sql
特性：  所有種子資料都在一個 transaction 內建立，量測完 rollback，**不會污染資料庫**。

"""
from __future__ import annotations

import datetime

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from agents.models import Agent, Skill, SourceConfig, SourcePlatform
from agents.serializers import AgentSerializer, SourcePlatformSerializer
from memory.models import Conversation

N_AGENTS = 30
N_SKILLS = 5
N_PLATFORMS = 20
N_CONFIGS = 6
N_CONVERSATIONS = 5000
PAGE_SIZE = 50


class Command(BaseCommand):
    help = "量測 N+1 修正與索引效果（種子資料在交易內建立、結束 rollback）"

    def handle(self, *args, **opts):
        with transaction.atomic():
            self._seed()

            with connection.cursor() as c:
                c.execute("SELECT VERSION()")
                self.stdout.write(f"\nMySQL 版本：{c.fetchone()[0]}")

            self._n1("Agent 列表（含 skills）",
                     Agent.objects.filter(name__startswith="perf_agent_"),
                     "skills", AgentSerializer)
            self._n1("SourcePlatform 列表（含 configs）",
                     SourcePlatform.objects.filter(name__startswith="perf_pf_"),
                     "configs", SourcePlatformSerializer)
            self._explain_conversation()

            transaction.set_rollback(True)  # 還原種子資料，DB 不留痕跡
        self.stdout.write(self.style.SUCCESS("\n✓ 量測完成（種子資料已 rollback）"))

    # ── 種子 ──
    def _seed(self):
        skills = [Skill.objects.create(name=f"perf_skill_{i}", description="x",
                                       handler_key="k", json_schema={}) for i in range(N_SKILLS)]
        for a in range(N_AGENTS):
            ag = Agent.objects.create(name=f"perf_agent_{a}", system_prompt="x", model="gpt-4.1")
            ag.skills.set(skills)
        for p in range(N_PLATFORMS):
            pf = SourcePlatform.objects.create(name=f"perf_pf_{p}", display_name="x",
                                               adapter_key="k", kind="vector")
            SourceConfig.objects.bulk_create(
                [SourceConfig(platform=pf, key=f"k{i}", value="1") for i in range(N_CONFIGS)])
        now = timezone.now()
        Conversation.objects.bulk_create([
            Conversation(sid=f"perf_sid_{i}", title=f"t{i}",
                         last_active_at=now - datetime.timedelta(minutes=i))
            for i in range(N_CONVERSATIONS)])

    # ── N+1：同一份資料，無 prefetch vs 有 prefetch 的查詢次數 ──
    def _n1(self, title, base_qs, rel, serializer_cls):
        with CaptureQueriesContext(connection) as before:
            _ = serializer_cls(base_qs, many=True).data
        with CaptureQueriesContext(connection) as after:
            _ = serializer_cls(base_qs.prefetch_related(rel), many=True).data
        b, a = len(before.captured_queries), len(after.captured_queries)
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n[N+1] {title}"))
        self.stdout.write(f"  改前（無 prefetch）：{b} 次查詢")
        self.stdout.write(f"  改後（prefetch_related('{rel}')）：{a} 次查詢")
        self.stdout.write(f"  → 減少 {b - a} 次（{b} → {a}）")

    # ── 索引：同樣 5000 列、同樣分頁查詢，比較「有/無 conv_list_idx」的執行計畫 ──
    def _explain_conversation(self):
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n[索引] Conversation 分頁列表 EXPLAIN（{N_CONVERSATIONS} 列，LIMIT {PAGE_SIZE}）"))
        tail = ("FROM conversation {hint} WHERE is_deleted = 0 "
                "ORDER BY last_active_at DESC, created_at DESC LIMIT %s")
        with connection.cursor() as c:
            c.execute("EXPLAIN FORMAT=TREE SELECT * " + tail.format(hint="IGNORE INDEX (conv_list_idx)"),
                      [PAGE_SIZE])
            before = "\n".join(r[0] for r in c.fetchall())
            c.execute("EXPLAIN FORMAT=TREE SELECT * " + tail.format(hint=""), [PAGE_SIZE])
            after = "\n".join(r[0] for r in c.fetchall())
        self.stdout.write("\n  改前（IGNORE INDEX，等同還沒加索引）：")
        self.stdout.write(self._indent(before))
        self.stdout.write("\n  改後（走 conv_list_idx）：")
        self.stdout.write(self._indent(after))

    @staticmethod
    def _indent(text):
        return "\n".join("    " + ln for ln in text.splitlines())
