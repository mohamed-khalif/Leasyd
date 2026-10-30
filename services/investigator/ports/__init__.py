"""What the investigator's core needs from the outside world, as interfaces. The core (domain/, agent/)
only uses these; adapters/ implements them on AWS, tests and evals with fakes."""

from typing import Protocol

from domain.model import Investigation, Result


class StaleWrite(Exception):
    """A save refused: someone saved the investigation since this copy was loaded."""


class Store(Protocol):
    def get(self, tenant: str, inv_id: str) -> Investigation | None: ...

    def create(self, inv: Investigation) -> None:
        """Saves a new investigation; refuses an id that exists."""

    def save(self, inv: Investigation) -> None:
        """Saves a change; refuses a stale copy (another writer saved since this was loaded)."""

    def claim_dedupe(self, tenant: str, key: str, inv_id: str, now_epoch: int, until_epoch: int) -> str | None:
        """Claims key for inv_id until until_epoch (a claim that ran out by now_epoch is taken over).
        -> None if claimed, else the open claim's inv_id."""

    def take_quota(self, tenant: str, hour: str, limit: int, until_epoch: int) -> bool:
        """Counts one investigation in the tenant's hour. False (nothing counted) when limit is reached."""

    def tenant_of_webhook(self, token_hash: str) -> str | None: ...

    def list(self, tenant: str, limit: int) -> list[dict]: ...


class Queue(Protocol):
    def send(self, message: dict) -> None: ...


class Agent(Protocol):
    name: str

    def investigate(self, inv: Investigation) -> Result: ...


class Clock(Protocol):
    def now(self) -> float:
        """Epoch seconds."""
