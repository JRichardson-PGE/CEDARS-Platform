"""pytest: PostgreSQL (DATABASE_URL, as CI's service container), fast password hashing, email kept in memory."""

from .base import *  # noqa: F403
from .base import DATABASES

ENVIRONMENT = "test"
SECRET_KEY = "test-only-not-a-secret"
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
STORAGES = {**STORAGES, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}  # noqa: F405
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
FRONT_PAGE_REFRESH_IN_BACKGROUND = False  # remade in the request, inside the test transaction
REST_FRAMEWORK = {**REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {"user": "100000/minute", "anon": "100000/minute"}}  # noqa: F405
WHITENOISE_USE_FINDERS = True  # serve app static files without collectstatic
WHITENOISE_AUTOREFRESH = True
# The vault beside the core database on the same server unless VAULT_DATABASE_URL names one (the test runner creates test_<name>).
if "vault" not in DATABASES:
    DATABASES["vault"] = {**DATABASES["default"], "NAME": f"{DATABASES['default']['NAME']}_vault"}
# Rules run in this process unless a test hands them to a worker; check jobs go through the test's own connection (in its transaction).
DATABASES.pop("jobs", None)
RULES_RUN_IN_WORKER = False
RULES_JOBS_DATABASE = "default"
