"""把 Streamlit 的 ui/.sessions/*.json 匯入 conversation/message（idempotent）。

並確保 dcard_insight 的 memory_collection metadata 列存在。
注意：JSON 沒有逐則時間戳，匯入的 message.created_at 會是匯入當下時間（可接受）。

用法： python manage.py import_sessions
"""
import json

from django.conf import settings
from django.core.management.base import BaseCommand

from memory.models import Conversation, MemoryCollection, Message


def _first_user_content(msgs: list) -> str:
    for m in msgs:
        if m.get("role") == "user":
            return m.get("content", "")
    return msgs[0].get("content", "") if msgs else ""


class Command(BaseCommand):
    help = "匯入 ui/.sessions/*.json 到 conversation/message，並確保 dcard_insight collection 存在"

    def handle(self, *args, **options):
        sessions_dir = settings.BASE_DIR.parent / "ui" / ".sessions"
        imported = skipped = 0

        if sessions_dir.exists():
            for f in sorted(sessions_dir.glob("*.json")):
                sid = f.stem
                try:
                    msgs = json.loads(f.read_text(encoding="utf-8"))
                except (ValueError, OSError) as e:
                    self.stdout.write(f"略過 {sid}：讀檔失敗 {e}")
                    continue
                conv, created = Conversation.objects.get_or_create(
                    sid=sid,
                    defaults={"title": _first_user_content(msgs)[:255],
                              "message_count": len(msgs)},
                )
                if not created and conv.messages.exists():
                    skipped += 1  # 已匯入過
                    continue
                for m in msgs:
                    Message.objects.create(
                        conversation=conv,
                        role=m.get("role", "user"),
                        content=m.get("content", ""),
                        used_tools=m.get("used_tools"),
                        sources=m.get("sources"),
                    )
                conv.message_count = conv.messages.count()
                conv.save(update_fields=["message_count"])
                imported += 1
        else:
            self.stdout.write(f"找不到 {sessions_dir}（沒有可匯入的對話）")

        col, _ = MemoryCollection.objects.get_or_create(
            name="dcard_insight",
            defaults={"display_name": "Dcard 口碑庫", "kind": "dcard", "is_readonly": True,
                      "note": "由 dcard_insight 專案批次建；後台只檢視/同步統計"},
        )
        self.stdout.write(self.style.SUCCESS(
            f"匯入 {imported} 段對話、略過 {skipped} 段；collection: {col.name}"))
