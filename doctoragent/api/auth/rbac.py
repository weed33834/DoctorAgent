"""Role-Based Access Control (RBAC) for the DoctorAgent API.

Provides a permission matrix over seven roles:

* 四个通用角色 —— ``ADMIN``、``EDITOR``、``VIEWER``、``AUDITOR``，由五个粗粒度
  权限（``read``、``write``、``delete``、``admin``、``audit``）支撑。
* 三个临床角色 —— ``CLINICIAN``（临床医生）、``PHARMACIST``（药剂师）、
  ``NURSE``（护士），由细粒度临床权限（如 ``clinical:analyze``、``phi:redact``、
  ``vault:read``、``fhir:read``）支撑。

临床角色接入与通用角色相同的策略矩阵和（当 casbin 可用时）相同的 enforcer，
因此无论后端如何切换，授权结果都是确定的。一个轻量的角色层级让高级临床角色
继承低级角色的权限（例如 ``CLINICIAN`` 继承 ``NURSE`` 与 ``VIEWER``）。

Enforcement is backed by `casbin <https://casbin.org>`_ when the optional
``auth`` extra is installed; otherwise a built-in static permission matrix is
used. Both engines produce identical results for the default policy so the
behaviour is deterministic regardless of whether casbin is available.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from doctoragent.compat import StrEnum

logger = logging.getLogger(__name__)


# ── Public role / permission enums ───────────────────────────────────────────


class Role(StrEnum):
    """Logical roles recognised by the DoctorAgent authorizer.

    支持七个角色：四个通用角色（``ADMIN``/``EDITOR``/``VIEWER``/``AUDITOR``）
    与三个临床角色（``CLINICIAN``/``PHARMACIST``/``NURSE``）。
    """

    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"
    AUDITOR = "auditor"
    # 临床角色
    CLINICIAN = "clinician"  # 临床医生
    PHARMACIST = "pharmacist"  # 药剂师
    NURSE = "nurse"  # 护士


class Permission(StrEnum):
    """Granular permissions that can be granted to a role.

    包含通用粗粒度权限（``read``/``write``/``delete``/``admin``/``audit``）
    以及临床场景下的细粒度权限（如 ``clinical:analyze``、``phi:redact``、
    ``vault:read``）。两类权限共用同一套检查机制。
    """

    # 通用权限
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    ADMIN = "admin"
    AUDIT = "audit"
    # 临床工作台
    CLINICAL_ANALYZE = "clinical:analyze"  # 执行临床分析
    CLINICAL_VIEW = "clinical:view"  # 查看临床分析结果
    # 智能对话
    CHAT_SEND = "chat:send"  # 发送对话消息
    CHAT_VIEW = "chat:view"  # 查看对话历史
    # PHI 脱敏
    PHI_DETECT = "phi:detect"  # 检测 PHI
    PHI_REDACT = "phi:redact"  # 执行 PHI 脱敏
    PHI_VIEW = "phi:view"  # 查看 PHI 脱敏结果
    # 安全规则
    RULES_VIEW = "rules:view"  # 查看安全规则
    # 审计日志（细粒度）
    AUDIT_VIEW = "audit:view"  # 查看审计日志
    AUDIT_VIEW_OWN = "audit:view_own"  # 仅查看自己的审计日志
    # 文档 Vault
    VAULT_READ = "vault:read"  # 读取文档 Vault
    VAULT_WRITE = "vault:write"  # 写入/上传文档 Vault
    # FHIR
    FHIR_READ = "fhir:read"  # 读取 FHIR 资源


# ── Default policy ───────────────────────────────────────────────────────────
#
# Source of truth for both the static fallback and the casbin policy. Each
# role maps to the set of permissions it is granted. ADMIN inherits every
# permission; EDITOR can read/write; VIEWER is read-only; AUDITOR can read
# and read the audit log but not mutate data.
#
# 临床角色的权限遵循最小权限原则：
# - CLINICIAN（临床医生）：可执行分析、查看结果、智能对话、PHI 脱敏（使用不管理）、
#   查看安全规则、查看自己的审计日志、读写文档 Vault、读取 FHIR。
# - PHARMACIST（药剂师）：可执行用药安全审查、智能对话、查看安全规则、查看审计日志、
#   读取文档 Vault 与 FHIR；不具备 PHI 脱敏与配置/租户管理能力。
# - NURSE（护士）：仅可查看临床分析结果（不能执行分析）、智能对话、查看安全规则、
#   查看审计日志、读取文档 Vault 与 FHIR；不具备临床分析执行、PHI 脱敏与配置管理能力。

DEFAULT_POLICY: dict[Role, frozenset[Permission]] = {
    # 通用角色（保持原有定义不变）
    Role.ADMIN: frozenset(Permission),
    Role.EDITOR: frozenset({Permission.READ, Permission.WRITE}),
    Role.VIEWER: frozenset({Permission.READ}),
    Role.AUDITOR: frozenset({Permission.READ, Permission.AUDIT}),
    # 临床角色
    Role.CLINICIAN: frozenset(
        {
            Permission.CLINICAL_ANALYZE,
            Permission.CLINICAL_VIEW,
            Permission.CHAT_SEND,
            Permission.CHAT_VIEW,
            Permission.PHI_DETECT,
            Permission.PHI_REDACT,
            Permission.PHI_VIEW,
            Permission.RULES_VIEW,
            Permission.AUDIT_VIEW_OWN,
            Permission.VAULT_READ,
            Permission.VAULT_WRITE,
            Permission.FHIR_READ,
        }
    ),
    Role.PHARMACIST: frozenset(
        {
            Permission.CLINICAL_ANALYZE,
            Permission.CLINICAL_VIEW,
            Permission.CHAT_SEND,
            Permission.CHAT_VIEW,
            Permission.RULES_VIEW,
            Permission.AUDIT_VIEW,
            Permission.VAULT_READ,
            Permission.FHIR_READ,
        }
    ),
    Role.NURSE: frozenset(
        {
            Permission.CLINICAL_VIEW,
            Permission.CHAT_SEND,
            Permission.CHAT_VIEW,
            Permission.RULES_VIEW,
            Permission.AUDIT_VIEW,
            Permission.VAULT_READ,
            Permission.FHIR_READ,
        }
    ),
}


# ── Role hierarchy ───────────────────────────────────────────────────────────
#
# 角色层级（高级角色继承低级角色的权限）。继承在静态矩阵与 casbin 后端中均生效：
# 一个角色的有效权限集合为其自身权限并上所有被继承角色的权限。未列出的角色
# （如 EDITOR/VIEWER/AUDITOR/NURSE）没有下游继承，仅保留自身权限，从而保证
# 已有用户的默认行为不变。

ROLE_HIERARCHY: dict[Role, list[Role]] = {
    Role.ADMIN: [
        Role.EDITOR,
        Role.AUDITOR,
        Role.CLINICIAN,
        Role.PHARMACIST,
        Role.NURSE,
        Role.VIEWER,
    ],
    Role.CLINICIAN: [Role.NURSE, Role.VIEWER],
    Role.PHARMACIST: [Role.VIEWER],
}


# ── Clinical role helpers ────────────────────────────────────────────────────


def is_clinical_role(role: Role) -> bool:
    """判断是否为临床角色（临床医生/药剂师/护士）。"""
    return role in (Role.CLINICIAN, Role.PHARMACIST, Role.NURSE)


def can_execute_clinical_analysis(role: Role) -> bool:
    """判断角色是否可执行临床分析。

    仅临床医生、药剂师与管理员可执行临床分析；护士仅可查看分析结果。
    """
    return role in (Role.CLINICIAN, Role.PHARMACIST, Role.ADMIN)


# ── Casbin model definition (RBAC with wildcard resources) ──────────────────

_CASBIN_MODEL = """[request_definition]
r = sub, obj, act

