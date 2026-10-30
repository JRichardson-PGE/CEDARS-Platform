"""Audit and change log (BR-09.1, 09.3, BR-12.7): an append-only stream of events written by every service.

    from cedars.services import audit
    audit.record("grant.revoked", obj=grant, before=..., after=..., reason="left the company")

Who and from where come from the request context (audit.context, set by AuditContextMiddleware) unless passed explicitly.
The table refuses UPDATE and DELETE at the database level (migration 0002)."""

__all__ = ["changes", "diff", "record", "snapshot"]


def __getattr__(name):
    # The service functions are loaded on first use: this package is imported while Django is still loading apps.
    if name in __all__:
        from . import service

        return getattr(service, name)
    raise AttributeError(name)
