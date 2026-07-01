"""根路由：Django Admin + DRF API（/api/v1/）。"""
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("accounts.urls")),
    path("api/v1/", include("agents.urls")),
    path("api/v1/", include("preferences.urls")),
    path("api/v1/", include("memory.urls")),
]
