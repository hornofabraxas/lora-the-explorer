import json
import pytest
import httpx
from unittest.mock import AsyncMock, patch, MagicMock

from lora_explorer.multiplayer.client import WorkerClient


@pytest.fixture
def client():
    return WorkerClient("http://localhost:8787", player_id="test123", secret="secret456")


@pytest.fixture
def unregistered_client():
    return WorkerClient("http://localhost:8787")


@pytest.mark.asyncio
async def test_register_success(unregistered_client):
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {"ok": True, "player_id": "abc123", "secret": "sec789"}
    mock_response.raise_for_status = MagicMock()

    with patch.object(unregistered_client._http, "post", new_callable=AsyncMock, return_value=mock_response):
        result = await unregistered_client.register("TestPlayer")

    assert result["ok"] is True
    assert result["player_id"] == "abc123"
    assert result["secret"] == "sec789"


@pytest.mark.asyncio
async def test_register_includes_typed_invite_code(unregistered_client):
    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True, "player_id": "a", "secret": "s"}
    mock_response.raise_for_status = MagicMock()

    with patch.object(unregistered_client._http, "post", new_callable=AsyncMock, return_value=mock_response) as mock_post:
        await unregistered_client.register("P", invite_code="holasoylora")

    assert mock_post.call_args.kwargs["json"]["invite_code"] == "holasoylora"


@pytest.mark.asyncio
async def test_register_falls_back_to_env_invite_code():
    client = WorkerClient("http://localhost:8787", invite_code="envcode")
    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True, "player_id": "a", "secret": "s"}
    mock_response.raise_for_status = MagicMock()

    with patch.object(client._http, "post", new_callable=AsyncMock, return_value=mock_response) as mock_post:
        await client.register("P")  # no typed code

    assert mock_post.call_args.kwargs["json"]["invite_code"] == "envcode"


@pytest.mark.asyncio
async def test_register_omits_invite_code_when_none(unregistered_client):
    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True, "player_id": "a", "secret": "s"}
    mock_response.raise_for_status = MagicMock()

    with patch.object(unregistered_client._http, "post", new_callable=AsyncMock, return_value=mock_response) as mock_post:
        await unregistered_client.register("P")

    assert "invite_code" not in mock_post.call_args.kwargs["json"]


@pytest.mark.asyncio
async def test_register_surfaces_worker_error_message(unregistered_client):
    resp = MagicMock()
    resp.json.return_value = {"ok": False, "error": "A valid invite code is required to register"}
    err = httpx.HTTPStatusError("403", request=MagicMock(), response=resp)
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock(side_effect=err)

    with patch.object(unregistered_client._http, "post", new_callable=AsyncMock, return_value=mock_response):
        result = await unregistered_client.register("P", invite_code="wrong")

    assert result["ok"] is False
    assert "invite code" in result["error"]


@pytest.mark.asyncio
async def test_register_failure(unregistered_client):
    with patch.object(
        unregistered_client._http, "post",
        new_callable=AsyncMock,
        side_effect=httpx.RequestError("Connection refused"),
    ):
        result = await unregistered_client.register("TestPlayer")

    assert result["ok"] is False
    assert "error" in result


@pytest.mark.asyncio
async def test_push_bundle_success(client):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "ok": True,
        "drops": [{"type": "probe", "id": "probe_1"}],
        "renown_balance": 100,
        "notifications": [],
        "ledger_entries_since": [],
    }
    mock_response.raise_for_status = MagicMock()

    with patch.object(client._http, "post", new_callable=AsyncMock, return_value=mock_response) as mock_post:
        result = await client.push_bundle({
            "survey_count": 5,
            "discoveries": 1,
            "provisions_earned": 50,
            "xp_earned": 25,
            "field_notes_earned": 5,
            "post_surveys": [],
            "coarse_cells": [],
            "timestamp": 1000000,
        })

    assert result["ok"] is True
    assert len(result["drops"]) == 1
    call_args = mock_post.call_args
    assert "X-Player-ID" in call_args.kwargs.get("headers", {})
    assert "X-Signature" in call_args.kwargs.get("headers", {})


@pytest.mark.asyncio
async def test_push_bundle_failure(client):
    with patch.object(
        client._http, "post",
        new_callable=AsyncMock,
        side_effect=httpx.RequestError("timeout"),
    ):
        result = await client.push_bundle({"survey_count": 1, "timestamp": 1000})

    assert result["ok"] is False


@pytest.mark.asyncio
async def test_get_leaderboard_success(client):
    mock_response = MagicMock()
    mock_response.json.return_value = {"players": []}
    mock_response.raise_for_status = MagicMock()

    with patch.object(client._http, "get", new_callable=AsyncMock, return_value=mock_response) as mock_get:
        result = await client.get_leaderboard()

    assert "players" in result
    # Leaderboard requires player auth now — the GET must be signed.
    assert "X-Signature" in mock_get.call_args.kwargs.get("headers", {})


@pytest.mark.asyncio
async def test_sign_headers(client):
    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True, "drops": [], "renown_balance": 0, "notifications": [], "ledger_entries_since": []}
    mock_response.raise_for_status = MagicMock()

    with patch.object(client._http, "post", new_callable=AsyncMock, return_value=mock_response) as mock_post:
        await client.push_bundle({"survey_count": 1, "timestamp": 1000})

    headers = mock_post.call_args.kwargs["headers"]
    assert headers["X-Player-ID"] == "test123"
    assert len(headers["X-Signature"]) == 64
    assert "X-Timestamp" in headers
