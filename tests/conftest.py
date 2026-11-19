from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from db import JournalDB

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_XML = FIXTURES_DIR / "sample.xml"


@pytest.fixture
def sample_xml(tmp_path: Path) -> Path:
    """Копирует sample.xml во временную директорию."""
    dst = tmp_path / "EventLog.xml"
    shutil.copy(SAMPLE_XML, dst)
    return dst


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.duckdb"


@pytest.fixture
def populated_db(db_path: Path, sample_xml: Path) -> JournalDB:
    """Создаёт БД и импортирует sample.xml."""
    with JournalDB(db_path, read_only=False) as db:
        db.import_file(sample_xml)
    return db


@pytest.fixture
def api_client(populated_db: JournalDB, monkeypatch: pytest.MonkeyPatch):
    """TestClient FastAPI с уже заполненной БД."""
    import api
    monkeypatch.setattr(api, "_DB_PATH", populated_db.path)
    # Пересоздаём app, чтобы lifespan увидел правильный путь
    from api import app
    with TestClient(app) as client:
        yield client