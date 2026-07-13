"""共用 DRF 元件：typed key-value 設定的輸入驗證。

source_config（模組一）與 user_preference（模組四）是同一種形狀——{key, value, value_type}，
DB 的 value 一律存字串，由 runtime 依 value_type 轉型。兩邊共用同一套驗證，規則才不會各寫一份而漂移。
"""
from __future__ import annotations

import json

from rest_framework import serializers

# 與 runtime 的 app/config_repo.py::_CASTERS 同一套轉型規則。
# 在這裡先轉一次是為了「說是 int 就得真的是 int」——不然壞值會一路寫進 DB，
# 等 runtime 讀出來轉型失敗、靜靜地退回原字串，變成很難查的設定漂移。
_CASTERS = {
    "str": str,
    "int": int,
    "float": float,
    "bool": lambda v: str(v).strip().lower() in ("1", "true", "yes", "on"),
    "json": json.loads,
}
VALUE_TYPES = tuple(_CASTERS)


def as_item_list(data, wrapper_key: str):
    """把 body 正規化成 list：接受裸 list，也接受 {"configs": [...]} 這種包一層的。

    型別不對時原樣回傳，交給 serializer(many=True) 去回報成 400——
    不要在 view 裡自己 .get()，那會在 body 是字串時撞 AttributeError 變成 500。
    """
    if isinstance(data, dict):
        return data.get(wrapper_key, [])
    return data


class TypedValueField(serializers.Field):
    """設定值欄位：接受任何 JSON 型別，正規化成字串（DB 的 value 是 CharField）。

    list/dict 走 json.dumps 而不是 str()——Python repr 會產出單引號的 "['dcard']"，
    runtime 的 json.loads 解不開，value_type=json 的偏好就會退化成一個字串。
    """

    def __init__(self, max_length: int | None = None, **kwargs):
        self.max_length = max_length
        kwargs.setdefault("required", False)
        kwargs.setdefault("default", "")
        super().__init__(**kwargs)

    def to_internal_value(self, data) -> str:
        if data is None:
            text = ""
        elif isinstance(data, bool):
            text = "true" if data else "false"
        elif isinstance(data, (dict, list)):
            text = json.dumps(data, ensure_ascii=False)
        else:
            text = str(data)
        if self.max_length is not None and len(text) > self.max_length:
            raise serializers.ValidationError(
                f"長度不可超過 {self.max_length} 字元（目前 {len(text)}）。")
        return text

    def to_representation(self, value) -> str:
        return value


class TypedKeyValueSerializer(serializers.Serializer):
    """{key, value, value_type} 的 upsert 輸入。子類覆寫 value 以套用各自的 max_length。"""

    key = serializers.CharField(max_length=64)
    value = TypedValueField()
    value_type = serializers.ChoiceField(choices=VALUE_TYPES, default="str")

    def validate(self, attrs: dict) -> dict:
        value, value_type = attrs["value"], attrs["value_type"]
        try:
            _CASTERS[value_type](value)
        except (ValueError, TypeError):
            raise serializers.ValidationError(
                {"value": f"「{value}」不是合法的 {value_type}。"}) from None
        return attrs
