"""根路由：Django Admin + DRF API（/api/v1/）+ OpenAPI 文件（/api/docs/）。"""
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("accounts.urls")),
    path("api/v1/", include("agents.urls")),
    path("api/v1/", include("preferences.urls")),
    path("api/v1/", include("memory.urls")),
    # OpenAPI：schema 由 code 自動產生，不會與實作脫節。
    # 兩者都沿用全域權限（IsAuthenticated）——先登入 /admin/，SessionAuthentication 就帶得過去。
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]
