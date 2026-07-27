import time
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from lora_explorer.game.database import Database
from lora_explorer.community.client import CommunityClient

import httpx


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(db_path=str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.close()


# --- Database migration & methods ---


@pytest.mark.asyncio
async def test_migration_adds_community_columns(db):
    player = await db.get_or_create_player("p1", 33.0, -112.0)
    assert player["discord_linked"] == 0
    assert player["community_api_key"] is None
    assert player["community_linked_at"] is None


@pytest.mark.asyncio
async def test_link_community(db):
    await db.get_or_create_player("p1", 33.0, -112.0)
    await db.link_community("p1", "test-api-key-123")
    player = await db.get_player("p1")
    assert player["discord_linked"] == 1
    assert player["community_api_key"] == "test-api-key-123"
    assert player["community_linked_at"] is not None
    assert player["community_linked_at"] <= int(time.time())


@pytest.mark.asyncio
async def test_unlink_community(db):
    await db.get_or_create_player("p1", 33.0, -112.0)
    await db.link_community("p1", "test-api-key-123")
    await db.unlink_community("p1")
    player = await db.get_player("p1")
    assert player["discord_linked"] == 0
    assert player["community_api_key"] is None
    assert player["community_linked_at"] is None


@pytest.mark.asyncio
async def test_get_community_stats_no_player(db):
    result = await db.get_community_stats("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_get_community_stats(db):
    await db.get_or_create_player("p1", 33.0, -112.0)
    await db.add_xp("p1", 500)
    await db.discover_hex("p1", "abc123")
    await db.discover_hex("p1", "def456")
    await db.award_postcard("p1", "Signal Strength", 3, "Strong signal", 5.0, -6.0)
    await db.award_postcard("p1", "Distance", 4, "Far away", 15.0, None)

    stats = await db.get_community_stats("p1")
    assert stats["rank"] == 1
    assert stats["xp"] == 500
    assert stats["hex_count"] == 2
    assert stats["postcard_count"] == 2
    assert stats["highest_star"] == 4
    assert stats["active_posts"] == 0
    assert stats["total_distance"] == 0.0


@pytest.mark.asyncio
async def test_get_community_stats_no_postcards(db):
    await db.get_or_create_player("p1", 33.0, -112.0)
    stats = await db.get_community_stats("p1")
    assert stats["highest_star"] == 0
    assert stats["postcard_count"] == 0


# --- CommunityClient ---


def _mock_response(status, json=None):
    req = httpx.Request("POST", "http://fake-server.local/api/test")
    return httpx.Response(status, json=json, request=req)


@pytest.mark.asyncio
async def test_client_link_success():
    client = CommunityClient("http://fake-server.local")
    with patch.object(client._http, "post", new_callable=AsyncMock, return_value=_mock_response(200, {"discord_id": "12345", "name": "Explorer"})):
        result = await client.link("MESA-7K", "my-api-key")
    assert result["ok"] is True
    assert result["discord_id"] == "12345"
    await client.close()


@pytest.mark.asyncio
async def test_client_link_rejected():
    client = CommunityClient("http://fake-server.local")
    with patch.object(client._http, "post", new_callable=AsyncMock, return_value=_mock_response(400, {"error": "invalid code"})):
        result = await client.link("BAD-CODE", "my-api-key")
    assert result["ok"] is False
    assert "error" in result
    await client.close()


@pytest.mark.asyncio
async def test_client_link_network_error():
    client = CommunityClient("http://fake-server.local")
    with patch.object(client._http, "post", new_callable=AsyncMock, side_effect=httpx.ConnectError("refused")):
        result = await client.link("MESA-7K", "my-api-key")
    assert result["ok"] is False
    await client.close()


@pytest.mark.asyncio
async def test_client_post_stats_success():
    client = CommunityClient("http://fake-server.local")
    with patch.object(client._http, "post", new_callable=AsyncMock, return_value=_mock_response(200, {"ok": True})) as mock_post:
        ok = await client.post_stats("my-key", {"rank": 3, "xp": 1000})
    assert ok is True
    call_kwargs = mock_post.call_args
    assert call_kwargs.kwargs["headers"]["Authorization"] == "Bearer my-key"
    await client.close()


@pytest.mark.asyncio
async def test_client_post_stats_failure():
    client = CommunityClient("http://fake-server.local")
    with patch.object(client._http, "post", new_callable=AsyncMock, return_value=_mock_response(401)):
        ok = await client.post_stats("bad-key", {"rank": 1})
    assert ok is False
    await client.close()


@pytest.mark.asyncio
async def test_client_post_achievement_success():
    client = CommunityClient("http://fake-server.local")
    with patch.object(client._http, "post", new_callable=AsyncMock, return_value=_mock_response(200, {"ok": True})):
        ok = await client.post_achievement("my-key", {"type": "rank_up", "rank": 5})
    assert ok is True
    await client.close()


@pytest.mark.asyncio
async def test_client_post_achievement_timeout():
    client = CommunityClient("http://fake-server.local")
    with patch.object(client._http, "post", new_callable=AsyncMock, side_effect=httpx.ReadTimeout("timeout")):
        ok = await client.post_achievement("my-key", {"type": "rank_up", "rank": 5})
    assert ok is False
    await client.close()
