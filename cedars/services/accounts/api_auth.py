"""Django REST Framework authentication for API tokens: `Authorization: Bearer cedars_...`. The call acts for the token's organisation
(request.organisation) and its audit events carry assurance "token"."""

from rest_framework import authentication, exceptions

from cedars.services.audit.context import set_actor

from . import tokens

TOKEN_ASSURANCE = "token"


class TokenAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).decode("latin-1").split()
        if not header or header[0] != self.keyword:
            return None
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("Send the token as: Authorization: Bearer <token>.")
        token = tokens.authenticate(header[1])
        if token is None:
            raise exceptions.AuthenticationFailed("That token is not valid: unknown, revoked or expired.")
        django_request = request._request
        django_request.organisation = token.organisation
        django_request.assurance = TOKEN_ASSURANCE
        set_actor(token.user, token.organisation, TOKEN_ASSURANCE)
        return token.user, token

    def authenticate_header(self, request):
        return self.keyword
