"""Sends the vault's models to the `vault` database and nothing else there; nothing of the vault is migrated into the core database."""

VAULT = "vault"


class VaultRouter:
    def db_for_read(self, model, **hints):
        return VAULT if model._meta.app_label == VAULT else None

    def db_for_write(self, model, **hints):
        return VAULT if model._meta.app_label == VAULT else None

    def allow_relation(self, a, b, **hints):
        if VAULT in (a._meta.app_label, b._meta.app_label):
            return a._meta.app_label == b._meta.app_label
        return None

    def allow_migrate(self, db, app_label, **hints):
        if app_label == VAULT:
            return db == VAULT
        return db != VAULT
