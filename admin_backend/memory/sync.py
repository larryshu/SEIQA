"""把 Qdrant collection 的統計同步進 memory_collection（API 與 Admin action 共用）。"""
from __future__ import annotations

import json
import urllib.request

from django.conf import settings
from django.utils import timezone


def sync_collection(col) -> dict:
    """打 Qdrant REST 取 points_count / vector size，更新並存回。Qdrant 不可用 → status=unknown。"""
    base = getattr(settings, "QDRANT_URL", "http://localhost:7333").rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/collections/{col.name}", timeout=5) as resp:
            result = json.loads(resp.read().decode("utf-8")).get("result", {})
        col.point_count = result.get("points_count")
        vectors = (result.get("config", {}).get("params", {}).get("vectors", {})) or {}
        if isinstance(vectors, dict):
            col.vector_size = vectors.get("size") or (
                next(iter(vectors.values()), {}) if vectors else {}
            ).get("size")
        col.status = "green"
        col.last_synced_at = timezone.now()
        col.save()
        return {"name": col.name, "point_count": col.point_count,
                "vector_size": col.vector_size, "status": col.status}
    except Exception as e:  # noqa: BLE001 — Qdrant 沒開/不可達就標 unknown
        col.status = "unknown"
        col.last_synced_at = timezone.now()
        col.save()
        return {"name": col.name, "status": "unknown", "error": str(e)}
