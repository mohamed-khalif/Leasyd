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


class Telemetry(Protocol):
    """One tenant's data, read-only, bound to that tenant when made (the model never picks a tenant).
    Runs the query tools the AI SRE has (services/ai/sre.py: list_services, query_promql, search_logs,
    search_spans, get_trace, top_values) and returns their JSON-able answers."""

    def run(self, name: str, args: dict) -> dict: ...


class Deploys(Protocol):
    """One tenant's recorded deploys (bound to that tenant when made), read-only:
    -> [{id, service, version, deployed_at, environment?, status, commit?, repository?, url?, description?}], oldest first."""

    def between(self, start_iso: str, end_iso: str, service: str | None = None) -> list[dict]: ...


class Channels(Protocol):
    """Where a tenant's investigations go (the alert channels, obs-alerts' records)."""

    def get(self, tenant: str, channel_id: str) -> dict | None:
        """The channel with its secrets decrypted: {type, name, url?, secret?, email?, delivery?, topic_arn?}."""

    def settings(self, tenant: str) -> dict:
        """{channels: [ids], services: {service: [ids]}, fallback: id} or {} when not set."""

    def rule(self, tenant: str, rule_id: str) -> dict | None: ...

    def first_skip_notice(self, tenant: str, hour: str) -> bool:
        """True the first time in that hour (the rate-limit notice goes out once an hour)."""


class Sender(Protocol):
    """One kind of channel (adapters/channels/): renders an Outcome in its own format, with a plain-text
    fallback, and sends it. Raises domain.delivery.DeliveryError with the reason."""
    kind: str

    def send(self, channel: dict, outcome) -> None: ...


class LLMClient(Protocol):
    """One model call: -> {"content": [blocks], "stop_reason": str, "usage": {"input_tokens", "output_tokens"}}."""
    model: str

    def create(self, *, system: str, messages: list, tools: list, max_tokens: int, tool_choice: dict | None = None) -> dict: ...


class Prompts(Protocol):
    """The investigation prompt (configurable without a code change): -> {"version", "system"}."""

    def load(self) -> dict: ...
