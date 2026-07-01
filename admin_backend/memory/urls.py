"""模組三路由：conversations / memory-collections（DRF router）。"""
from rest_framework.routers import DefaultRouter

from .views import ConversationViewSet, MemoryCollectionViewSet

router = DefaultRouter()
router.register("conversations", ConversationViewSet)
router.register("memory-collections", MemoryCollectionViewSet)

urlpatterns = router.urls
