"""模組二路由：JWT 認證端點（對應規格 §7.1）。"""
from django.urls import path
from rest_framework_simplejwt.views import (
    TokenBlacklistView,
    TokenObtainPairView,
    TokenRefreshView,
)

from .views import EndUserLoginView, EndUserRegisterView, MeView

urlpatterns = [
    # 操作者（後台）認證
    path("auth/login/", TokenObtainPairView.as_view(), name="auth-login"),
    path("auth/refresh/", TokenRefreshView.as_view(), name="auth-refresh"),
    path("auth/logout/", TokenBlacklistView.as_view(), name="auth-logout"),
    path("auth/me/", MeView.as_view(), name="auth-me"),
    # 終端使用者（聊天）認證
    path("end-auth/register/", EndUserRegisterView.as_view(), name="end-auth-register"),
    path("end-auth/login/", EndUserLoginView.as_view(), name="end-auth-login"),
]
