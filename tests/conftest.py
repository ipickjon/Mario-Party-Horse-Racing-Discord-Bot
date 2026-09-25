import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Every test gets its own database file."""
    from mpr import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init()
    return db
