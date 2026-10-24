"""Small device-capability boundary; product profiles do not choose permissions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

DEFAULT_BACKEND = "accessibility"


class PhoneBackend(Protocol):
    """The existing MCP tools depend on these operations, not an OEM SDK."""

    def status(self) -> dict[str, Any]: ...
    def apps(self, query: str = "", limit: int = 80) -> dict[str, Any]: ...
    def execute(self, action: str, args: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class BackendSpec:
    id: str
    implemented: bool


BACKENDS = (
    BackendSpec(DEFAULT_BACKEND, True),
    BackendSpec("shizuku", False),
    BackendSpec("oem", False),
)


class BackendUnavailable(ValueError):
    pass


def create_backend(name: str, factories: Mapping[str, Callable[[], PhoneBackend]]) -> PhoneBackend:
    """Construct only an explicitly implemented adapter, without silent fallback."""
    spec = next((item for item in BACKENDS if item.id == name), None)
    if spec is None:
        raise BackendUnavailable(f"Unknown phone backend: {name}")
    if not spec.implemented:
        raise BackendUnavailable(f"Phone backend '{name}' is not implemented")
    factory = factories.get(name)
    if factory is None:
        raise BackendUnavailable(f"Phone backend '{name}' has no registered adapter")
    return factory()
