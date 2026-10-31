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
    "django.forms",  # after cedars.core, whose django/forms/field.html overrides Django's (FORM_RENDERER below)
    "cedars.services.accounts",
    "cedars.services.audit",
    "cedars.services.registry",
    "cedars.services.dictionary",
    "cedars.services.rules",
    "cedars.services.submissions",
    "cedars.services.refdata",
    "cedars.services.notifications",
    "cedars.services.retention",
    "cedars.services.vault",
    "cedars.services.legacy",
    "cedars.services.snapshots",
    "cedars.modules.administration",
    "cedars.modules.impact_profiles",
    "cedars.modules.monthly",
    "cedars.modules.calculations",
    "cedars.modules.programs",
    "cedars.modules.budgets",
    "cedars.modules.claims",
    "cedars.modules.documents",
    "cedars.modules.data",
    "allauth",
    "allauth.account",
    "allauth.mfa",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.openid_connect",
    "rest_framework",
    "drf_spectacular",
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
    "django.contrib.messages.middleware.MessageMiddleware",  # before RequireMFAMiddleware, whose redirect carries an error message
    "cedars.services.accounts.middleware.ActiveOrganisationMiddleware",
    "cedars.services.accounts.middleware.RequireMFAMiddleware",
    "cedars.services.audit.middleware.AuditContextMiddleware",
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
# The PII vault (decision G): its own database, set only where PII is held (never the sandbox). Without it PII uploads are refused.
if env("VAULT_DATABASE_URL", ""):
    DATABASES["vault"] = dj_database_url.parse(env("VAULT_DATABASE_URL"), conn_max_age=60)
DATABASE_ROUTERS = ["cedars.services.vault.router.VaultRouter"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

# Sign-in (decision F): the platform directory with MFA for everyone; organizations that can federate sign in through their own
# directory as OpenID Connect providers (SOCIALACCOUNT_PROVIDERS["openid_connect"]["APPS"], one per organization, from the environment).
LOGIN_URL = "account_login"
LOGIN_REDIRECT_URL = "administration:account"
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE = True
ACCOUNT_LOGIN_ON_EMAIL_CONFIRMATION = False
ACCOUNT_RATE_LIMITS = {"login_failed": "10/m/ip,5/5m/key"}
# The platform's own wording on allauth's forms, pages and emails (cedars/core/templates/account, mfa); the subject prefix replaces
# allauth's "[<site domain>] ". CEDARS_SUPPORT_EMAIL is the address the sign-in pages and Help give for support (blank = none shown);
# CEDARS_FEEDBACK_URL a feedback form or survey that Help links to (blank = an email to the support address). Both are set per
# environment, never written into the code.
ACCOUNT_FORMS = {
    "login": "cedars.services.accounts.forms.LoginForm",
    "signup": "cedars.services.accounts.forms.SignupForm",
    "reset_password": "cedars.services.accounts.forms.ResetPasswordForm",
}
ACCOUNT_EMAIL_SUBJECT_PREFIX = "[CEDARS] "
CEDARS_SUPPORT_EMAIL = env("CEDARS_SUPPORT_EMAIL", "")
CEDARS_FEEDBACK_URL = env("CEDARS_FEEDBACK_URL", "")

# One cache that every web process shares, so rate limits (the API's and allauth's sign-in limits) count across all of them: a table
# in the core database (created by migration core 0001; `manage.py createcachetable` makes it again).
CACHES = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "cedars_cache"}}
MFA_SUPPORTED_TYPES = ["totp", "recovery_codes", "webauthn"]
MFA_TOTP_ISSUER = "CEDARS"
MFA_PASSKEY_LOGIN_ENABLED = False
SOCIALACCOUNT_ONLY = False
SOCIALACCOUNT_EMAIL_AUTHENTICATION = False  # a directory's email claim never signs into an existing platform account by itself
SOCIALACCOUNT_PROVIDERS = {"openid_connect": {"APPS": []}}
CEDARS_REQUIRE_MFA = True
CEDARS_BASE_URL = env("CEDARS_BASE_URL", "http://localhost:8000")  # links in emails
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "cedars@localhost")

