"""Every page the accessibility check visits, as (name, path, who). A PR that adds a page adds it here (CLAUDE.md).

who: None (signed out), "password" (signed in with a password only: the second-factor set-up), or a key of test_axe.SCENE's people
(signed in with MFA). Paths may name the scene's objects with {pge}, {cpuc}, {allocation}."""

PAGES = [
    ("home", "/", None),
    ("sign-in", "/accounts/login/", None),
    ("sign-up", "/accounts/signup/", None),
    ("password-reset", "/accounts/password/reset/", None),
    ("set-up-second-factor", "/accounts/2fa/totp/activate/", "password"),
    ("second-factor", "/accounts/2fa/", "pge_admin"),
    ("account", "/account/", "pge_admin"),
    ("request-access", "/account/request-access/", "community"),
    ("organisations", "/admin/organisations/", "cpuc_admin"),
    ("organisation-pa-admin", "/admin/organisations/{pge}/", "pge_admin"),
    ("organisation-cpuc", "/admin/organisations/{cpuc}/", "cpuc_admin"),
    ("organisation-first-admin", "/admin/organisations/SCE/", "cpuc_admin"),
    ("organisation-new", "/admin/organisations/new/", "cpuc_admin"),
    ("stewardship", "/admin/stewardship/", "cpuc_admin"),
    ("settings", "/admin/settings/", "cpuc_admin"),
    ("audit", "/admin/audit/", "cpuc_admin"),
    ("allocations", "/admin/allocations/", "cpuc_admin"),
    ("allocation", "/admin/allocations/{allocation}/", "cpuc_admin"),
]
