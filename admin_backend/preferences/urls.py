"""模組四路由：system-settings（DRF router）＋ end-users/{id}/preferences。"""
from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import EndUserPreferencesView, SystemSettingViewSet

router = DefaultRouter()
router.register("system-settings", SystemSettingViewSet)

urlpatterns = router.urls + [
    path("end-users/<int:end_user_id>/preferences/", EndUserPreferencesView.as_view(),
         name="end-user-preferences"),
]
