"""pytest: PostgreSQL (DATABASE_URL, as CI's service container), fast password hashing, email kept in memory."""

from .base import *  # noqa: F403

ENVIRONMENT = "test"
SECRET_KEY = "test-only-not-a-secret"
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
STORAGES = {**STORAGES, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}  # noqa: F405
WHITENOISE_USE_FINDERS = True  # serve app static files without collectstatic
WHITENOISE_AUTOREFRESH = True
