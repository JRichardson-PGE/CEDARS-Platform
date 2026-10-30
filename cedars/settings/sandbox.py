"""The sandbox (BR-11.2, BR-12.3): the production stack with its own database, no PII, open to PAs for test submissions and runs.
Every page carries a banner saying so."""

from .prod import *  # noqa: F403

ENVIRONMENT = "sandbox"
