"""DRF 例外處理：把 ValidationError 的欄位錯誤，額外壓平成一行 detail。

為什麼要客製：DRF 預設把 400 回成 {"username": ["此欄位為必填。"]}，但既有前端
（ui/streamlit_app.py、runtime 的 /demo 登入頁）是讀 body["detail"] 來顯示訊息的，
只給欄位錯誤的話它們會退回顯示「失敗（HTTP 400）」，使用者看不到原因。

這裡兩種都給：detail 給人看（前端沿用不必改），errors 給程式看（前端未來要做
欄位級標紅時可以直接用）。掛在 REST_FRAMEWORK["EXCEPTION_HANDLER"]，全站生效。
"""
from __future__ import annotations

from rest_framework.exceptions import ValidationError
from rest_framework.views import exception_handler as drf_exception_handler


def _flatten(errors, prefix: str = "") -> list[str]:
    """把巢狀錯誤結構壓成 ["username：此欄位為必填。", ...]。"""
    if isinstance(errors, dict):
        messages: list[str] = []
        for field, value in errors.items():
            label = "" if field == "non_field_errors" else str(field)
            messages += _flatten(value, label or prefix)
        return messages
    if isinstance(errors, list):
        messages = []
        for item in errors:
            messages += _flatten(item, prefix)
        return messages
    return [f"{prefix}：{errors}" if prefix else str(errors)]


def api_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is not None and isinstance(exc, ValidationError):
        messages = _flatten(response.data)
        response.data = {
            "detail": "；".join(messages) if messages else "輸入格式有誤。",
            "errors": exc.detail,
        }
    return response