# The API (/api/v1, decision J): API tokens for members' systems, the session for the platform's own pages' scripts. JSON only (no
# browsable API pages); the OpenAPI description is at /api/v1/schema. Pages and the API call the same service functions.
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "cedars.services.accounts.api_auth.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.MultiPartParser",
        "rest_framework.parsers.FormParser",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
    # Rate limits per user (every token and session of one person counts together) and per address for calls without one; over
    # the limit a call gets 429 with Retry-After. Counted in the shared cache below, so every web process sees the same count.
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle", "rest_framework.throttling.UserRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"user": env("CEDARS_API_RATE_USER", "120/minute"), "anon": env("CEDARS_API_RATE_ANON", "30/minute")},
    "NUM_PROXIES": int(env("CEDARS_NUM_PROXIES", "0")),  # proxies in front (prod: 1, the load balancer); 0 = the socket address
}
SPECTACULAR_SETTINGS = {
    "TITLE": "CEDARS API",
    "DESCRIPTION": (
        "The CEDARS platform's API. Authenticate with an API token from your account page: Authorization: Bearer <token>. "
        "Calls are rate limited per user (all of a user's tokens together) and per address without a token; over the limit a call "
        "gets 429 Too Many Requests with a Retry-After header in seconds."
    ),
    "VERSION": "1",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v1",
}
CEDARS_API_TOKEN_MAX_DAYS = int(env("CEDARS_API_TOKEN_MAX_DAYS", "365"))

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

# Form fields render through the project's own field template (cedars/core/templates/django/forms/field.html): one .field block per
# field, so label, hint, errors and control stay together. TemplatesSetting looks in the apps' template directories first.
FORM_RENDERER = "django.forms.renderers.TemplatesSetting"

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_ROOT = env("MEDIA_ROOT", str(BASE_DIR / "media"))  # default storage on a developer's machine; S3 elsewhere
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# Object storage (cedars/core/storage.py): a local folder in development and tests, S3 in production (MinIO stands in locally through
# AWS_ENDPOINT_URL, which boto3 reads itself). Credentials are never settings: the task role in AWS, the environment elsewhere.
CEDARS_STORAGE = {
    "backend": env("CEDARS_STORAGE_BACKEND", "local"),
    "root": env("CEDARS_STORAGE_ROOT", str(BASE_DIR / "var" / "storage")),
    "buckets": {area: env(f"CEDARS_BUCKET_{area.upper()}", "") for area in ("uploads", "results", "refdata", "snapshots")},
}

# Calculations (cedars/modules/calculations): where runs are priced and how they queue.
CALCULATIONS_BACKEND = env("CALCULATIONS_BACKEND", "local")  # local (calculations_worker) | batch (AWS Batch)
CALCULATIONS_WORKER_COMMAND = env("CALCULATIONS_WORKER_COMMAND", "recet worker")  # the local backend's ReCET worker
CALCULATIONS_BATCH_QUEUES = {"small": env("CEDARS_BATCH_QUEUE_SMALL", ""), "large": env("CEDARS_BATCH_QUEUE_LARGE", "")}
CALCULATIONS_BATCH_JOB_DEFINITION = env("CEDARS_BATCH_JOB_DEFINITION", "")  # used when a release names none (infra/terraform)
CALCULATIONS_SMALL_RUN_RECORDS = int(env("CALCULATIONS_SMALL_RUN_RECORDS", "10000"))  # up to this many measures: the small-runs queue
CALCULATIONS_MAX_RECORDS = int(env("CALCULATIONS_MAX_RECORDS", "2000000"))  # BRD v0.5's configurable cap (the engine checks it too)
CALCULATIONS_RESULTS_CACHE = env(
    "CALCULATIONS_RESULTS_CACHE", str(BASE_DIR / "var" / "cache" / "results")
)  # local copies of runs' results files
CALCULATIONS_WORKERS = {"small": int(env("CALCULATIONS_WORKERS_SMALL", "1")), "large": int(env("CALCULATIONS_WORKERS_LARGE", "1"))}

