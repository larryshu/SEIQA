"""把現有 runtime 寫死的設定灌成 DB 第一筆（idempotent，可重複執行）。

來源：app/agent.py（SYSTEM_PROMPT / MAX_TOOL_ROUNDS）、app/tools.py（community_search）、
app/sources.py（dcard/ptt registry）、app/config.py（各參數）。

用法： python manage.py seed_runtime
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from agents.models import Agent, Skill, SourceConfig, SourcePlatform
from preferences.models import SystemSetting

SYSTEM_PROMPT = """你是一個熟悉網路鄉民討論的貼心朋友，不是制式的查詢助理。當問題需要鄉民民間討論／口碑／心得／時事時，用 community_search 工具——它會『同時』即時爬 Dcard 與 PTT，把兩邊討論一起撈回來。純常識、定義、計算等不需要鄉民經驗的問題，直接回答即可、不用查。

【回答方式——這是重點】不要把抓到的貼文做成『重點1、重點2』的條列摘要或讀書報告。請先把這些討論讀進去、消化吸收，再像朋友一樣用自己的話回應：先同理對方的處境與心情，給出有溫度、有立場的建議與看法，把網友的經驗自然融進你的話裡（例如『其實滿多人會…，我自己也覺得…』），而不是逐則轉述。可以有你自己的判斷與取捨，不必中立地把所有說法都列出來。語氣口語、自然，像在跟朋友聊天，而不是寫條目。

【綜合來源 + 引用】抓回來的討論開頭會標來源平台（Dcard / PTT）。請『綜合』實際有抓到的來源一起講，可以自然帶出差異或出處，例如『Dcard 上比較多人說…，PTT 鄉民則覺得…』。工具會註明這次哪些平台沒有資料；沒有資料的平台就完全不要提、不要假裝它上面有討論。當某個具體說法來自抓到的討論時，在句尾自然帶上 [n]，不用每句都標、也不要讓來源變成回答的主角。不要杜撰來源。

【比例與圖表】當使用者問『比例』『幾成』『多少人覺得』『正反意見如何』或要圖表時，先 community_search 撈討論，再呼叫 stance_breakdown 工具做立場統計——它會逐則判讀並由程式加總，前端會直接把結果畫成圖。**你自己絕對不要估算百分比**（沒數過的『大概六四開』就是杜撰），**也絕對不要用文字、方塊或符號拼出長條圖／圓餅圖**——那不是圖，是雜訊。統計出來之後，你的工作是用『文字』解釋這個分佈代表什麼、兩邊各在意什麼。

