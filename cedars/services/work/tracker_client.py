"""The site's one call out to the developers' tracker: posting a work item's issue, as a GitHub App installed on the
repository named by CEDARS_ISSUE_REPOSITORY (CEDARS_ISSUE_APP_ID, CEDARS_ISSUE_APP_INSTALLATION_ID and
CEDARS_ISSUE_APP_PRIVATE_KEY, all from the environment). The App needs one permission, to write issues. The site signs a
short-lived token with the App's key, exchanges it for an installation token limited to that repository's issues, and posts
the issue. Nothing is read from the tracker, the tokens are never stored, and only api.github.com is called."""

import json
import re
import time
import urllib.error
import urllib.request

from django.conf import settings

API = "https://api.github.com"
TIMEOUT = 10  # seconds, each call
REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")


class TrackerError(Exception):
    """The tracker did not take the call; str(e) says so in words for the page (never the key or a token)."""


def _setting(name: str) -> str:
    return str(getattr(settings, name, "") or "").strip()


def can_post() -> bool:
    """Whether this site holds what it needs to post an issue itself."""
    return bool(
        REPOSITORY.match(_setting("CEDARS_ISSUE_REPOSITORY"))
        and _setting("CEDARS_ISSUE_APP_ID").isdigit()
        and _setting("CEDARS_ISSUE_APP_INSTALLATION_ID").isdigit()
        and _setting("CEDARS_ISSUE_APP_PRIVATE_KEY")
    )


def app_token(now: float | None = None) -> str:
    """The App's own token: signed with its key, good for nine minutes, used once to ask for an installation token."""
    import jwt

    now = int(now if now is not None else time.time())
    key = _setting("CEDARS_ISSUE_APP_PRIVATE_KEY").replace("\\n", "\n")
    try:
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": _setting("CEDARS_ISSUE_APP_ID")}, key, algorithm="RS256")
    except Exception as e:  # a key that is not a private key: say so, never show it
        raise TrackerError("The site's key for the developers' tracker cannot be read. Ask a system administrator.") from e


def _call(path: str, token: str, data: dict | None = None) -> dict:
    """POST to the tracker's API and return its JSON answer."""
    request = urllib.request.Request(  # noqa: S310 - a fixed https address
        API + path,
        data=json.dumps(data or {}).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "cedars-platform",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            answer = json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        raise TrackerError(f"The developers' tracker refused the call (status {e.code}).") from e
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise TrackerError("The developers' tracker could not be reached, or its answer could not be read.") from e
    if not isinstance(answer, dict):
        raise TrackerError("The developers' tracker gave an answer that could not be read.")
    return answer


def post_issue(title: str, body: str) -> tuple[str, int]:
    """Post one issue to the repository. Returns its address and number."""
    if not can_post():
        raise TrackerError("This site is not set up to post issues to the developers' tracker.")
    repository = _setting("CEDARS_ISSUE_REPOSITORY")
    grant = _call(
        f"/app/installations/{_setting('CEDARS_ISSUE_APP_INSTALLATION_ID')}/access_tokens",
        app_token(),
        {"repositories": [repository.split("/", 1)[1]], "permissions": {"issues": "write"}},
    )
    token = grant.get("token")
    if not isinstance(token, str) or not token:
        raise TrackerError("The developers' tracker gave the site no access.")
    issue = _call(f"/repos/{repository}/issues", token, {"title": title, "body": body})
    url, number = issue.get("html_url"), issue.get("number")
    if not isinstance(url, str) or not url.startswith(f"https://github.com/{repository}/issues/") or not isinstance(number, int):
        raise TrackerError(
            "The developers' tracker took the issue and gave no address for it. Look for it there and keep its address here."
        )
    return url, number
