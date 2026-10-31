"""A low-cost demo on one server (deploy/demo): the sandbox's settings (banner, no PII vault, test data only) behind Caddy on the same
machine. Email goes to the web container's log unless EMAIL_HOST names an SMTP server, so sign-up and password links work without one."""

from .base import env
from .sandbox import *  # noqa: F403

EMAIL_BACKEND = env(
    "EMAIL_BACKEND",
    "django.core.mail.backends.smtp.EmailBackend" if env("EMAIL_HOST", "") else "django.core.mail.backends.console.EmailBackend",
)
