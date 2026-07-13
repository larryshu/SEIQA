"""模組二 DRF serializers：終端使用者註冊 / 登入的輸入驗證。

輸入一律走 serializer，不在 view 裡直接讀 request.data——少送欄位會得到結構化的 400，
而不是 KeyError 變成 500；帳號/email 重複也在這裡擋掉，不必等 DB 拋 IntegrityError。
"""
from __future__ import annotations

from rest_framework import serializers

from .models import EndUser


class EndUserRegisterSerializer(serializers.Serializer):
    """POST /end-auth/register/。驗證通過即建立帳號（save() → create()）。"""

    username = serializers.CharField(max_length=64)
    password = serializers.CharField(max_length=128, write_only=True)
    display_name = serializers.CharField(max_length=128, required=False, allow_blank=True)
    email = serializers.EmailField(max_length=254, required=False,
                                   allow_blank=True, allow_null=True)

    def validate_username(self, value: str) -> str:
        if EndUser.objects.filter(username=value).exists():
            raise serializers.ValidationError("此帳號已存在")
        return value

    def validate_email(self, value):
        # email 在 model 上是 unique；先擋掉重複，免得 DB 拋 IntegrityError 變成 500
        if value and EndUser.objects.filter(email=value).exists():
            raise serializers.ValidationError("此 email 已被使用")
        return value

    def create(self, validated_data: dict) -> EndUser:
        username = validated_data["username"]
        user = EndUser(
            username=username,
            display_name=validated_data.get("display_name") or username,
            email=validated_data.get("email") or None,  # 空字串要存成 NULL，否則撞 unique
            auth_provider="local",
            status="active",
        )
        user.set_password(validated_data["password"])
        user.save()
        return user


class EndUserLoginSerializer(serializers.Serializer):
    """POST /end-auth/login/。只驗形狀；帳密對不對由 view 判斷並回 401。

    刻意不在 validate() 裡比對密碼：那會讓「帳密錯誤」變成 400，但既有前端
    （Streamlit / demo 頁）是以 401 區分「輸入沒填」與「帳密錯」的。
    """

    username = serializers.CharField(max_length=64)
    password = serializers.CharField(max_length=128, write_only=True)


class EndUserAuthResponseSerializer(serializers.Serializer):
    """註冊 / 登入成功的回應。實際拿來組回應，順便當成 OpenAPI 的 response schema。"""

    end_user_id = serializers.IntegerField()
    username = serializers.CharField()
    display_name = serializers.CharField()
    token = serializers.CharField(help_text="HS256 JWT，7 天效期；runtime 用同一把 TOKEN_SECRET 驗章")


class OperatorMeSerializer(serializers.Serializer):
    """GET /auth/me/ 的回應：目前登入的操作者與他的角色。"""

    id = serializers.IntegerField()
    username = serializers.CharField()
    email = serializers.CharField()
    is_superuser = serializers.BooleanField()
    roles = serializers.ListField(child=serializers.CharField(),
                                  help_text="admin / editor / viewer；superuser 自動視為 admin")
