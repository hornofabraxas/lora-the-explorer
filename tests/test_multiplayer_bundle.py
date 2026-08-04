import hashlib
import hmac
import json
import pytest
import time
from unittest.mock import AsyncMock, MagicMock

from lora_explorer.multiplayer.bundle import build_bundle, sign_bundle


def make_mock_db(player=None, surveys=None, posts=None):
    db = AsyncMock()
    db.get_first_player = AsyncMock(return_value=player)
    db.fetch_surveys_since = AsyncMock(return_value=surveys or [])
    db.get_all_posts = AsyncMock(return_value=posts or [])
    return db


@pytest.mark.asyncio
async def test_build_bundle_no_player():
    db = make_mock_db(player=None)
    result = await build_bundle(db, since_timestamp=0)
    assert result is None


@pytest.mark.asyncio
async def test_build_bundle_no_surveys():
    db = make_mock_db(
        player={"key": "player1", "home_lat": 33.0, "home_lon": -112.0},
        surveys=[],
    )
    result = await build_bundle(db, since_timestamp=0)
    assert result is None


@pytest.mark.asyncio
async def test_build_bundle_with_surveys():
    surveys = [
        {"hex_id": "hex_a", "is_discovery": 1, "provisions_earned": 30, "xp_earned": 10, "surveyed_at": 1000},
        {"hex_id": "hex_b", "is_discovery": 0, "provisions_earned": 20, "xp_earned": 8, "surveyed_at": 1001},
        {"hex_id": "hex_a", "is_discovery": 0, "provisions_earned": 15, "xp_earned": 5, "surveyed_at": 1002},
    ]
    posts = [
        {"hex_id": "hex_a", "level": 2, "name": "Post Alpha"},
    ]
    db = make_mock_db(
        player={"key": "player1"},
        surveys=surveys,
        posts=posts,
    )

    result = await build_bundle(db, since_timestamp=0)

    assert result is not None
    assert result["survey_count"] == 3
    assert result["discoveries"] == 1
    assert "timestamp" in result
    # Per-survey earnings and per-post survey activity rode along only for the
    # (removed) public ledger — they must no longer cross the trust boundary.
    assert "provisions_earned" not in result
    assert "xp_earned" not in result
    assert "post_surveys" not in result
    assert "coarse_cells" not in result


@pytest.mark.asyncio
async def test_build_bundle_coarse_centroid_is_snapped_for_privacy():
    # Home leaves this install only as a ~0.75° grid centroid (~50mi cells), so
    # the Worker never learns anything finer than the cell. Reported lat/lng must
    # be exact multiples of the grid step and must hide the true sub-cell offset.
    from lora_explorer.multiplayer.bundle import COARSE_CENTROID_STEP_DEG
    surveys = [
        {"hex_id": "hex_a", "is_discovery": 0, "provisions_earned": 10, "xp_earned": 5, "surveyed_at": 1000},
    ]
    db = make_mock_db(
        player={"key": "player1", "home_lat": 33.412, "home_lon": -112.087},
        surveys=surveys,
    )
    result = await build_bundle(db, since_timestamp=0)
    centroid = result["coarse_centroid"]
    step = COARSE_CENTROID_STEP_DEG
    # Snapped to the grid: value / step is a whole number.
    assert round(centroid["lat"] / step) * step == pytest.approx(centroid["lat"])
    assert round(centroid["lng"] / step) * step == pytest.approx(centroid["lng"])
    # Coarser than the exact home — never echoes the true coordinate back.
    assert centroid["lat"] != 33.412
    assert centroid["lng"] != -112.087
    # And within half a cell of the true location (a valid snap, not garbage).
    assert abs(centroid["lat"] - 33.412) <= step / 2
    assert abs(centroid["lng"] - (-112.087)) <= step / 2


@pytest.mark.asyncio
async def test_build_bundle_includes_active_title():
    surveys = [
        {"hex_id": "hex_a", "is_discovery": 0, "provisions_earned": 10, "xp_earned": 5, "surveyed_at": 1000},
    ]
    db = make_mock_db(player={"key": "player1", "active_title": "Warlord"}, surveys=surveys)
    result = await build_bundle(db, since_timestamp=0)
    assert result["active_title"] == "Warlord"


