"""模組二的 API view：操作者 auth/me + 終端使用者 end-auth（register / login）。"""
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

import jwt
from django.conf import settings
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import EndUser
from .permissions import roles_of
from .serializers import EndUserLoginSerializer, EndUserRegisterSerializer


def issue_end_user_token(end_user) -> str:
    """簽一個給終端使用者用的 JWT（與 runtime 共用 TOKEN_SECRET，HS256，7 天）。"""
    now = datetime.now(dt_timezone.utc)
    return jwt.encode(
        {"end_user_id": end_user.id, "username": end_user.username, "type": "end_user",
         "iat": now, "exp": now + timedelta(days=7)},
        settings.TOKEN_SECRET, algorithm="HS256",
    )


class MeView(APIView):
    """回傳目前登入操作者的基本資料與角色，用來確認 JWT 帶 token 後能通過認證。"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        u = request.user
        return Response({
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "is_superuser": u.is_superuser,
            "roles": sorted(roles_of(u)),
        })


class EndUserRegisterView(APIView):
    """終端使用者自助註冊（公開）。成功直接回 token（等同註冊即登入）。"""

    permission_classes = [AllowAny]

    def post(self, request):
        ser = EndUserRegisterSerializer(data=request.data)
        ser.is_valid(raise_exception=True)  # 缺欄位 / 帳號重複 → 400（含 detail 與欄位級 errors）
        u = ser.save()
        return Response({"end_user_id": u.id, "username": u.username,
                         "display_name": u.display_name, "token": issue_end_user_token(u)},
                        status=201)


class EndUserLoginView(APIView):
    """終端使用者登入（公開）。比對 username + 密碼，成功回 token + 基本資料。"""

    permission_classes = [AllowAny]

    def post(self, request):
        ser = EndUserLoginSerializer(data=request.data)
        ser.is_valid(raise_exception=True)  # 沒填 → 400；帳密錯 → 下面回 401（兩者要分得開）
        username = ser.validated_data["username"]
        password = ser.validated_data["password"]
        try:
            u = EndUser.objects.get(username=username)
        except EndUser.DoesNotExist:
            return Response({"detail": "帳號或密碼錯誤"}, status=401)
        if u.status != "active" or not u.check_password(password):
            return Response({"detail": "帳號或密碼錯誤"}, status=401)
        u.last_login_at = timezone.now()
        u.save(update_fields=["last_login_at"])
        return Response({"end_user_id": u.id, "username": u.username,
                         "display_name": u.display_name, "token": issue_end_user_token(u)})
