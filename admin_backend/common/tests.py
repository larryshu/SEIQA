"""OpenAPI schema 與 Swagger UI 的測試。

最有價值的是 test_schema_generates_without_warnings：schema 是從 code 產生的，
只要 view 少了型別標註、或自訂欄位 drf-spectacular 看不懂，它就會發警告——
把「零警告」變成測試，等於強迫文件跟著實作一起維護，不會慢慢腐爛。
"""
from __future__ import annotations

import io
from contextlib import redirect_stderr

from django.core.management import call_command
from testutils import RoleAPITestCase

SCHEMA_URL = "/api/schema/"
DOCS_URL = "/api/docs/"


class SchemaEndpointTests(RoleAPITestCase):
    def test_schema_requires_authentication(self):
        """文件跟 API 同一套權限，不對外裸奔——登入後台後靠 SessionAuthentication 就看得到。"""
        self.as_anonymous()

        self.assertEqual(self.client.get(SCHEMA_URL).status_code, 401)

    def test_viewer_can_fetch_the_schema(self):
        self.as_role("viewer")

        resp = self.client.get(SCHEMA_URL)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("openapi", resp.content.decode("utf-8"))

    def test_swagger_ui_renders(self):
        self.as_role("viewer")

        resp = self.client.get(DOCS_URL)

        self.assertEqual(resp.status_code, 200)


class SchemaContentTests(RoleAPITestCase):
    def test_schema_generates_without_warnings(self):
        """有警告就代表某個端點的文件在騙人（drf-spectacular 猜不出型別時會退回 string）。"""
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            call_command("spectacular", "--fail-on-warn", stdout=io.StringIO())

        self.assertNotIn("Warning", stderr.getvalue())

    def test_every_module_is_documented(self):
        self.as_role("viewer")

        body = self.client.get(SCHEMA_URL).content.decode("utf-8")

        for path in ("/api/v1/agents/", "/api/v1/conversations/",
                     "/api/v1/system-settings/", "/api/v1/end-auth/login/"):
            self.assertIn(path, body)