# Data, dashboards and downloads (cedars/modules/data): queries over the publication snapshots.
DATA_CACHE = env("DATA_CACHE", str(BASE_DIR / "var" / "cache" / "snapshots"))  # local copies of snapshots kept in S3
DATA_QUERY_THREADS = int(env("DATA_QUERY_THREADS", "2"))  # DuckDB threads per query
DATA_QUERY_MEMORY = env("DATA_QUERY_MEMORY", "512MB")  # DuckDB memory limit per query (it spills to disk beyond it)
DATA_DOWNLOAD_MAX_ROWS = int(env("DATA_DOWNLOAD_MAX_ROWS", "200000"))  # rows or records made while the person waits; more: a background job
DATA_DOWNLOAD_JOBS_PER_USER = int(env("DATA_DOWNLOAD_JOBS_PER_USER", "3"))  # background downloads one person may have waiting at once
DATA_DOWNLOAD_INLINE_JOBS = env_bool("DATA_DOWNLOAD_INLINE_JOBS", False)  # development: make a queued download once its request commits

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
# BR-11.3: a nightly sync that fails is tried again after each of these waits (minutes); when the last attempt fails too, the CPUC's
# administrators are alerted. Empty = one attempt.
ETRM_SYNC_RETRY_MINUTES = [float(m) for m in env_list("ETRM_SYNC_RETRY_MINUTES", "10,30")]
LOAD_SHAPE_FIRST_START = env("LOAD_SHAPE_FIRST_START", "2027-01-01")  # earlier measure versions keep DEER impact profile IDs
LOAD_SHAPE_MAX_UPLOAD_MB = int(env("LOAD_SHAPE_MAX_UPLOAD_MB", "50"))
LOAD_SHAPE_MAX_UNZIPPED_MB = int(env("LOAD_SHAPE_MAX_UNZIPPED_MB", "1024"))
IMPACT_PROFILE_TERRITORIES = env_list("IMPACT_PROFILE_TERRITORIES", "PGE,SCE,SCG,SDGE")  # a profile with no territory is written for each
IMPACT_PROFILES_INLINE_JOBS = env_bool("IMPACT_PROFILES_INLINE_JOBS", False)  # development: run queued jobs in the request

# Job and system health (BR-12.2; cedars/core/health.py, /admin/health/, `manage.py check_health`): minutes before a job counts as
# stalled, and how far back failures are listed.
HEALTH = {
    "queued": int(env("HEALTH_QUEUED_MINUTES", "30")),  # a calculation run or Impact profiles job still waiting to start
    "run_running": int(env("HEALTH_RUN_MINUTES", "120")),  # a calculation run still running (Batch stops a job at 60)
    "job_running": int(env("HEALTH_PROFILE_JOB_MINUTES", "240")),  # an Impact profiles job still running (library writes are long)
    "etrm": int(env("HEALTH_ETRM_MINUTES", str(36 * 60))),  # since the last successful eTRM sync (it runs nightly)
    "failures": int(env("HEALTH_FAILURES_MINUTES", str(24 * 60))),  # failed runs, jobs and emails listed this far back
    "worker_seen": int(env("HEALTH_WORKER_SEEN_MINUTES", "10")),  # a local calculations worker counts as running if it polled this recently
}

# Documents & EM&V module: the largest file a document version may carry (the PDA's largest main files are tens of MB)
DOCUMENTS_MAX_FILE_MB = int(env("DOCUMENTS_MAX_FILE_MB", "200"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
}
