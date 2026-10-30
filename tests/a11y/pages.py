"""Every page the accessibility check visits, as (name, path, who). A PR that adds a page adds it here (CLAUDE.md).

who: None (signed out), "password" (signed in with a password only: the second-factor set-up), or a key of test_axe.SCENE's people
(signed in with MFA). Paths may name the scene's objects by their keys, e.g. {pge}, {cpuc}, {allocation}, {package}, {upload}."""

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
    ("impact-profile-library", "/impact-profiles/?q=Msr", "developer"),
    ("impact-profile-deemed", "/impact-profiles/{profile}/", "developer"),
    ("impact-profile-custom", "/impact-profiles/{custom_profile}/", "developer"),
    ("impact-profile-custom-new", "/impact-profiles/custom/new/", "developer"),
    ("load-shape-packages", "/impact-profiles/packages/", "developer"),
    ("load-shape-package", "/impact-profiles/packages/{package}/", "developer"),
    ("load-shape", "/impact-profiles/load-shapes/{load_shape}/", "developer"),
    ("load-shape-uploads", "/impact-profiles/uploads/", "developer"),
    ("load-shape-upload", "/impact-profiles/uploads/{upload}/", "developer"),
    ("load-shape-upload-rejected", "/impact-profiles/uploads/{rejected}/", "developer"),
    ("etrm-sync-history", "/impact-profiles/etrm/", "cpuc_admin"),
    ("profile-library-files", "/impact-profiles/library/", "cpuc_admin"),
]