[policy_definition]
p = sub, obj, act

[policy_effect]
e = some(where (p.eft == allow))

[matchers]
m = r.sub == p.sub && keyMatch(r.obj, p.obj) && (r.act == p.act || p.act == "*")
"""


def _policy_lines_for(role: Role, permissions: frozenset[Permission]) -> list[str]:
    """Render casbin ``p`` policy lines for a single role.

    Each permission becomes a policy granting ``<role>, *, <permission>`` — the
    resource is left as the wildcard ``*`` so the policy applies to every
    resource. The ``admin`` permission is also modelled as the wildcard action
    ``*`` so an ADMIN can perform any action on any resource.
    """
    lines: list[str] = []
    for perm in permissions:
        if perm is Permission.ADMIN:
            # ADMIN permission ⇒ wildcard action (any verb on any resource).
            lines.append(f"p, {role.value}, *, *")
        else:
            lines.append(f"p, {role.value}, *, {perm.value}")
    return lines


def _build_casbin_policy_text(policy: dict[Role, frozenset[Permission]]) -> str:
    lines: list[str] = []
    for role, perms in policy.items():
        lines.extend(_policy_lines_for(role, perms))
    return "\n".join(lines) + "\n"


# ── Optional casbin import ───────────────────────────────────────────────────

try:  # pragma: no cover — exercised on both branches depending on environment
    import casbin  # type: ignore[import-untyped]
    from casbin import Enforcer  # type: ignore[import-untyped]
    from casbin.model import Model as CasbinModel  # type: ignore[import-untyped]

    _CASBIN_AVAILABLE = True
except ImportError:  # pragma: no cover — casbin is in the `auth` extra
    casbin = None  # type: ignore[assignment]
    Enforcer = None  # type: ignore[assignment]
    CasbinModel = None  # type: ignore[assignment]
    _CASBIN_AVAILABLE = False


# ── Authorizer ───────────────────────────────────────────────────────────────


class RBACAuthorizer:
    """Authorize (role, resource, action) tuples against the RBAC policy.

    Uses casbin when available, otherwise falls back to the in-process static
    permission matrix. ``load_policy`` reloads the underlying casbin enforcer
    from a policy file (CSV); without casbin the static matrix is always used.

    角色层级（``ROLE_HIERARCHY``）在两条路径上都被展开为"有效权限"：静态矩阵
    直接对有效权限集合做成员判断；casbin 后端则按有效权限写入策略行，使两个
    后端在继承场景下行为一致。
    """

    def __init__(
        self,
        policy: dict[Role, frozenset[Permission]] | None = None,
        policy_path: str | Path | None = None,
        hierarchy: dict[Role, list[Role]] | None = None,
    ) -> None:
        self._policy: dict[Role, frozenset[Permission]] = dict(
            policy if policy is not None else DEFAULT_POLICY
        )
        # 角色层级：仅在使用默认策略时默认启用 ROLE_HIERARCHY；传入自定义策略
        # 时默认不启用继承（保持"自定义策略完全覆盖行为"的既有语义，保证向后
        # 兼容）。调用方可显式传入 ``hierarchy`` 以按需启用继承。
        if hierarchy is not None:
            self._hierarchy: dict[Role, list[Role]] = hierarchy
        elif policy is None:
            self._hierarchy = ROLE_HIERARCHY
        else:
            self._hierarchy = {}
        self._policy_path: Path | None = Path(policy_path) if policy_path else None
        self._enforcer: Any = None
        self._init_enforcer()

    # ── setup ───────────────────────────────────────────────────────────

    def _init_enforcer(self) -> None:
        """Build the casbin enforcer from the in-memory policy.

        When casbin is unavailable this is a no-op and ``check`` falls back
        to the static matrix.
        """
        if not _CASBIN_AVAILABLE:
            self._enforcer = None
            return
        try:
            model = CasbinModel()  # type: ignore[misc]
            model.load_model_from_text(_CASBIN_MODEL)
            enforcer = Enforcer(model)  # type: ignore[misc]
            # If an on-disk policy file is provided, load it; otherwise seed
            # from the in-memory default policy.
            if self._policy_path is not None and self._policy_path.exists():
                try:
                    enforcer.load_policy(self._policy_path.as_posix())
                except Exception:  # noqa: BLE001 — fall back to default policy
                    logger.warning(
                        "Failed to load casbin policy from %s; using default",
                        self._policy_path,
                        exc_info=True,
                    )
                    self._seed_enforcer(enforcer)
            else:
                self._seed_enforcer(enforcer)
            self._enforcer = enforcer
        except Exception:  # noqa: BLE001 — never fatal: fall back to static
            logger.warning(
                "casbin enforcer initialization failed; falling back to static permission matrix",
                exc_info=True,
            )
            self._enforcer = None

    def _seed_enforcer(self, enforcer: Any) -> None:
        """Inject the default policy into *enforcer* as in-memory rules.

        每个角色按"有效权限"（自身权限并上角色层级中继承到的权限）写入，
        这样 casbin 后端与静态矩阵在角色继承下行为一致。
        """
        effective_policy = {role: self._effective_permissions(role) for role in self._policy}
        for line in _build_casbin_policy_text(effective_policy).splitlines():
            parts = [p.strip() for p in line.split(",")]
            if not parts or parts[0] != "p":
                continue
            # parts == ["p", sub, obj, act]
            enforcer.add_policy(parts[1], parts[2], parts[3])

    # ── public API ──────────────────────────────────────────────────────

    def load_policy(self, policy_path: str | Path | None = None) -> None:
        """Reload the casbin policy from *policy_path* (CSV).

        Without casbin this is a no-op (the static matrix cannot be reloaded
        from disk).
        """
        if policy_path is not None:
            self._policy_path = Path(policy_path)
        if not _CASBIN_AVAILABLE or self._policy_path is None:
            self._init_enforcer()
            return
        self._init_enforcer()

    def check(self, role: str, resource: str, action: str) -> bool:
        """Return True if *role* is allowed to perform *action* on *resource*.

        Unknown roles are denied. The lookup is case-insensitive on the role.
        """
        if role is None:
            return False
        normalized = str(role).strip().lower()
        try:
            role_enum = Role(normalized)
        except ValueError:
            return False

        if self._enforcer is not None:
            try:
                return bool(self._enforcer.enforce(role_enum.value, resource, action))
            except Exception:  # noqa: BLE001 — fall back to static matrix
                logger.warning(
                    "casbin enforcement raised; falling back to static matrix",
                    exc_info=True,
                )

        return self._check_static(role_enum, action)

    def _effective_permissions(self, role: Role) -> frozenset[Permission]:
        """返回角色的有效权限集合（包含角色层级中继承到的权限）。

        使用迭代展开避免循环继承；未在 ``self._hierarchy`` 中列出的角色
        仅返回其自身权限，保证向后兼容。
        """
        seen: set[Role] = set()
        result: set[Permission] = set()
        stack: list[Role] = [role]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            result.update(self._policy.get(current, frozenset()))
            stack.extend(self._hierarchy.get(current, []))
        return frozenset(result)

    def _check_static(self, role: Role, action: str) -> bool:
        perms = self._effective_permissions(role)
        # Normalize the action; ADMIN permission grants every action.
        if Permission.ADMIN in perms:
            return True
        try:
            action_perm = Permission(action)
        except ValueError:
            return False
        return action_perm in perms

    def get_permissions(self, role: str) -> list[Permission]:
        """Return the list of permissions granted to *role* (unknown → empty).

        返回角色自身直接持有的权限（不含角色层级继承的权限）。
        """
        if role is None:
            return []
        try:
            role_enum = Role(str(role).strip().lower())
        except ValueError:
            return []
        return sorted(self._policy.get(role_enum, frozenset()), key=lambda p: p.value)


# ── FastAPI dependency factory ───────────────────────────────────────────────


def require_role(*roles: Role) -> Callable[..., Any]:
    """Build a FastAPI dependency that requires the request user to hold one of *roles*.

    The dependency reads ``request.state.user`` (set by the OIDC authenticator
    or any auth dependency that populates a :class:`~doctoragent.api.auth.oidc.UserInfo`)
    and checks ``user.roles`` against the allowed roles. A mismatch raises
    ``403 Forbidden``. When no user is present on the request the dependency
    raises ``403`` (fail-closed).

    FastAPI is imported lazily so that importing this module does not hard-require
    the ``server`` extra. The ``request`` parameter is annotated with the real
    ``fastapi.Request`` class (assigned post-definition) so FastAPI injects the
    request object rather than treating it as a query parameter.
    """
    allowed = {r.value for r in roles}

    async def _dependency(request: Any) -> Any:  # type: ignore[valid-type]
        # Lazy import keeps rbac.py importable without the server extra.
        try:
            from fastapi import HTTPException
        except ImportError as exc:  # pragma: no cover — server extra missing
            raise RuntimeError(
                "require_role requires FastAPI: pip install 'doctoragent[server]'"
            ) from exc

        user = getattr(getattr(request, "state", None), "user", None)
        if user is None:
            # Service-account semantics: a single static API token cannot
            # carry per-user roles, so a request authenticated *with* the
            # configured DOCTORAGENT_API_TOKEN is treated as holding every
            # role. Requests that merely came from localhost (or that carry
            # no authentication at all) are still denied — fail-closed.
            auth_method = getattr(request.state, "auth_method", None)
            if auth_method != "static_token":
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "Forbidden: requires one of roles "
                        f"{sorted(allowed)} — authenticate via OIDC for "
                        "role-based access, or with DOCTORAGENT_API_TOKEN "
                        "as the service account"
                    ),
                )
            return None
        user_roles = getattr(user, "roles", None) or []
        user_role_set = {str(r).strip().lower() for r in user_roles}
        if not (user_role_set & allowed):
            raise HTTPException(
                status_code=403,
                detail=f"Forbidden: requires one of roles {sorted(allowed)}",
            )
        return user

    # Annotate ``request`` with the real FastAPI ``Request`` class so FastAPI's
    # dependency resolver injects the request object. The annotation is set as
    # an actual class object (not a string) so ``typing.get_type_hints`` resolves
    # it regardless of ``from __future__ import annotations``.
    try:
        from fastapi import Request as _FastAPIRequest

        _dependency.__annotations__ = {"request": _FastAPIRequest, "return": Any}
    except ImportError:  # pragma: no cover — server extra missing
        pass

    return _dependency


__all__ = [
    "DEFAULT_POLICY",
    "Permission",
    "RBACAuthorizer",
    "Role",
    "ROLE_HIERARCHY",
    "can_execute_clinical_analysis",
    "is_clinical_role",
    "require_role",
]
