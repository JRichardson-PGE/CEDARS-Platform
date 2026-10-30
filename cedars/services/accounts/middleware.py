from .service import organisations_of

ORG_SESSION_KEY = "cedars_organisation"
ASSURANCE_SESSION_KEY = "cedars_assurance"


class ActiveOrganisationMiddleware:
    """request.organisation: the organisation the user is acting for, switched in the session (BR-01.1), always one they are an active
    member of (None for a community account). request.assurance: how the session was established (password, mfa or federated)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.organisation = None
        request.assurance = ""
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            request.assurance = request.session.get(ASSURANCE_SESSION_KEY, "password")
            orgs = list(organisations_of(user))
            chosen = request.session.get(ORG_SESSION_KEY)
            request.organisation = next((o for o in orgs if o.pk == chosen), orgs[0] if orgs else None)
            request.user_organisations = orgs
        return self.get_response(request)
