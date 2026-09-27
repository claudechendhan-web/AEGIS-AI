from collections.abc import Iterable
from enum import StrEnum

from core.errors import PermissionDeniedError


class Capability(StrEnum):
    READ_ONLY = "READ_ONLY"
    FILESYSTEM_READ = "FILESYSTEM_READ"
    FILESYSTEM_WRITE = "FILESYSTEM_WRITE"
    NETWORK = "NETWORK"
    SANDBOXED_EXECUTION = "SANDBOXED_EXECUTION"
    PROCESS_EXECUTION = "PROCESS_EXECUTION"


class PermissionPolicy:
    """Explicit capability policy with process execution permanently disabled."""

    def __init__(
        self, allowed: Iterable[Capability | str] = (Capability.READ_ONLY,)
    ) -> None:
        if isinstance(allowed, (str, bytes)):
            raise PermissionDeniedError("allowed capabilities must be an iterable")
        parsed: set[Capability] = set()
        for capability in allowed:
            try:
                parsed.add(Capability(capability))
            except (TypeError, ValueError) as exc:
                raise PermissionDeniedError(
                    f"unknown capability: {capability}"
                ) from exc
        if Capability.PROCESS_EXECUTION in parsed:
            raise PermissionDeniedError(
                "PROCESS_EXECUTION is disabled; use the sandbox-gated "
                "SANDBOXED_EXECUTION capability instead"
            )
        self._allowed = frozenset(parsed)

    @classmethod
    def default(cls) -> "PermissionPolicy":
        return cls()

    def allows(self, capability: Capability | str) -> bool:
        try:
            parsed = Capability(capability)
        except (TypeError, ValueError):
            return False
        return parsed in self._allowed

    def require(self, capability: Capability | str) -> None:
        parsed = self.parse(capability)
        if not self.allows(parsed):
            raise PermissionDeniedError(f"capability denied: {parsed.value}")

    def parse(self, capability: Capability | str) -> Capability:
        try:
            return Capability(capability)
        except (TypeError, ValueError) as exc:
            raise PermissionDeniedError(f"unknown capability: {capability}") from exc

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": sorted(item.value for item in self._allowed),
            "process_execution": False,
            "sandboxed_execution": Capability.SANDBOXED_EXECUTION in self._allowed,
        }
