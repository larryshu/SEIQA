"""驗證 Django 簽發的終端使用者 token，取出 end_user_id。

與 Django 共用 settings.token_secret（HS256）。任何問題（沒帶、過期、簽章錯、沒設密鑰、
沒裝 jwt）一律回 None → 視為匿名，聊天照常可用（fail-safe）。
"""
from __future__ import annotations

from .config import settings

try:
    import jwt
except ImportError:  # 沒裝 jwt → 一律匿名
    jwt = None


def end_user_id_from_token(authorization: str | None) -> int | None:
    if not authorization or not settings.token_secret or jwt is None:
        return None
    parts = authorization.split()
    token = parts[1] if (len(parts) == 2 and parts[0].lower() == "bearer") else authorization
    try:
        payload = jwt.decode(token, settings.token_secret, algorithms=["HS256"])
    except Exception:  # noqa: BLE001 — 過期/簽章錯/格式錯都當匿名
        return None
    if payload.get("type") != "end_user":
        return None
    uid = payload.get("end_user_id")
    try:
        return int(uid) if uid is not None else None
    except (ValueError, TypeError):
        return None
