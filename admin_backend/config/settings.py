"""
Django settings for config project (社群輿情智能問答 後台).

DB 連線與機密從 admin_backend/.env 讀（python-dotenv）。
詳見 docs/admin_backend_spec.md。
"""

import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# 讀 admin_backend/.env（DB 連線、SECRET_KEY 等機密；不進版控）
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-xjro^+^))2^f+ivlai+h)hj%0^-bh^0a1!8ge#z_hddcf8@qg5",
)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = _bool("DJANGO_DEBUG", True)

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # 3rd party
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "django_filters",     # 對話列表的過濾 / 搜尋
    "drf_spectacular",    # OpenAPI schema → /api/docs/
    # local apps（四模組）
    "accounts",
    "agents",
    "memory",
    "preferences",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Database — MySQL（utf8mb4）；連線資訊從 .env 讀
# https://docs.djangoproject.com/en/5.1/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": os.environ.get("DB_NAME", "crawl_agent"),
        "USER": os.environ.get("DB_USER", "root"),
        "PASSWORD": os.environ.get("DB_PASSWORD", ""),
        "HOST": os.environ.get("DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("DB_PORT", "3306"),
        "CONN_MAX_AGE": 60,  # 連線重用（§11 連線池）
        "OPTIONS": {
            "charset": "utf8mb4",
            "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
        },
    }
}


# Password validation
# https://docs.djangoproject.com/en/5.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# Django REST Framework + JWT（操作者認證，細節於 M1 完成）
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",  # 開發期 browsable API 用
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    # 400 的 body 同時帶 detail（給既有前端顯示）與 errors（欄位級）；見 config/exceptions.py
    "EXCEPTION_HANDLER": "config.exceptions.api_exception_handler",
    # 過濾 / 搜尋 / 排序：掛成全域 backend，但只有宣告了 filterset_fields、search_fields、
    # ordering_fields 的 viewset 才真的吃得到——沒宣告的等於沒掛，不會憑空多出查詢參數。
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    # 限流：ScopedRateThrottle 只作用在有標 throttle_scope 的 view（見 accounts/views.py），
    # 其餘端點不受影響。認證端點是唯一對外開放（AllowAny）的入口，不限流就等於開放暴力破解。
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.ScopedRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "end_auth_login": "5/min",      # 終端使用者登入：人打錯密碼不會一分鐘超過五次
        "end_auth_register": "10/hour",  # 註冊本來就是稀有動作
        "admin_login": "10/min",         # 後台操作者登入（JWT 簽發）
    },
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# 限流的計數存在 cache。預設 LocMemCache 是「每個 process 各一份」——開多個 worker 時
# 實際額度會變成 N 倍。要真的擋住，正式環境應換成共用的 Redis：
#   "BACKEND": "django.core.cache.backends.redis.RedisCache",
#   "LOCATION": os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/1"),
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "seiqa-admin",
    }
}

# OpenAPI 文件（/api/schema/、/api/docs/）。
SPECTACULAR_SETTINGS = {
    "TITLE": "社群輿情智能問答 — 後台 API",
    "DESCRIPTION": "agent 設定 / 帳戶 / 記憶 / 偏好四模組。schema 由 code 自動產生，"
                   "不會與實作脫節。權限模型見 accounts/permissions.py（viewer 讀、editor 寫、admin 全權）。",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,  # schema 端點自己不要出現在 schema 裡
    "SCHEMA_PATH_PREFIX": "/api/v1",
    # 一定要覆寫：drf-spectacular 的 SERVE_PERMISSIONS 預設是 AllowAny，會蓋掉上面全域的
    # IsAuthenticated——照預設裝下去，整份 API 結構（含所有端點與欄位）是對外裸奔的。
    # 改成沿用全域權限：登入 /admin/ 後靠 SessionAuthentication 就看得到。
    "SERVE_PERMISSIONS": ["rest_framework.permissions.IsAuthenticated"],
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
}


# Internationalization
# https://docs.djangoproject.com/en/5.1/topics/i18n/

LANGUAGE_CODE = "zh-hant"

TIME_ZONE = "Asia/Taipei"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
STATIC_URL = "static/"

# Default primary key field type
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Qdrant（memory_collection sync 用；與 runtime .env 的 QDRANT_URL 對齊）
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:7333")

# FastAPI runtime（後台「通知 runtime 重載設定」用）
RUNTIME_URL = os.environ.get("RUNTIME_URL", "http://localhost:8001")

# 終端使用者登入 token 簽章密鑰（與 runtime .env 共用；end-auth 簽 JWT 用）
TOKEN_SECRET = os.environ.get("TOKEN_SECRET", "")
