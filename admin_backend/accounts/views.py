"""模組二的 API view：操作者 auth/me + 終端使用者 end-auth（register / login）。

認證端點是全站唯一 AllowAny 的入口，所以也是唯一需要限流的地方：
三支登入/註冊都標了 throttle_scope，額度定義在 settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]。
"""
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

import jwt
from django.conf import settings
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from .models import EndUser
from .permissions import roles_of
from .serializers import (
    EndUserAuthResponseSerializer,
    EndUserLoginSerializer,
    EndUserRegisterSerializer,
    OperatorMeSerializer,
)

BAD_CREDENTIALS = OpenApiResponse(description="帳號或密碼錯誤（不區分是哪個錯，避免帳號列舉）")
THROTTLED = OpenApiResponse(description="請求過於頻繁，被限流擋下（Retry-After 標頭給重試秒數）")


def issue_end_user_token(end_user) -> str:
    """簽一個給終端使用者用的 JWT（與 runtime 共用 TOKEN_SECRET，HS256，7 天）。"""
    now = datetime.now(dt_timezone.utc)
    return jwt.encode(
        {"end_user_id": end_user.id, "username": end_user.username, "type": "end_user",
         "iat": now, "exp": now + timedelta(days=7)},
        settings.TOKEN_SECRET, algorithm="HS256",
    )


def _auth_response(end_user, status: int = 200) -> Response:
    ser = EndUserAuthResponseSerializer({
        "end_user_id": end_user.id,
        "username": end_user.username,
        "display_name": end_user.display_name,
        "token": issue_end_user_token(end_user),
    })
    return Response(ser.data, status=status)


class ThrottledTokenObtainPairView(TokenObtainPairView):
    """後台操作者登入（簽 JWT）。SimpleJWT 原生的 view 沒有限流，包一層補上。

    這支比終端使用者的登入更值得保護——它後面是 admin 權限。
    """

    throttle_scope = "admin_login"


@extend_schema(
    responses=OperatorMeSerializer,
    summary="目前登入的操作者與角色",
    description="帶 JWT 或 Session 呼叫。roles 決定你在其他端點的讀寫權限。",
)
class MeView(APIView):
    """回傳目前登入操作者的基本資料與角色，用來確認 JWT 帶 token 後能通過認證。"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        u = request.user
        ser = OperatorMeSerializer({
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "is_superuser": u.is_superuser,
            "roles": sorted(roles_of(u)),
        })
        return Response(ser.data)


@extend_schema(
    request=EndUserRegisterSerializer,
    responses={201: EndUserAuthResponseSerializer, 429: THROTTLED},
    summary="終端使用者自助註冊",
    description="成功直接回 token，等同註冊即登入。限流 10/hour。",
)
class EndUserRegisterView(APIView):
    """終端使用者自助註冊（公開）。成功直接回 token（等同註冊即登入）。"""

    permission_classes = [AllowAny]
    throttle_scope = "end_auth_register"

    def post(self, request):
        ser = EndUserRegisterSerializer(data=request.data)
        ser.is_valid(raise_exception=True)  # 缺欄位 / 帳號重複 → 400（含 detail 與欄位級 errors）
        return _auth_response(ser.save(), status=201)


@extend_schema(
    request=EndUserLoginSerializer,
    responses={200: EndUserAuthResponseSerializer, 401: BAD_CREDENTIALS, 429: THROTTLED},
    summary="終端使用者登入",
    description="限流 5/min（依來源 IP 計）——這是全站唯一能無限次猜密碼的入口。",
)
class EndUserLoginView(APIView):
    """終端使用者登入（公開）。比對 username + 密碼，成功回 token + 基本資料。"""

    permission_classes = [AllowAny]
    throttle_scope = "end_auth_login"

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
        return _auth_response(u)
