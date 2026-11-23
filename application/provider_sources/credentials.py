"""Authorization contexts of a one-off acquisition (02 design, section 6).

``PublicKeyContext``: the installation key, obtained by the shared transport
exactly as collection does; it never enters a plan or a result.
``OwnerContext``: an owner binding; its token comes from an
``OwnerCredentialProvider``. Without a configured provider, owner acquisitions return
``owner_auth_unavailable``. Tests use a fake provider with a fictitious token.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol

from provider_credentials import OwnerBearer

_BINDING = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
_PROJECT = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
# OAuth scope identifiers (not request hosts), e.g. .../auth/youtube.readonly.
_SCOPE = re.compile(r"https://www\.googleapis\.com/auth/[a-z0-9._-]{1,64}\Z")


class OwnerAuthUnavailable(RuntimeError):
    """No usable owner token; nothing was sent. Codes are fixed and payload-free."""
    code = "owner_auth_unavailable"

    def __init__(self, code=None):
        super().__init__(code or self.code)
        if code:
            self.code = code


class OwnerAuthExpired(OwnerAuthUnavailable):
    code = "owner_auth_expired"


class OwnerAuthRevoked(OwnerAuthUnavailable):
    code = "owner_auth_revoked"


@dataclass(frozen=True)
class PublicKeyContext:
    kind = "public_key"

    @property
    def label(self):
        return "public_key"


@dataclass(frozen=True)
class OwnerBinding:
    binding_id: str
    channel_id: str
    scopes: frozenset
    quota_project_label: str

    def __post_init__(self):
        if (not isinstance(self.binding_id, str) or _BINDING.fullmatch(self.binding_id) is None
                or not isinstance(self.channel_id, str) or _CHANNEL.fullmatch(self.channel_id) is None
                or not isinstance(self.quota_project_label, str) or _PROJECT.fullmatch(self.quota_project_label) is None
                or not isinstance(self.scopes, frozenset)
                or not all(isinstance(scope, str) and _SCOPE.fullmatch(scope) for scope in self.scopes)):
            raise ValueError("owner_binding_invalid")


@dataclass(frozen=True)
class OwnerContext:
    binding: OwnerBinding
    kind = "owner"

    @property
    def label(self):
        return "owner:" + self.binding.binding_id


class OwnerCredentialProvider(Protocol):
    def binding(self) -> OwnerBinding: ...

    def access_token(self, *, scopes: frozenset, deadline: float) -> OwnerBearer:
        """Return a token valid for one of ``scopes`` or raise OwnerAuth*; token refresh is the provider's."""


@dataclass(frozen=True)
class Credential:
    """What the transport receives: None is the installation key context."""
    context: str
    transport_value: OwnerBearer | None

    def __repr__(self):
        return f"Credential({self.context})"


def resolve_credential(context, *, owner_provider=None, scopes=frozenset(), deadline):
    """One credential for one acquisition; an owner binding must match the plan's."""
    if isinstance(context, PublicKeyContext):
        return Credential("public_key", None)
    if not isinstance(context, OwnerContext):
        raise OwnerAuthUnavailable()
    if owner_provider is None:
        raise OwnerAuthUnavailable()
    if owner_provider.binding() != context.binding:
        raise OwnerAuthUnavailable("owner_binding_changed")
    usable = frozenset(scopes) & context.binding.scopes
    if not usable:
        raise OwnerAuthUnavailable("owner_scope_missing")
    token = owner_provider.access_token(scopes=usable, deadline=deadline)
    if not isinstance(token, OwnerBearer):
        raise OwnerAuthUnavailable()
    return Credential(context.label, token)
