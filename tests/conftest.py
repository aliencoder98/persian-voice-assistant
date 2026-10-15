import datetime as dt

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models import Contact, User
from app.tools import impl  # noqa: F401  (registers tools)
from app.tools.registry import TEHRAN, ToolContext


@pytest.fixture(scope="session")
def engine():
    """A throwaway database next to the real one, created from the models."""
    s = get_settings()
    test_db = f"{s.postgres_db}_test"
    admin = create_engine(s.database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{test_db}"'))
        conn.execute(text(f'CREATE DATABASE "{test_db}"'))
    admin.dispose()
    eng = create_engine(s.database_url.set(database=test_db))
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    """Each test runs in a transaction that is rolled back; commit() only releases a savepoint."""
    conn = engine.connect()
    outer = conn.begin()
    sess = Session(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
    yield sess
    sess.close()
    outer.rollback()
    conn.close()


FIXED_NOW = dt.datetime(2026, 10, 14, 10, 30, tzinfo=TEHRAN)  # Wednesday, 22 Mehr 1405


@pytest.fixture
def user(session):
    u = User(telegram_user_id=111, name="Owner", email="owner@example.com")
    session.add(u)
    session.flush()
    session.add_all(
        [
            Contact(user_id=u.id, name="علی رضایی", aliases=["علی", "Ali"], email="ali@example.com"),
            Contact(user_id=u.id, name="سارا محمدی", aliases=["سارا", "Sara"], email="sara@example.com"),
        ]
    )
    session.flush()
    return u


@pytest.fixture
def ctx(session, user):
    return ToolContext(session=session, user=user, now=lambda: FIXED_NOW)
