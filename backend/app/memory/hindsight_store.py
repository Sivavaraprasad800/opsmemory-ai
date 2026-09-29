"""Adapter for the real Hindsight service.

Thin on purpose. All platform logic lives in ``app.memory.service``; this class only
translates between the platform's value objects and the ``hindsight-client`` SDK, and
converts every transport failure into ``MemoryUnavailable`` so the service layer can degrade
instead of crashing the request (build spec section 48).
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Sequence

from app.memory.base import (
    EXPERIENCE,
    OBSERVATION,
    WORLD,
    MemoryUnavailable,
    RecallHit,
    RecallResult,
    ReflectionResult,
    RetentionResult,
)

logger = logging.getLogger(__name__)

# The Hindsight SDK can drive an asyncio transport internally (aiohttp). Calling a sync SDK
# method from a thread that already has a running event loop fails with "This event loop is
# already running" - which is exactly the situation inside FastAPI's lifespan and async routes.
# Every SDK call therefore hops onto this dedicated single-worker thread, which never has a
# loop, so the same code path works from a sync script, a threadpool request handler and an
# async startup hook. One worker keeps calls serialised, matching the single bank they target.
_SDK_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hindsight-sdk")

# Counting the bank costs a network round trip, so the answer is cached for this long. The
# dashboard polls health far more often than memories are retained.
STATS_TTL_SECONDS = 30.0
STATS_MAX_ITEMS = 500


def _in_sdk_thread(call: Callable[..., Any], **kwargs: Any) -> Any:
    """Run one synchronous Hindsight SDK call on a thread that has no running event loop."""
    return _SDK_EXECUTOR.submit(call, **kwargs).result()


class HindsightStore:
    """`retain` / `recall` / `reflect` against a Hindsight memory bank."""

    name = "hindsight"

    def __init__(
        self,
        *,
        base_url: str,
        bank_id: str,
        api_key: str = "",
        timeout: float = 30.0,
    ) -> None:
        try:
            from hindsight_client import Hindsight  # imported lazily: optional dependency
        except ImportError as exc:  # pragma: no cover - depends on installed extras
            raise MemoryUnavailable(
                "hindsight-client is not installed; cannot use the real Hindsight backend"
            ) from exc

        self.bank_id = bank_id
        self.base_url = base_url.rstrip("/")
        kwargs: dict[str, Any] = {"base_url": self.base_url, "timeout": timeout}
        if api_key:
            kwargs["api_key"] = api_key
        self._client = Hindsight(**kwargs)
        self._bank_ready = False
        self._stats_cache: dict[str, Any] | None = None
        self._stats_cached_at = 0.0

    # -- bank --------------------------------------------------------------------------
    def ensure_bank(self, *, mission: str, directives: Sequence[str] = ()) -> None:
        if self._bank_ready:
            return
        try:
            _in_sdk_thread(
                self._client.create_bank,
                bank_id=self.bank_id,
                name="OpsMemory AI — organizational SRE experience",
                mission=mission,
                disposition={"skepticism": 4, "literalism": 4, "empathy": 2},
            )
        except Exception as exc:  # noqa: BLE001 - bank may already exist
            logger.info("create_bank returned %s (bank likely already exists)", exc)
        for index, directive in enumerate(directives):
            try:
                if hasattr(self._client, "create_directive"):
                    # create_directive(bank_id, name, content, priority=0, is_active=True)
                    _in_sdk_thread(
                        self._client.create_directive,
                        bank_id=self.bank_id,
                        name=f"opsmemory-directive-{index + 1}",
                        content=directive,
                        priority=index,
                    )
            except Exception as exc:  # noqa: BLE001 - directives are an enhancement, not a gate
                logger.info("could not add directive: %s", exc)
        self._bank_ready = True

    # -- retain ------------------------------------------------------------------------
    def retain(
        self,
        *,
        content: str,
        context: str = "",
        document_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: Sequence[str] = (),
        occurred_at: str | None = None,
    ) -> RetentionResult:
        from datetime import datetime

        kwargs: dict[str, Any] = {"bank_id": self.bank_id, "content": content}
        if context:
            kwargs["context"] = context
        if document_id:
            kwargs["document_id"] = document_id
        if metadata:
            kwargs["metadata"] = {str(k): str(v) for k, v in metadata.items()}
        if occurred_at:
            try:
                kwargs["timestamp"] = datetime.fromisoformat(occurred_at)
            except ValueError:
                logger.debug("unparseable occurred_at %r; using server time", occurred_at)

        try:
            response = _in_sdk_thread(self._client.retain, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise MemoryUnavailable(f"Hindsight retain failed: {exc}") from exc

        memory_id = self._extract_id(response) or (document_id or "")
        return RetentionResult(
            memory_id=memory_id,
            document_id=document_id or memory_id,
            backend=self.name,
            accepted=True,
            detail="retained via Hindsight",
        )

    def retain_batch(
        self,
        *,
        items: Sequence[dict[str, Any]],
        document_id: str | None = None,
        context: str = "",
    ) -> RetentionResult:
        try:
            response = _in_sdk_thread(
                self._client.retain_batch,
                bank_id=self.bank_id,
                items=list(items),
                document_id=document_id,
                context=context,
            )
        except Exception as exc:  # noqa: BLE001
            raise MemoryUnavailable(f"Hindsight retain_batch failed: {exc}") from exc
        memory_id = self._extract_id(response) or (document_id or "")
        return RetentionResult(
            memory_id=memory_id,
            document_id=document_id or memory_id,
            backend=self.name,
            facts_extracted=len(items),
            detail="batch retained via Hindsight",
        )

    # -- recall ------------------------------------------------------------------------
    def recall(
        self,
        *,
        query: str,
        types: Sequence[str] = (WORLD, EXPERIENCE, OBSERVATION),
        budget: str = "mid",
        max_tokens: int = 2048,
        tags: Sequence[str] = (),
        top_k: int = 8,
    ) -> RecallResult:
        # Hindsight rejects queries over 500 tokens; keep the generated query well inside that.
        if len(query) > 1800:
            query = query[:1800]

        kwargs: dict[str, Any] = {
            "bank_id": self.bank_id,
            "query": query,
            "budget": budget,
        }
        if types:
            kwargs["types"] = list(types)
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if tags:
            kwargs["tags"] = list(tags)

        try:
            response = _in_sdk_thread(self._client.recall, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise MemoryUnavailable(f"Hindsight recall failed: {exc}") from exc

        raw_results = getattr(response, "results", None)
        if raw_results is None and isinstance(response, dict):
            raw_results = response.get("results", [])

        hits: list[RecallHit] = []
        for item in raw_results or []:
            hits.append(self._to_hit(item))

        return RecallResult(
            query=query,
            hits=hits[:top_k] if top_k else hits,
            backend=self.name,
            note="retrieved via Hindsight TEMPR",
        )

    def reflect(self, *, query: str, context: str = "", budget: str = "mid") -> ReflectionResult:
        kwargs: dict[str, Any] = {"bank_id": self.bank_id, "query": query, "budget": budget}
        if context:
            kwargs["context"] = context
        try:
            answer = _in_sdk_thread(self._client.reflect, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise MemoryUnavailable(f"Hindsight reflect failed: {exc}") from exc

        text = getattr(answer, "text", None)
        if text is None and isinstance(answer, dict):
            text = answer.get("text", "")
        citations: list[str] = []
        for attr in ("citations", "sources"):
            value = getattr(answer, attr, None)
            if isinstance(value, list):
                citations = [str(getattr(v, "id", v)) for v in value]
                break
        return ReflectionResult(text=text or "", backend=self.name, citations=citations)

    # -- introspection -----------------------------------------------------------------
    def list_memories(
        self, *, memory_type: str | None = None, search: str | None = None, limit: int = 100
    ) -> list[RecallHit]:
        kwargs: dict[str, Any] = {"bank_id": self.bank_id, "limit": limit, "offset": 0}
        if memory_type:
            kwargs["type"] = memory_type
        if search:
            kwargs["search_query"] = search
        try:
            response = _in_sdk_thread(self._client.list_memories, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise MemoryUnavailable(f"Hindsight list_memories failed: {exc}") from exc

        return [self._to_hit(item) for item in self._items_of(response)]

    def stats(self) -> dict[str, Any]:
        """Count what the bank actually holds.

        The bank - not the local ledger - is the source of truth for what the organization
        remembers, so the dashboard must read the counts from here. Listing memories is a
        network round trip, so the answer is cached briefly. When the bank cannot be counted
        the counts are reported as *unknown* rather than zero: showing "0 documents" next to a
        ledger that lists five retained experiences is a lie, and it is the kind of lie that
        makes a memory product look broken (build spec section 48).
        """
        now = time.monotonic()
        if self._stats_cache is not None and now - self._stats_cached_at < STATS_TTL_SECONDS:
            return dict(self._stats_cache)

        base: dict[str, Any] = {"backend": self.name, "bank_id": self.bank_id}
        try:
            response = _in_sdk_thread(
                self._client.list_memories,
                bank_id=self.bank_id,
                limit=STATS_MAX_ITEMS,
                offset=0,
            )
        except Exception as exc:  # noqa: BLE001 - a count failure must not break the dashboard
            logger.info("could not count the Hindsight bank: %s", exc)
            return {**base, "counts_available": False}

        items = self._items_of(response)
        # ``total`` is the bank's own size, so it stays correct even when the page of items is
        # capped; the per-type breakdown is counted from the page we actually received.
        total = getattr(response, "total", None)
        facts = total if isinstance(total, int) else len(items)

        by_type: dict[str, int] = {}
        documents: set[str] = set()
        entities: set[str] = set()
        for item in items:
            fact_type = str(self._field(item, "fact_type") or self._field(item, "type") or WORLD)
            by_type[fact_type] = by_type.get(fact_type, 0) + 1
            document_id = self._field(item, "document_id")
            if document_id:
                documents.add(str(document_id))
            entities.update(self._string_list(self._field(item, "entities")))

        stats = {
            **base,
            "counts_available": True,
            "facts": facts,
            "observations": by_type.get(OBSERVATION, 0),
            "documents": len(documents),
            "entities": len(entities),
            "facts_by_type": by_type,
            "bank_size_capped": len(items) >= STATS_MAX_ITEMS,
        }
        self._stats_cache = stats
        self._stats_cached_at = now
        return dict(stats)

    def health(self) -> dict[str, Any]:
        try:
            version = _in_sdk_thread(self._client.get_version)
        except Exception as exc:  # noqa: BLE001
            return {"backend": self.name, "healthy": False, "detail": str(exc)}
        api_version = getattr(version, "api_version", None) or "unknown"
        # The counts ride along with the health verdict, exactly as the in-process store does,
        # because the dashboard reads them from one place. ``stats`` is cached, so polling
        # health does not mean polling the bank.
        return {
            "backend": self.name,
            "healthy": True,
            "base_url": self.base_url,
            "bank_id": self.bank_id,
            "api_version": api_version,
            "detail": "connected to Hindsight",
            **{k: v for k, v in self.stats().items() if k != "backend"},
        }

    # -- helpers -----------------------------------------------------------------------
    @staticmethod
    def _string_list(value: Any) -> list[str]:
        """Normalise a list-or-delimited-string field into a list of strings.

        The API returns ``entities`` as a comma-separated string for some memory types and as a
        list for others. Iterating the string directly yields one entry per *character* - a
        plausible-looking list of nonsense - so it is split explicitly.
        """
        if value is None:
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(part) for part in value if str(part).strip()]
        return [str(value)]

    @staticmethod
    def _field(item: Any, name: str) -> Any:
        """Read a field from an SDK model, a dataclass or a plain mapping alike."""
        if isinstance(item, dict):
            return item.get(name)
        return getattr(item, name, None)

    @classmethod
    def _items_of(cls, response: Any) -> list[Any]:
        """Pull the list of memory units out of a Hindsight list response.

        The API returns ``ListMemoryUnitsResponse(items=[...], total=N)``. Reading the wrong
        attribute here fails *silently* - an empty list looks exactly like an empty bank - which
        is how a bank holding 44 memories ends up rendering as "nothing retained yet" all over
        the dashboard. Every spelling the SDK has used is accepted, and an unrecognised shape is
        logged rather than quietly flattened to zero.
        """
        if isinstance(response, dict):
            for key in ("items", "memories", "results"):
                value = response.get(key)
                if isinstance(value, list):
                    return value
            if response:
                logger.info("unrecognised Hindsight list payload keys: %s", sorted(response)[:8])
            return []
        for name in ("items", "memories", "results"):
            value = getattr(response, name, None)
            if isinstance(value, list):
                return value
        # A bare list is also acceptable.
        if isinstance(response, list):
            return response
        logger.info("unrecognised Hindsight list response: %s", type(response).__name__)
        return []

    @staticmethod
    def _extract_id(response: Any) -> str | None:
        if response is None:
            return None
        if isinstance(response, str):
            return response
        if isinstance(response, dict):
            for key in ("id", "memory_id", "document_id"):
                if response.get(key):
                    return str(response[key])
            return None
        for attr in ("id", "memory_id", "document_id"):
            value = getattr(response, attr, None)
            if value:
                return str(value)
        return None

    @classmethod
    def _to_hit(cls, item: Any) -> RecallHit:
        def get(name: str, default: Any = None) -> Any:
            value = cls._field(item, name)
            return default if value is None else value

        # The API names the kind of memory ``fact_type``; it is what the platform calls ``type``.
        # Getting this wrong classifies every observation as a world fact and empties the
        # "Observations" view even though the bank is full of them.
        kind = get("type", None) or get("fact_type", None) or WORLD
        source_ids = get("source_fact_ids", None) or get("source_memory_ids", None) or []
        occurred_start = get("occurred_start", None) or get("var_date", None) or get("date", None)
        return RecallHit(
            id=str(get("id", "")),
            text=str(get("text", "")),
            type=str(kind),
            score=float(get("score", 0.0) or 0.0),
            context=str(get("context", "") or ""),
            metadata=dict(get("metadata", {}) or {}),
            tags=cls._string_list(get("tags", None)),
            entities=cls._string_list(get("entities", None)),
            occurred_start=_iso(occurred_start),
            occurred_end=_iso(get("occurred_end", None)),
            mentioned_at=_iso(get("mentioned_at", None) or occurred_start),
            document_id=_iso(get("document_id", None)),
            proof_count=get("proof_count", None),
            source_fact_ids=[str(s) for s in source_ids],
        )


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)
