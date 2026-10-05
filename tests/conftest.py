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


@pytest.fixture(autouse=True)
def hundred_point_wallets(request, monkeypatch):
    """Most tests work out balances by hand from 100-point wallets. The
    rules are the same at any starting balance, so they run on 100. The
    shipped default (1,000) is checked by tests marked real_economy."""
    if request.node.get_closest_marker("real_economy"):
        return
    from mpr import economy
    monkeypatch.setattr(economy, "STARTING_BALANCE", 100)
    monkeypatch.setattr(economy, "RAIL_FLOOR", 100)


def pytest_configure(config):
    config.addinivalue_line("markers", "real_economy: run with the shipped economy settings")