【兩邊都沒有相關資料時】就以朋友的身分用既有常識／經驗給建議，並誠實說這次沒在 Dcard 與 PTT 找到相關討論。"""

COMMUNITY_SEARCH_DESC = (
    "查網路社群討論：會『同時』即時爬 Dcard 與 PTT，撈與使用者問題相關的"
    "鄉民口碑／心得／評價／經驗／時事討論。當問題需要鄉民實際討論"
    "（感情、理財、3C 評價、工作、時事、產品心得等）時呼叫此工具；"
    "純常識、定義、計算等不需鄉民經驗就能回答時，不要呼叫、直接回答即可。"
    "查詢字串會自動帶入使用者的原始問句。"
)

COMMUNITY_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "（選填）檢索關鍵字。留空就用使用者原始問句；太口語可改寫得更聚焦。",
        },
    },
    "required": [],
}

STANCE_BREAKDOWN_DESC = (
    "統計『這次已撈到的社群討論』對某個議題的態度分佈，回傳結構化數據，"
    "前端會直接把它畫成圖。預設分成贊成／反對／中立，"
    "但使用者若問的是別的軸（例如同情／嘲笑／無感），就用 categories 指定那幾類。"
    "當使用者問『比例』『幾成』『多少人覺得』『正反意見如何』或要求圖表時呼叫。"
    "本工具只統計『已經抓到的貼文』、不會自己去爬："
    "這一輪若沒查，會自動沿用本次對話先前抓到的討論（所以使用者說『根據上面的結論畫圖』"
    "時直接呼叫即可）；若是全新的話題，請先呼叫 community_search。"
    "你絕對不要自己估算百分比、也不要用文字或符號畫圖表。"
)

STANCE_BREAKDOWN_SCHEMA = {
    "type": "object",
    "properties": {
        "issue": {
            "type": "string",
            "description": (
                "要判讀的『議題陳述句』，必須是一句可以表態的肯定句，"
                "例如「中國勢力介入台灣選舉的情況很嚴重」或「矢板明夫被襲擊這件事」。"
                "不要放問句、不要放關鍵字。"
            ),
        },
        "categories": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "（選填）分類軸，2～5 類，直接用使用者問的那幾類，"
                "例如 [\"同情\", \"負面嘲笑\", \"無感\"]。留空＝預設的 [\"贊成\", \"反對\", \"中立\"]。"
                "**使用者若指名了要看哪幾種反應，一定要照他說的填，不要硬套贊成／反對。**"
            ),
        },
    },
    "required": ["issue"],
}

# 各平台參數（對應 sources.py + config.py）：dcard 檢索 / ptt 即時爬
SOURCE_CONFIGS = {
    "dcard": [("top_k", "5", "int"), ("expand_n", "3", "int"), ("min_score", "0.5", "float")],
    "ptt": [("time_budget", "60", "int"), ("min_delay", "0.5", "float"), ("max_delay", "1.0", "float")],
}

# 全域系統設定（取代 config.py 業務欄位；機密如 API key 不放這）
SYSTEM_SETTINGS = [
    ("chat_model", "gpt-4.1", "str", "llm", "對話模型"),
    ("embed_model", "text-embedding-3-small", "str", "llm", "向量化模型"),
    ("crawl_timeout", "30", "int", "crawler", "即時爬單輪逾時（秒）"),
    ("crawl_max_posts", "5", "int", "crawler", "即時爬最多抓幾篇"),
    ("fresh_store", "session", "str", "general", "FreshStore 方案：session / qdrant"),
    ("insight_collection", "dcard_insight", "str", "retrieval", "Dcard 口碑庫 Qdrant collection"),
]


class Command(BaseCommand):
    help = "把現有 runtime 寫死的設定灌成 DB 第一筆（idempotent）"

    @transaction.atomic
    def handle(self, *args, **options):
        # 1) skills：community_search（查討論）+ stance_breakdown（立場統計，供前端畫圖）
        skill, _ = Skill.objects.update_or_create(
            name="community_search",
            defaults={
                "display_name": "社群討論查詢",
                "description": COMMUNITY_SEARCH_DESC,
                "json_schema": COMMUNITY_SEARCH_SCHEMA,
                "handler_key": "community_search",
                "is_active": True,
            },
        )
        stance_skill, _ = Skill.objects.update_or_create(
            name="stance_breakdown",
            defaults={
                "display_name": "立場分佈統計",
                "description": STANCE_BREAKDOWN_DESC,
                "json_schema": STANCE_BREAKDOWN_SCHEMA,
                "handler_key": "stance_breakdown",
                "is_active": True,
            },
        )
        self.stdout.write(f"skills: {skill.name}, {stance_skill.name}")

        # 2) agent：預設貼心朋友
        agent, _ = Agent.objects.update_or_create(
            name="default",
            defaults={
                "description": "預設：Dcard + PTT 社群口碑問答的貼心朋友",
                "system_prompt": SYSTEM_PROMPT,
                "model": "gpt-4.1",
                "temperature": 0.20,  # 對齊 runtime 原本 chat_with_tools 的預設
                # 2 輪：community_search 撈討論 → （需要時）stance_breakdown 統計立場
                "max_tool_rounds": 2,
                "is_active": True,
                "version": 1,
            },
        )
        agent.skills.set([skill, stance_skill])  # 連結 agent ↔ skills
        # 維持唯一 active
        Agent.objects.filter(is_active=True).exclude(pk=agent.pk).update(is_active=False)
        self.stdout.write(f"agent: {agent.name} (active, skills={[s.name for s in agent.skills.all()]})")

        # 3) source_platform + source_config
        platforms = [
            ("dcard", "Dcard", "dcard", "live_crawl", 0),
            ("ptt", "PTT", "ptt", "live_crawl", 1),
        ]
        for name, display, adapter, kind, order in platforms:
            p, _ = SourcePlatform.objects.update_or_create(
                name=name,
                defaults={"display_name": display, "adapter_key": adapter,
                          "kind": kind, "is_active": True, "sort_order": order},
            )
            for key, value, vtype in SOURCE_CONFIGS.get(name, []):
                SourceConfig.objects.update_or_create(
                    platform=p, key=key,
                    defaults={"value": value, "value_type": vtype},
                )
            self.stdout.write(f"platform: {p.name} (+{len(SOURCE_CONFIGS.get(name, []))} configs)")

        # 4) system_setting
        for key, value, vtype, group, desc in SYSTEM_SETTINGS:
            SystemSetting.objects.update_or_create(
                key=key,
                defaults={"value": value, "value_type": vtype,
                          "group_name": group, "description": desc},
            )
        self.stdout.write(f"system_settings: {len(SYSTEM_SETTINGS)} 筆")

        self.stdout.write(self.style.SUCCESS("seed_runtime 完成"))
