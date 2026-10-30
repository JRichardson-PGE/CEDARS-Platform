"""Production and staging on the CPUC's AWS account: secrets from the environment (Secrets Manager), HTTPS only,
behind the load balancer."""

from .base import *  # noqa: F403
from .base import env, env_bool

ENVIRONMENT = env("CEDARS_ENVIRONMENT", "prod")
SECRET_KEY = env("DJANGO_SECRET_KEY")

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env_bool("DJANGO_SSL_REDIRECT", True)
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]  # the load balancer's health check speaks plain HTTP inside the VPC
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = int(env("DJANGO_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST", "")
EMAIL_PORT = int(env("EMAIL_PORT", "587"))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = True
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "cedars@localhost")

# HSTS preloading binds the whole parent domain (and its subdomains) in browsers; that is the CPUC's call for its domain, not this app's.
SILENCED_SYSTEM_CHECKS = ["security.W021"]
