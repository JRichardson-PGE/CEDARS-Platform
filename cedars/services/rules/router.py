"""The `jobs` database is a second connection to the core database (settings: RULES_JOBS_DATABASE), through which check jobs are
written so a worker sees them at once, before the request that submitted them ends its transaction. Nothing is migrated through it."""

JOBS = "jobs"


class JobsRouter:
    def allow_migrate(self, db, app_label, **hints):
        return False if db == JOBS else None
