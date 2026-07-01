"""RBAC：以 Django Group 當角色（admin / editor / viewer）。

供 M2 之後的 DRF ViewSet 套用：
- IsAdminRole         ：僅 admin（操作者、金鑰、稽核）
- IsEditorOrAdmin     ：editor 以上（一般寫入）
- RoleBasedReadWrite  ：讀＝viewer 以上、寫＝editor 以上
superuser 一律視為 admin。
"""
from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS, BasePermission

ADMIN, EDITOR, VIEWER = "admin", "editor", "viewer"


def roles_of(user) -> set[str]:
    if not user or not user.is_authenticated:
        return set()
    names = set(user.groups.values_list("name", flat=True))
    if user.is_superuser:
        names.add(ADMIN)
    return names


class IsAdminRole(BasePermission):
    message = "需要 admin 角色。"

    def has_permission(self, request, view) -> bool:
        return ADMIN in roles_of(request.user)


class IsEditorOrAdmin(BasePermission):
    message = "需要 editor 或 admin 角色。"

    def has_permission(self, request, view) -> bool:
        return bool({ADMIN, EDITOR} & roles_of(request.user))


class RoleBasedReadWrite(BasePermission):
    """讀（SAFE methods）：viewer 以上；寫：editor 以上。"""

    def has_permission(self, request, view) -> bool:
        roles = roles_of(request.user)
        if request.method in SAFE_METHODS:
            return bool({ADMIN, EDITOR, VIEWER} & roles)
        return bool({ADMIN, EDITOR} & roles)
