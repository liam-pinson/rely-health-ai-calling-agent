import datetime
import os
import uuid

# Point at a dedicated test database *before* any `app.*` module is
# imported -- app.config reads DATABASE_URL at import time, and
# load_dotenv() defaults to not overriding already-set env vars, so this
# wins over whatever's in backend/.env (the real dev/demo database).
os.environ["DATABASE_URL"] = (
    "postgresql://postgres:postgres@localhost:5432/ai_calling_agent_test"
)
os.environ.setdefault("RETELL_API_KEY", "test-retell-api-key")
os.environ.setdefault("RETELL_FROM_NUMBER", "+15550000000")
os.environ.setdefault("PROVIDER", "retell")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.models import Base, CallLog, Patient  # noqa: E402

ADMIN_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/postgres"


def _ensure_test_database_exists() -> None:
    admin_engine = create_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": "ai_calling_agent_test"},
            ).scalar()
            if not exists:
                conn.execute(text("CREATE DATABASE ai_calling_agent_test"))
    finally:
        admin_engine.dispose()


_ensure_test_database_exists()

# Imported only after DATABASE_URL is pointed at the test database, so
# app.db builds its engine/session against it.
from app.db import engine as app_engine  # noqa: E402
from app.main import app  # noqa: E402

Base.metadata.create_all(app_engine)

TestSessionLocal = sessionmaker(bind=app_engine)


@pytest.fixture(autouse=True)
def clean_db():
    """Truncate everything before each test for isolation -- simpler and
    less error-prone than fighting the app's own db.commit() calls with
    a rollback/savepoint pattern.
    """
    with app_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE TABLE webhook_events, transcript_turns, call_logs, patients CASCADE"
            )
        )
    yield


@pytest.fixture
def db_session():
    session = TestSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def patient(db_session):
    p = Patient(
        id=uuid.uuid4(),
        first_name="Test",
        last_name="Patient",
        date_of_birth=datetime.date(1990, 1, 1),
        phone_number="+15555550100",
        appointment_date=datetime.date.today(),
        appointment_time=datetime.time(9, 0),
        timezone="America/New_York",
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def make_call_log(db_session):
    def _make(
        patient_id,
        status: str,
        provider_call_id: str | None = None,
        started_at: datetime.datetime | None = None,
    ) -> CallLog:
        call = CallLog(
            call_id=uuid.uuid4(),
            patient_id=patient_id,
            provider_call_id=provider_call_id,
            status=status,
            started_at=started_at or datetime.datetime.now(datetime.timezone.utc),
        )
        db_session.add(call)
        db_session.commit()
        db_session.refresh(call)
        return call

    return _make