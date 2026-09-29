"""Regression tests for the Hindsight adapter contract.

Every bug covered here failed *silently* against the live service, which is what made them worth
locking down. A bank holding forty-odd memories rendered as "nothing retained yet", because the
adapter read attribute names the SDK does not use; an observation's ``entities`` string was
iterated character by character; and one transient connect error during startup pinned the
platform to the in-process store for the rest of the process's life.

These tests are offline and deterministic: they supply the response shapes the real client
returns rather than reaching the network.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from app.memory.base import EXPERIENCE, OBSERVATION, WORLD, RecallHit
from app.memory.hindsight_store import HindsightStore


class FakeUnit:
    """One memory unit, with the field names the live ``ListMemoryUnitsResponse`` really uses."""

    def __init__(self, **kwargs: Any) -> None:
        self.id = kwargs.get("id", "unit-1")
        self.text = kwargs.get("text", "payment-service leaked connections")
        self.context = kwargs.get("context", "")
        self.fact_type = kwargs.get("fact_type", WORLD)
        self.document_id = kwargs.get("document_id")
        self.mentioned_at = kwargs.get("mentioned_at")
        self.occurred_start = kwargs.get("occurred_start")
        self.occurred_end = kwargs.get("occurred_end")
        self.proof_count = kwargs.get("proof_count")
        self.entities = kwargs.get("entities", [])
        self.tags = kwargs.get("tags", [])
        self.metadata = kwargs.get("metadata", {})
        self.source_memory_ids = kwargs.get("source_memory_ids", [])


class FakeListResponse:
    """Mirrors ``ListMemoryUnitsResponse(items=[...], total=N)``."""

    def __init__(self, items: list[FakeUnit], total: int | None = None) -> None:
        self.items = items
        self.total = len(items) if total is None else total
        self.limit = len(items)
        self.offset = 0


class FakeClient:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def list_memories(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._response

    def get_version(self) -> Any:
        class Version:
            api_version = "0.10.1"

        return Version()


def make_store(response: Any) -> HindsightStore:
    store = HindsightStore(base_url="http://127.0.0.1:9", bank_id="test-bank")
    store._client = FakeClient(response)  # noqa: SLF001 - deliberate test seam
    return store


# -- response shapes ------------------------------------------------------------------


def test_items_are_read_from_the_sdk_field_that_actually_exists() -> None:
    """The live API returns `items`; reading `memories`/`results` yields a silent empty bank."""
    store = make_store(FakeListResponse([FakeUnit(id="a"), FakeUnit(id="b")], total=44))
    hits = store.list_memories(limit=50)
    assert [hit.id for hit in hits] == ["a", "b"]


def test_older_shapes_are_still_accepted() -> None:
    for field in ("memories", "results"):
        response = type("Alt", (), {field: [FakeUnit(id="x")]})()
        assert [hit.id for hit in make_store(response).list_memories(limit=5)] == ["x"]


def test_an_unrecognised_payload_is_not_silently_flattened_to_empty() -> None:
    """An unknown shape must be reported, not mistaken for a bank with nothing in it."""
    class Unknown:
        def __init__(self) -> None:
            self.data = [FakeUnit(id="x")]

    assert make_store(Unknown()).list_memories(limit=5) == []


def test_fact_type_maps_to_the_platforms_memory_type() -> None:
    """`fact_type` is the API's name for the kind of memory. Without the mapping every
    observation is reported as a world fact and the Observations view looks empty."""
    units = [
        FakeUnit(id="1", fact_type=WORLD, entities="payment-service, latency_ms"),
        FakeUnit(id="2", fact_type=OBSERVATION),
        FakeUnit(id="3", fact_type=EXPERIENCE),
    ]
    kinds = [hit.type for hit in make_store(FakeListResponse(units)).list_memories(limit=10)]
    assert kinds == [WORLD, OBSERVATION, EXPERIENCE]


def test_a_delimited_entity_string_is_split_not_iterated_per_character() -> None:
    unit = FakeUnit(entities="payment-service, latency_ms, connection_saturation")
    hit = make_store(FakeListResponse([unit])).list_memories(limit=1)[0]
    assert hit.entities == ["payment-service", "latency_ms", "connection_saturation"]


def test_entity_and_tag_lists_survive_unchanged() -> None:
    unit = FakeUnit(entities=["a", "b"], tags=["kind:pattern"])
    hit = make_store(FakeListResponse([unit])).list_memories(limit=1)[0]
    assert hit.entities == ["a", "b"]
    assert hit.tags == ["kind:pattern"]


def test_proof_counts_and_source_facts_are_carried_through() -> None:
    """Consolidated beliefs are only trustworthy if their provenance survives the adapter."""
    unit = FakeUnit(id="obs-1", fact_type=OBSERVATION, proof_count=3, source_memory_ids=["f1", "f2"])
    hit = make_store(FakeListResponse([unit])).list_memories(limit=1)[0]
    assert hit.proof_count == 3
    assert hit.source_fact_ids == ["f1", "f2"]


def test_stats_count_the_bank_rather_than_the_page_of_items() -> None:
    units = [
        FakeUnit(id="1", fact_type=WORLD, document_id="doc-a"),
        FakeUnit(id="2", fact_type=WORLD, document_id="doc-a"),
        FakeUnit(id="3", fact_type=OBSERVATION, document_id=None),
    ]
    # `total` is larger than the page: the count must follow the bank, not the page.
    store = make_store(FakeListResponse(units, total=44))
    stats = store.stats()
    assert stats["counts_available"] is True
    assert stats["facts"] == 44
    assert stats["observations"] == 1
    assert stats["documents"] == 1


def test_stats_report_unknown_rather_than_zero_when_the_bank_cannot_be_read() -> None:
    """A count that could not be read must never be rendered as a confident 0."""

    class Broken:
        def list_memories(self, **kwargs: Any) -> Any:
            raise RuntimeError("connection reset")

    store = HindsightStore(base_url="http://127.0.0.1:9", bank_id="test-bank")
    store._client = Broken()  # noqa: SLF001
    stats = store.stats()
    assert stats["counts_available"] is False
    assert "facts" not in stats


def test_a_stat_failure_degrades_health_rather_than_raising() -> None:
    class Broken:
        def list_memories(self, **kwargs: Any) -> Any:
            raise RuntimeError("connection reset")

        def get_version(self) -> Any:
            class Version:
                api_version = "0.10.1"

            return Version()

    store = HindsightStore(base_url="http://127.0.0.1:9", bank_id="test-bank")
    store._client = Broken()  # noqa: SLF001
    health = store.health()
    assert health["healthy"] is True
    assert health["counts_available"] is False


# -- degradation must be recoverable ---------------------------------------------------


def test_memory_service_restores_hindsight_once_it_recovers(memory_service) -> None:
    """A transient blip must not pin the platform to the in-process store forever.

    Hindsight is the mandated organizational memory, so the next successful health probe has to
    put it back in service. The probe is cleared to force a fresh check inside the call.
    """

    class FlakyStore:
        name = "hindsight"

        def __init__(self) -> None:
            self.healthy = False

        def health(self) -> dict[str, Any]:
            return {"backend": "hindsight", "healthy": self.healthy, "detail": "probe"}

        def ensure_bank(self, **kwargs: Any) -> None:
            return None

        def stats(self) -> dict[str, Any]:
            return {"backend": "hindsight", "bank_id": "test-bank", "counts_available": True}

    flaky = FlakyStore()
    had_hindsight = memory_service._hindsight  # noqa: SLF001
    memory_service._hindsight = flaky  # noqa: SLF001
    try:
        memory_service._record_degradation("transient startup race")  # noqa: SLF001
        assert memory_service.degraded is True
        # Still failing: the fallback is the honest answer.
        assert memory_service._active_store().name == "fallback"  # noqa: SLF001

        flaky.healthy = True
        memory_service._health_checked_at = 0.0  # noqa: SLF001 - force a fresh probe
        active = memory_service._active_store()  # noqa: SLF001
        assert active is flaky, "a healthy Hindsight must be put back in service"
        assert memory_service.degraded is False
        assert memory_service.degradation_reasons == []
    finally:
        memory_service._hindsight = had_hindsight  # noqa: SLF001
        memory_service.clear_degradation()


# -- the background clock must yield to a scripted scenario ----------------------------


def test_ticker_does_not_step_the_world_while_paused(monkeypatch) -> None:
    """Two writers advancing one simulation is what made a demo POST take six minutes."""
    from contextlib import contextmanager

    import app.workers.ticker as ticker_module
    from app.workers.ticker import SimulationTicker

    advanced: list[float] = []

    class SpyEngine:
        def __init__(self, db: Any) -> None:
            pass

        def advance(self, seconds: float, **kwargs: Any) -> None:
            advanced.append(seconds)

    class Running:
        running = True

    @contextmanager
    def fake_session_scope():
        yield None

    monkeypatch.setattr(ticker_module, "SimulationEngine", SpyEngine)
    monkeypatch.setattr(ticker_module, "session_scope", fake_session_scope)
    monkeypatch.setattr(ticker_module, "world", lambda: Running())

    ticker = SimulationTicker(interval_ms=10)
    assert ticker.paused is False

    ticker.pause()
    assert ticker.paused is True
    ticker.start()
    try:
        time.sleep(0.15)
        assert advanced == [], "the ticker advanced the world while a scenario owned the clock"
    finally:
        ticker.resume()
        # Give the loop a moment to notice that the clock is free again.
        deadline = time.monotonic() + 2.0
        while not advanced and time.monotonic() < deadline:
            time.sleep(0.01)
        ticker.stop()

    assert advanced, "the ticker must resume advancing once the scenario releases the clock"


def test_exclusive_clock_releases_the_ticker_even_if_the_body_raises() -> None:
    from app.workers.ticker import SimulationTicker

    ticker = SimulationTicker(interval_ms=10)
    with pytest.raises(RuntimeError):
        with ticker.exclusive_clock():
            assert ticker.paused is True
            raise RuntimeError("scenario blew up")
    assert ticker.paused is False, "a failed scenario must not leave the clock held forever"


def test_recall_hits_keep_their_retrieval_arms_when_present() -> None:
    """Explainability depends on knowing which retrieval arm surfaced a memory."""
    hit = RecallHit(id="1", text="t", retrieval=["semantic", "keyword"])
    assert hit.retrieval == ["semantic", "keyword"]
    assert isinstance(threading.current_thread(), threading.Thread)
