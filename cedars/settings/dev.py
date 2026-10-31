"""A developer's machine: debug pages, a fixed secret, console email. `docker compose up` sets DATABASE_URL to its own PostgreSQL."""

from .base import *  # noqa: F403
from .base import env, env_bool

ENVIRONMENT = "dev"
DEBUG = True
SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-not-a-secret")
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]", "0.0.0.0"]
EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", "localhost")
EMAIL_PORT = int(env("EMAIL_PORT", "1025"))
STORAGES = {**STORAGES, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}  # noqa: F405
WHITENOISE_USE_FINDERS = True  # serve app static files without collectstatic
WHITENOISE_AUTOREFRESH = True
IMPACT_PROFILES_INLINE_JOBS = env("IMPACT_PROFILES_INLINE_JOBS", "1") == "1"  # no separate worker needed on a laptop
# A laptop demo may sign in with a password alone: CEDARS_REQUIRE_MFA=0 (on by default; base.py keeps it on everywhere else).
CEDARS_REQUIRE_MFA = env_bool("CEDARS_REQUIRE_MFA", True)
DATA_DOWNLOAD_INLINE_JOBS = env("DATA_DOWNLOAD_INLINE_JOBS", "1") == "1"  # background downloads made in the request on a laptop
