"""Background simulation clock.

The simulated environment advances on a daemon thread so the dashboard has live motion. It is
explicitly stoppable, and it is disabled in tests (``SIM_AUTOSTART=false``) so every scenario is
deterministic. Detection is *not* run on this thread — only telemetry generation is — so a slow
detection pass can never stall the clock.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator

from app.core.config import settings
from app.database.session import session_scope
from app.sim.engine import SimulationEngine, world

logger = logging.getLogger(__name__)

# How often detection runs over ingested telemetry when the platform is bound to wall clock.
DETECTION_TICK_SECONDS = 15.0


class SimulationTicker:
    def __init__(self, *, interval_ms: int | None = None) -> None:
        self.interval = (interval_ms or settings.sim_tick_ms) / 1000.0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Set while somebody else owns the clock. The background loop must not step the world
        # underneath a scripted scenario: two writers advancing the same simulation produces
        # interleaved telemetry, a wildly inflated row count, and a demo that takes minutes
        # because every tick fights the request for the same lock and the same SQLite file.
        self._paused = threading.Event()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    @contextmanager
    def exclusive_clock(self) -> Iterator[None]:
        """Hold the simulation clock so a long-running scenario runs against a still world."""
        self._paused.set()
        try:
            yield
        finally:
            self._paused.clear()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="opsmemory-sim-ticker", daemon=True)
        self._thread.start()
        logger.info("simulation ticker started (interval %.3fs)", self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        logger.info("simulation ticker stopped")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if world().running and not self._paused.is_set():
                    with session_scope() as db:
                        self._tick(db)
            except Exception as exc:  # noqa: BLE001 - the ticker must never die silently
                logger.warning("simulation tick failed: %s", exc)
            self._stop.wait(self._interval_for_mode())

    def _interval_for_mode(self) -> float:
        """Detection is far more expensive than a simulation step, so the real clock ticks
        slower. Fifteen seconds is comfortably inside the detector's five-minute window, so an
        incident is still opened promptly after the symptoms appear."""
        return self.interval if not settings.real_clock else DETECTION_TICK_SECONDS

    def _tick(self, db) -> None:
        if settings.real_clock:
            # Real-clock mode has nothing to simulate: the telemetry is being pushed by a
            # connected project. So the ticker's job becomes running detection over it, which is
            # what makes the loop automatic - a connector only has to send data, and incidents
            # open, get investigated and reach memory on their own.
            from app.domain.incidents import detect_and_open_incidents

            opened = detect_and_open_incidents(db, SimulationEngine(db))
            if opened:
                logger.info("detection opened %d incident(s) from ingested telemetry", len(opened))
            return
        SimulationEngine(db).advance(settings.sim_seconds_per_tick)


ticker = SimulationTicker()
