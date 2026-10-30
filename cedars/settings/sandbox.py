"""The sandbox (BR-11.2, BR-12.3): the production stack with its own database, no PII, open to PAs for test submissions and runs.
Every page carries a banner saying so, every email's subject starts with [SANDBOX], and filings older than the sandbox_data
retention period (30 days by default) are deleted by the daily retention job."""

from .prod import *  # noqa: F403
from .prod import DATABASES

ENVIRONMENT = "sandbox"
EMAIL_SUBJECT_PREFIX = "[SANDBOX] "
DATABASES.pop("vault", None)  # the sandbox never holds PII
