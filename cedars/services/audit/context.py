"""Who is acting, for audit events written anywhere during a request (or a job, which sets it with `acting_as`)."""

import contextvars
import uuid
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class Actor:
    user: object = None  # a User, or None for the system
    organisation: object = None  # the active Organisation, if any
    assurance: str = ""  # password, mfa or federated
    request_id: str = ""


_current: contextvars.ContextVar[Actor] = contextvars.ContextVar("cedars_audit_actor", default=Actor())  # noqa: B039 - Actor is frozen


def current() -> Actor:
    return _current.get()


@contextmanager
def acting_as(user=None, organisation=None, assurance="", request_id=""):
    token = _current.set(Actor(user, organisation, assurance, request_id or uuid.uuid4().hex))
    try:
        yield
    finally:
        _current.reset(token)
