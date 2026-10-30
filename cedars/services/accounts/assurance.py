"""How a session was established (decision F), read from the sign-in records django-allauth keeps in the session:

federated  signed in through an organisation's own directory (an OpenID Connect or SAML provider)
mfa        platform password (or passkey) plus a second factor
password   a password alone: the session can only reach the pages that set up a second factor (RequireMFAMiddleware)
"""

from allauth.account.authentication import get_authentication_records

FEDERATED, MFA, PASSWORD = "federated", "mfa", "password"


def session_assurance(request) -> str:
    methods = {r.get("method") for r in get_authentication_records(request, request.user)}
    if "socialaccount" in methods:
        return FEDERATED
    if "mfa" in methods or "passkey" in methods:
        return MFA
    return PASSWORD
