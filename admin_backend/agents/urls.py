"""模組一路由：agents / skills / source-platforms（DRF router）。"""
from rest_framework.routers import DefaultRouter

from .views import AgentViewSet, SkillViewSet, SourcePlatformViewSet

router = DefaultRouter()
router.register("agents", AgentViewSet)
router.register("skills", SkillViewSet)
router.register("source-platforms", SourcePlatformViewSet)

urlpatterns = router.urls
