"""Who may do what in the Impact profiles module.

Everyone signed in sees the libraries and the eTRM sync history (as in CEDARS). Load shapes are uploaded under an organisation membership
with impact_profiles.submit, and only for packages whose PA lead is that organisation: the PA-lead match is a rule on the package, not a
separate account per PA (one identity with a membership per PA, BR-01.1). A PA's members see its upload history. Custom profiles belong
to the PA that uploaded them. The CPUC's system administrators, or its members with impact_profiles.admin, run the sync and write the
library."""

from cedars.services.accounts import service as access
from cedars.services.accounts.models import Organisation
from cedars.services.accounts.service import AccessDenied

SUBMIT = "impact_profiles.submit"
ADMIN = "impact_profiles.admin"


def is_admin(user) -> bool:
    if user is None:
        return True
    org = access.cpuc()
    return access.is_system_admin(user) or (org is not None and access.has_permission(user, org, ADMIN))


def can_submit(user, organisation: Organisation | None) -> bool:
    return user is None or (organisation is not None and access.has_permission(user, organisation, SUBMIT))


def require_submit(user, organisation: Organisation | None, what: str) -> None:
    if organisation is None:
        raise AccessDenied(f"Choose the organisation you are acting for before you {what}.")
    if not can_submit(user, organisation):
        raise AccessDenied(f"You need the Impact profiles: submit permission in {organisation.code} to {what}.")


def require_package_lead(user, organisation: Organisation | None, package) -> None:
    """Upload rule from the Load Shapes user guide: the package's PA lead must be the organisation the user acts for."""
    require_submit(user, organisation, "upload load shapes")
    if user is not None and package.pa_lead != organisation.code:
        raise AccessDenied(
            f"{package.measure_version_id} is led by {package.pa_lead}. "
            f"Act for {package.pa_lead} (a membership there) to upload its load shapes."
        )


def require_admin(user, what: str) -> None:
    if not is_admin(user):
        raise AccessDenied(f"Only a CPUC administrator can {what}.")


def sees_organisation(user, organisation_code: str) -> bool:
    """Members of the PA (and CPUC administrators) see its upload history."""
    if is_admin(user):
        return True
    return any(o.code == organisation_code for o in access.organisations_of(user))
