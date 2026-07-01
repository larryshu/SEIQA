"""建立 admin / editor / viewer 三個角色 Group（RBAC 基礎）。

用法： python manage.py seed_roles
"""
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "建立 admin / editor / viewer 三個角色 Group"

    def handle(self, *args, **options):
        for name in ("admin", "editor", "viewer"):
            _, created = Group.objects.get_or_create(name=name)
            self.stdout.write(("建立" if created else "已存在") + f"：{name}")
        self.stdout.write(self.style.SUCCESS("角色 seed 完成"))
