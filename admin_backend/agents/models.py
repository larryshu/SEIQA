"""模組一：Skill / Agent — agent 人設與參數、skill 定義、來源平台與其參數。

取代 runtime 寫死的部分（見 docs/admin_backend_spec.md §8）：
- agent          ← app/agent.py 的 SYSTEM_PROMPT / MAX_TOOL_ROUNDS / model
- skill           ← app/tools.py 的 TOOLS
- source_platform ← app/sources.py 的 REGISTRY（啟用/順序）
- source_config   ← 各平台檢索參數（top_k / min_score / expand_n / PTT 預算…）
表名以 db_table 對齊規格，方便 FastAPI runtime 直接讀。
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class Agent(models.Model):
    """一個 agent 角色（人設 + 模型 + 參數）。全系統同時只有一個 is_active。"""

    name = models.CharField(max_length=64, unique=True)
    description = models.CharField(max_length=255, blank=True)
    system_prompt = models.TextField()
    model = models.CharField(max_length=64)  # 例：gpt-4.1
    temperature = models.DecimalField(max_digits=3, decimal_places=2, default=0.70)
    max_tool_rounds = models.SmallIntegerField(default=1)
    is_active = models.BooleanField(default=False)
    version = models.IntegerField(default=1)
    skills = models.ManyToManyField("Skill", through="AgentSkill", related_name="agents", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="created_agents",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "agent"

    def __str__(self) -> str:
        return f"{self.name}{' (active)' if self.is_active else ''}"


class Skill(models.Model):
    """一個工具（function-calling）。description 是給 LLM 的『何時該用』觸發條件。"""

    name = models.CharField(max_length=64, unique=True)  # 例：community_search
    display_name = models.CharField(max_length=128, blank=True)
    description = models.TextField()
    json_schema = models.JSONField(default=dict)  # function-calling parameters schema
    handler_key = models.CharField(max_length=64)  # runtime dispatch 對應的內部 key
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "skill"

    def __str__(self) -> str:
        return self.name


class AgentSkill(models.Model):
    """agent ↔ skill 多對多（含排序）。"""

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="agent_skills")
    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name="agent_skills")
    sort_order = models.SmallIntegerField(default=0)

    class Meta:
        db_table = "agent_skill"
        unique_together = ("agent", "skill")
        ordering = ["sort_order"]


class SourcePlatform(models.Model):
    """一個社群來源平台（對應 sources.py 的 adapter）。"""

    name = models.CharField(max_length=32, unique=True)  # dcard / ptt / mobile01 ...
    display_name = models.CharField(max_length=64)  # Dcard 口碑庫 / PTT
    adapter_key = models.CharField(max_length=64)  # runtime adapter key
    kind = models.CharField(max_length=16)  # vector / live_crawl
    is_active = models.BooleanField(default=True)
    sort_order = models.SmallIntegerField(default=0)  # 合併順序
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "source_platform"
        ordering = ["sort_order"]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.name})"


class SourceConfig(models.Model):
    """每平台參數（key-value typed）。例：top_k / min_score / expand_n / time_budget。"""

    platform = models.ForeignKey(SourcePlatform, on_delete=models.CASCADE, related_name="configs")
    key = models.CharField(max_length=64)
    value = models.CharField(max_length=255)
    value_type = models.CharField(max_length=16, default="str")  # int / float / str / bool

    class Meta:
        db_table = "source_config"
        unique_together = ("platform", "key")

    def __str__(self) -> str:
        return f"{self.platform.name}.{self.key}={self.value}"