@pytest.mark.asyncio
async def test_build_bundle_active_title_defaults_empty():
    surveys = [
        {"hex_id": "hex_a", "is_discovery": 0, "provisions_earned": 10, "xp_earned": 5, "surveyed_at": 1000},
    ]
    db = make_mock_db(player={"key": "player1"}, surveys=surveys)
    result = await build_bundle(db, since_timestamp=0)
    assert result["active_title"] == ""


@pytest.mark.asyncio
async def test_build_bundle_chartered_at_from_created_at():
    # chartered_at must carry the post's real created_at so the Worker's renown
    # reflects true age; it must NOT default to `now` on every push.
    surveys = [
        {"hex_id": "hex_a", "is_discovery": 0, "provisions_earned": 10, "xp_earned": 5, "surveyed_at": 1000},
    ]
    posts = [
        {"hex_id": "hex_a", "level": 3, "name": "Post Alpha", "created_at": 1700000000,
         "mp_token": "tok_a"},
    ]
    db = make_mock_db(player={"key": "player1"}, surveys=surveys, posts=posts)

    result = await build_bundle(db, since_timestamp=0)

    assert result["post_summaries"][0]["chartered_at"] == 1700000000


@pytest.mark.asyncio
async def test_build_bundle_uses_mp_token_not_hex():
    # The real H3 hex decodes to coordinates; the bundle must carry the post's
    # opaque token as its identity instead.
    surveys = [
        {"hex_id": "hex_b", "is_discovery": 0, "provisions_earned": 10, "xp_earned": 5, "surveyed_at": 1000},
    ]
    posts = [
        {"hex_id": "hex_a", "level": 1, "name": "Post Alpha", "mp_token": "tok123abc"},
    ]
    db = make_mock_db(player={"key": "player1"}, surveys=surveys, posts=posts)

    result = await build_bundle(db, since_timestamp=0)
    assert result["post_summaries"][0]["post_token"] == "tok123abc"
    # Custom name still travels with the token.
    assert result["post_summaries"][0]["name"] == "Post Alpha"


@pytest.mark.asyncio
async def test_build_bundle_suppresses_auto_name():
    # An auto-named post (name == hex_name(hex_id)) would leak a reversible
    # hash of the real hex; the bundle sends "" and rivals render a
    # token-derived name instead.
    from lora_explorer.game.hex_names import hex_name

    surveys = [
        {"hex_id": "hex_a", "is_discovery": 0, "provisions_earned": 10, "xp_earned": 5, "surveyed_at": 1000},
    ]
    posts = [
        {"hex_id": "hex_a", "level": 1, "name": hex_name("hex_a"), "mp_token": "tokxyz"},
    ]
    db = make_mock_db(player={"key": "player1"}, surveys=surveys, posts=posts)

    result = await build_bundle(db, since_timestamp=0)
    assert result["post_summaries"][0]["name"] == ""


def test_sign_bundle():
    body = json.dumps({"survey_count": 5, "timestamp": 1000000})
    headers = sign_bundle(body, "player123", "secret456")

    assert headers["X-Player-ID"] == "player123"
    assert len(headers["X-Signature"]) == 64
    assert "X-Timestamp" in headers


def test_sign_bundle_same_inputs_same_second():
    body = json.dumps({"test": True})
    h1 = sign_bundle(body, "p1", "s1")
    h2 = sign_bundle(body, "p1", "s1")
    assert h1["X-Timestamp"] == h2["X-Timestamp"]
    assert h1["X-Signature"] == h2["X-Signature"]


def test_sign_bundle_different_secrets():
    body = json.dumps({"test": True})
    h1 = sign_bundle(body, "p1", "secret_a")
    h2 = sign_bundle(body, "p1", "secret_b")
    assert h1["X-Signature"] != h2["X-Signature"]
