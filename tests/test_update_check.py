import json

import pytest
import pytest_asyncio

from lora_explorer import __version__
from lora_explorer import update_check
from lora_explorer.game.database import Database


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(db_path=str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.close()


class _FakeResp:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    def __init__(self, json_data=None, raise_exc=None, **_):
        self._json = json_data
        self._raise = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        if self._raise:
            raise self._raise
        return _FakeResp(200, self._json)


def _patch_client(monkeypatch, **kwargs):
    monkeypatch.setattr(update_check.httpx, "AsyncClient", lambda **_: _FakeAsyncClient(**kwargs))


# --- version comparison -------------------------------------------------------

def test_is_newer_true_for_a_higher_version():
    assert update_check.is_newer("v0.3.0", "0.2.0") is True


def test_is_newer_false_for_same_or_lower():
    assert update_check.is_newer("0.2.0", "0.2.0") is False
    assert update_check.is_newer("0.1.0", "0.2.0") is False


def test_is_newer_ignores_leading_v_and_suffix():
    assert update_check.is_newer("v0.3.0-rc1", "0.2.0") is True


# --- check_now -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_now_reports_update_available(db, monkeypatch):
    _patch_client(monkeypatch, json_data={"tag_name": "v9.9.9", "html_url": "https://example/releases/v9.9.9"})
    result = await update_check.check_now(db)
    assert result["ok"] is True
    assert result["update_available"] is True
    assert result["latest_version"] == "v9.9.9"
    assert result["current_version"] == __version__


@pytest.mark.asyncio
async def test_check_now_reports_up_to_date(db, monkeypatch):
    _patch_client(monkeypatch, json_data={"tag_name": f"v{__version__}", "html_url": "https://example"})
    result = await update_check.check_now(db)
    assert result["ok"] is True
    assert result["update_available"] is False


@pytest.mark.asyncio
async def test_check_now_never_raises_on_network_failure(db, monkeypatch):
    _patch_client(monkeypatch, raise_exc=Exception("boom"))
    result = await update_check.check_now(db)
    assert result["ok"] is False
    assert "error" in result


@pytest.mark.asyncio
async def test_check_now_logs_failure_at_warning(db, monkeypatch, caplog):
    """The app runs at INFO by default (main.py), so a debug-level log here
    is invisible in `docker logs` — exactly where someone troubleshooting a
    failed "Check now" click would look."""
    import logging
    _patch_client(monkeypatch, raise_exc=Exception("Temporary failure in name resolution"))
    with caplog.at_level(logging.INFO, logger="lora_explorer.update_check"):
        await update_check.check_now(db)
    assert any(
        r.levelno >= logging.WARNING and "Temporary failure in name resolution" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_check_now_caches_result(db, monkeypatch):
    _patch_client(monkeypatch, json_data={"tag_name": "v9.9.9", "html_url": "https://example"})
    await update_check.check_now(db)
    cached = await update_check.get_cached(db)
    assert cached["latest_version"] == "v9.9.9"


@pytest.mark.asyncio
async def test_get_cached_returns_none_before_any_check(db):
    assert await update_check.get_cached(db) is None


# --- opt-in toggle -------------------------------------------------------------

@pytest.mark.asyncio
async def test_disabled_by_default(db):
    assert await update_check.is_enabled(db) is False


@pytest.mark.asyncio
async def test_set_enabled_round_trips(db):
    await update_check.set_enabled(db, True)
    assert await update_check.is_enabled(db) is True
    await update_check.set_enabled(db, False)
    assert await update_check.is_enabled(db) is False


# --- loop never calls the network while disabled --------------------------------

@pytest.mark.asyncio
async def test_loop_makes_no_request_while_disabled(db, monkeypatch):
    calls = []
    monkeypatch.setattr(update_check.httpx, "AsyncClient", lambda **_: calls.append(1) or _FakeAsyncClient())

    async def _one_tick():
        if await update_check.is_enabled(db):
            await update_check.check_now(db)

    await _one_tick()
    assert calls == []
