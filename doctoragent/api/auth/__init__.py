"""Authentication for the DoctorAgent API.

This package provides:

* :class:`RBACAuthorizer` / :func:`require_role` — role-based access control
  over the API surface, backed by casbin when available and a static
  permission matrix otherwise.
* :class:`OIDCAuthenticator` — bearer-token verification against an external
  OIDC provider (SSO), returning a normalised :class:`UserInfo`.

Both modules degrade gracefully: importing this package never requires the
optional ``auth`` or ``server`` extras. Constructing
:class:`OIDCAuthenticator` without authlib installed raises :class:`ImportError`
with an install hint, which the API server turns into a ``503`` response.
"""

from doctoragent.api.auth.oidc import OIDCAuthenticator, UserInfo
from doctoragent.api.auth.rbac import (
    DEFAULT_POLICY,
    Permission,
    RBACAuthorizer,
    Role,
    require_role,
)

__all__ = [
    "DEFAULT_POLICY",
    "OIDCAuthenticator",
    "Permission",
    "RBACAuthorizer",
    "Role",
    "UserInfo",
    "require_role",
]
