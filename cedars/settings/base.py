"""Settings every environment shares. Anything that differs between machines comes from an environment variable, never a file in git."""

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

import cedars

BASE_DIR = Path(__file__).resolve().parent.parent.parent
VERSION = cedars.__version__


def env(name: str, default: str | None = None) -> str:
    """An environment variable, or the default; no default means it is required."""
    value = os.environ.get(name, default)
    if value is None:
        raise ImproperlyConfigured(f"set the environment variable {name}")
    return value


def env_list(name: str, default: str = "") -> list[str]:
    return [v.strip() for v in env(name, default).split(",") if v.strip()]


def env_bool(name: str, default: bool = False) -> bool:
    return env(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


# Which deployment this is: dev, test, sandbox, staging or prod. The sandbox shows a banner on every page (BR-11.2).
ENVIRONMENT = "base"

SECRET_KEY = env("DJANGO_SECRET_KEY", "")
DEBUG = False
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    # shared services (cedars/services), then modules (cedars/modules); see docs/ARCHITECTURE.md
    "cedars.core",  # first, so its templates (allauth's layout and form elements) win
    "cedars.services.accounts",
    "cedars.services.audit",
    "cedars.services.registry",
    "cedars.services.dictionary",
    "cedars.services.rules",
    "cedars.services.submissions",
    "cedars.services.refdata",
    "cedars.modules.administration",
    "cedars.modules.impact_profiles",
    "allauth",
    "allauth.account",
    "allauth.mfa",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.openid_connect",
]

AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "cedars.core.middleware.SecurityHeadersMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "cedars.services.accounts.middleware.ActiveOrganisationMiddleware",
    "cedars.services.accounts.middleware.RequireMFAMiddleware",
    "cedars.services.audit.middleware.AuditContextMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "cedars.urls"
WSGI_APPLICATION = "cedars.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "cedars.core.context_processors.platform",
            ],
        },
    },
]

# PostgreSQL only: the audit trail's append-only trigger and the registry's effective-date ranges need it.
DATABASES = {"default": dj_database_url.parse(env("DATABASE_URL", "postgres://cedars:cedars@localhost:5432/cedars"), conn_max_age=60)}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

# Sign-in (decision F): the platform directory with MFA for everyone; organisations that can federate sign in through their own
# directory as OpenID Connect providers (SOCIALACCOUNT_PROVIDERS["openid_connect"]["APPS"], one per organisation, from the environment).
LOGIN_URL = "account_login"
LOGIN_REDIRECT_URL = "administration:account"
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE = True
ACCOUNT_LOGIN_ON_EMAIL_CONFIRMATION = False
ACCOUNT_RATE_LIMITS = {"login_failed": "10/m/ip,5/5m/key"}
MFA_SUPPORTED_TYPES = ["totp", "recovery_codes", "webauthn"]
MFA_TOTP_ISSUER = "CEDARS"
MFA_PASSKEY_LOGIN_ENABLED = False
SOCIALACCOUNT_ONLY = False
SOCIALACCOUNT_EMAIL_AUTHENTICATION = False  # a directory's email claim never signs into an existing platform account by itself
SOCIALACCOUNT_PROVIDERS = {"openid_connect": {"APPS": []}}
CEDARS_REQUIRE_MFA = True

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/Los_Angeles"
USE_I18N = False
USE_TZ = True

DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # larger uploads spill to a temporary file

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_ROOT = env("MEDIA_ROOT", str(BASE_DIR / "media"))  # default storage on a developer's machine; S3 elsewhere
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

# Same-origin scripts and styles only (as ReCET's web app): vendored libraries, never a CDN.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)

# Impact profiles module. The restricted eTRM database is read with a read-only role; until we have access, a fixture file stands in.
ETRM_DATABASE_URL = env("ETRM_DATABASE_URL", "")
ETRM_SCHEMA = env("ETRM_SCHEMA", "etrm")
ETRM_FIXTURE = env("ETRM_FIXTURE", "")
LOAD_SHAPE_FIRST_START = env("LOAD_SHAPE_FIRST_START", "2027-01-01")  # earlier measure versions keep DEER impact profile IDs
LOAD_SHAPE_MAX_UPLOAD_MB = int(env("LOAD_SHAPE_MAX_UPLOAD_MB", "50"))
LOAD_SHAPE_MAX_UNZIPPED_MB = int(env("LOAD_SHAPE_MAX_UNZIPPED_MB", "1024"))
IMPACT_PROFILE_TERRITORIES = env_list("IMPACT_PROFILE_TERRITORIES", "PGE,SCE,SCG,SDGE")  # a profile with no territory is written for each
IMPACT_PROFILES_INLINE_JOBS = env_bool("IMPACT_PROFILES_INLINE_JOBS", False)  # development: run queued jobs in the request

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
}
