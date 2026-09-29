"""Test configuration.

Environment variables are set **before** any ``app.*`` module is imported, because
``app.core.config.settings`` and the SQLAlchemy engine are created at import time. Getting this
order wrong would silently point the suite at the developer's real database.

The suite runs entirely offline: no OpenAI key and no Hindsight server, which means it exercises
the deterministic reasoner and the in-process memory store. That is deliberate — the tool ledger,
the evidence trail, the safety gate and the verification engine are the same code paths in both
modes, so offline tests still cover the behaviour that matters.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

TEST_DB_PATH = Path(tempfile.gettempdir()) / "opsmemory_test.db"
MEMORY_PATH = Path(tempfile.gettempdir()) / "opsmemory_test_memory.json"

os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH.as_posix()}"
os.environ["ENVIRONMENT"] = "development"
os.environ["SECRET_KEY"] = "test-secret-key-that-is-long-enough-to-be-plausible"
os.environ["OPENAI_API_KEY"] = ""
os.environ["HINDSIGHT_BASE_URL"] = ""
os.environ["HINDSIGHT_API_KEY"] = ""
os.environ["SIM_AUTOSTART"] = "false"
os.environ["LOG_LEVEL"] = "CRITICAL"
os.environ["HINDSIGHT_BANK_ID"] = "opsmemory-test"

import pytest  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.database.models import Incident, Service  # noqa: E402
from app.database.seed import clean_room, seed_catalogue  # noqa: E402
from app.database.session import SessionLocal, init_db  # noqa: E402
from app.domain.incidents import detect_and_open_incidents, reset_detection_state  # noqa: E402
from app.memory.service import get_memory_service, reset_memory_service  # noqa: E402
from app.sim.engine import SimulationEngine, world  # noqa: E402


def pytest_configure(config: pytest.Config) -> None:
    assert settings.is_sqlite, "the test suite must run against SQLite"
    # Compare on the file name only: the URL uses forward slashes while Path uses the OS separator.
    assert TEST_DB_PATH.name in settings.database_url, "tests must not touch the real database"
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(TEST_DB_PATH) + suffix)
        if candidate.exists():
            candidate.unlink()


@pytest.fixture(scope="session", autouse=True)
def _schema() -> None:
    init_db()
    with SessionLocal() as db:
        seed_catalogue(db)
        db.commit()


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def memory_service():
    """A memory service whose store is wiped between tests."""
    reset_memory_service()
    service = get_memory_service()
    service.reset()
    if service._fallback.persist_path and service._fallback.persist_path.exists():
        service._fallback.persist_path.unlink()
    service._fallback.reset()
    return service


TEST_SERVICES = ("payment-service", "order-service", "checkout-service")
"""Services that get a runtime in tests.

Simulating the whole estate would generate nine services' worth of telemetry per test for no
additional coverage, so tests simulate only the services they exercise.
"""

WARMUP_SECONDS = 2100.0
"""Enough history for the 300s recent window and the 1800s reference window to be populated."""


@pytest.fixture()
def clean_world(db, memory_service):
    """Clean-room: wipe dynamic state, reset the world, rebuild a healthy baseline.

    Deliberately *not* autouse, because it is the expensive fixture in this suite; only the
    tests that need a simulated estate (and therefore a baseline to detect against) request it.
    """
    clean_room(db, memory=get_memory_service())
    seed_catalogue(db)
    db.commit()
    reset_detection_state()
    simulation = SimulationEngine(db)
    simulation.load_catalogue(only=TEST_SERVICES)
    simulation.advance(WARMUP_SECONDS)
    db.commit()
    yield simulation
    world().reset()
    reset_detection_state()


@pytest.fixture()
def engine(clean_world, db):
    simulation = SimulationEngine(db)
    simulation.load_catalogue(only=TEST_SERVICES)
    return simulation


@pytest.fixture()
def open_incident(db, engine):
    """Factory: drive the simulator until an incident opens, and return it."""

    def _open(
        scenario: str = "connection_exhaustion",
        service: str = "payment-service",
        environment: str = "production",
        max_rounds: int = 10,
        step_seconds: float = 90.0,
    ) -> Incident:
        engine.inject_fault(scenario=scenario, service_name=service, environment=environment)
        db.flush()
        for _ in range(max_rounds):
            engine.advance(step_seconds)
            db.flush()
            opened = detect_and_open_incidents(db, engine)
            if opened:
                db.commit()
                return opened[0]
        raise AssertionError(
            f"the simulator never produced an incident for scenario={scenario} service={service} "
            f"after {max_rounds} rounds; check the detector thresholds or the scenario leak rate"
        )

    return _open


@pytest.fixture()
def user_tokens(db, memory_service):
    """Bearer tokens for each seeded role, obtained through the real login endpoint."""
    from app.api.deps import reset_rate_limits
    from app.main import app
    from fastapi.testclient import TestClient

    reset_rate_limits()
    client = TestClient(app)
    tokens: dict[str, str] = {}
    for role, email in (
        ("admin", "admin@acme.test"),
        ("sre", "sre@acme.test"),
        ("analyst", "analyst@acme.test"),
        ("viewer", "viewer@acme.test"),
    ):
        response = client.post("/api/auth/login", json={"email": email, "password": "password123"})
        assert response.status_code == 200, response.text
        tokens[role] = response.json()["access_token"]
    return tokens


@pytest.fixture()
def client(user_tokens, clean_world):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {user_tokens['sre']}"})
        yield test_client


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:  # noqa: ARG001
    # Best-effort cleanup. On Windows the SQLAlchemy engine may still hold the file open, which
    # is harmless for the next run (the file is truncated at session start) so failures here are
    # deliberately swallowed rather than turning a green suite red.
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(TEST_DB_PATH) + suffix)
        try:
            if candidate.exists():
                candidate.unlink()
        except PermissionError:
            pass
    shutil.rmtree(str(MEMORY_PATH.parent / ".opsmemory"), ignore_errors=True)
