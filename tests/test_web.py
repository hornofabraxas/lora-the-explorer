import asyncio
import json
import time
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock

from lora_explorer.game.database import Database
from lora_explorer.game.engine import GameEngine, RANK_THRESHOLDS, BURIED_CACHE_AMOUNT
from lora_explorer.game.hex_names import hex_name
from lora_explorer.radio.adapter import RadioAdapter, IncomingMessage, MessageHandler, PositionResult
from lora_explorer.web.app import create_app
from lora_explorer.web.auth import hash_password, create_session_cookie, _get_secret, COOKIE_NAME
from lora_explorer.web.routes import _next_rank_info, _format_time_ago
from lora_explorer.game.engine import rank_name


FAR_POSITION = (40.07, -104.93)


class MockRadioAdapter(RadioAdapter):
    def __init__(self):
        self.handler: MessageHandler | None = None
        self.sent_messages = []
        self.connected = False
        self.mock_position = FAR_POSITION
        self.engine = None

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def send_message(self, recipient_key, text):
        self.sent_messages.append((recipient_key, text))
        return True

    async def set_message_handler(self, handler):
        self.handler = handler

    async def request_position(self, node_key, progress_callback=None):
        if self.mock_position is None:
            from lora_explorer.radio.adapter import PositionFailure
            return PositionResult(failure=PositionFailure.TIMEOUT)
        return PositionResult(position=self.mock_position)

    async def simulate_message(self, text, sender="abc123", snr=-8.0, rssi=-105, hops=2):
        msg = IncomingMessage(sender_key=sender, text=text, snr=snr, rssi=rssi, hops=hops)
        if not self.handler:
            return None
        if self.engine:
            self.engine._recent_messages.clear()
        before = len(self.sent_messages)
        response = await self.handler(msg)
        if response is None and self.engine and self.engine._command_task \
                and not self.engine._command_task.done():
            await self.engine._command_task
            if len(self.sent_messages) > before:
                return self.sent_messages[-1][1]
            return None
        return response

    async def get_companion_status(self):
        return {"connected": self.connected, "connection": "TCP mock:4000"}

    def get_contacts(self):
        return {"abc123": {"adv_name": "TESTNODE"}}

    async def get_repeaters(self):
        return []

    async def reboot_companion(self):
        return True

    async def await_survey(self):
        if self.engine and self.engine._command_task:
            await self.engine._command_task


@pytest.fixture
def adapter():
    return MockRadioAdapter()


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(db_path=str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def engine(adapter, db):
    e = GameEngine(adapter=adapter, home_lat=40.0, home_lon=-105.0, db=db)
    adapter.engine = e
    await e.start()
    yield e
    await e.stop()


@pytest.fixture
def config(tmp_path):
    return {
        "connection_type": "wifi",
        "companion_host": "192.168.1.100",
        "companion_port": 4000,
        "home_lat": 40.0,
        "home_lon": -105.0,
        "db_path": str(tmp_path / "test.db"),
    }


@pytest_asyncio.fixture
async def app(engine, db, config, adapter):
    await db.set_setting("password_hash", hash_password("testpass123"))
    a = create_app(engine, db, config, radio=adapter)
    secret = _get_secret(config["db_path"])
    a.state._test_session_cookie = create_session_cookie(secret)
    return a


# --- Helper function tests ---

def test_rank_name_known():
    assert rank_name(1) == "Novice"
    assert rank_name(50) == "Grandmaster"


def test_rank_name_bands():
    assert rank_name(5) == "Scout"
    assert rank_name(10) == "Surveyor"
    assert rank_name(20) == "Pathfinder"


def test_next_rank_info_from_novice():
    info = _next_rank_info(1)
    assert info["level"] == 2
    assert info["name"] == "Novice"


def test_next_rank_info_at_max():
    assert _next_rank_info(50) is None


def test_format_time_ago():
    assert _format_time_ago(int(time.time())) == "just now"
    assert "m ago" in _format_time_ago(int(time.time()) - 300)
    assert "h ago" in _format_time_ago(int(time.time()) - 7200)
    assert "d ago" in _format_time_ago(int(time.time()) - 172800)


# --- App factory tests ---

def test_create_app_returns_fastapi(app):
    from fastapi import FastAPI
    assert isinstance(app, FastAPI)


def test_app_has_state(app, engine, db, config):
    assert app.state.engine is engine
    assert app.state.db is db
    assert app.state.config is config


def test_app_has_dashboard_route(app):
    openapi = app.openapi()
    assert "/" in openapi["paths"]


# --- Route integration tests (via ASGI) ---

async def _asgi_request(app, method: str, path: str, data: dict | None = None,
                         json_body: dict | None = None) -> tuple[int, str, dict]:
    """Minimal ASGI request without httpx. `json_body` sends a JSON body with
    the matching content-type instead of the default form encoding used by
    `data` — the two are mutually exclusive."""
    from urllib.parse import urlencode
    if json_body is not None:
        req_body = json.dumps(json_body).encode()
    else:
        req_body = urlencode(data).encode() if data else b""
    cookie = getattr(app.state, "_test_session_cookie", None)
    content_type = b"application/json" if json_body is not None else b"application/x-www-form-urlencoded"
    headers = [(b"content-type", content_type)]
    if cookie:
        headers.append((b"cookie", f"{COOKIE_NAME}={cookie}".encode()))
    query_string = b""
    if "?" in path:
        path, qs = path.split("?", 1)
        query_string = qs.encode()
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query_string,
        "headers": headers,
        "root_path": "",
        "client": ("127.0.0.1", 0),
    }
    status_code = None
    body_parts = []
    headers = {}

    async def receive():
        return {"type": "http.request", "body": req_body}

    async def send(message):
        nonlocal status_code
        if message["type"] == "http.response.start":
            status_code = message["status"]
            headers.update({
                k.decode(): v.decode()
                for k, v in message.get("headers", [])
            })
        elif message["type"] == "http.response.body":
            body_parts.append(message.get("body", b""))

    await app(scope, receive, send)
    body = b"".join(body_parts).decode("utf-8")
    return status_code, body, headers


async def _get(app, path: str) -> tuple[int, str]:
    status, body, _ = await _asgi_request(app, "GET", path)
    return status, body


async def _post(app, path: str, data: dict | None = None) -> tuple[int, str, dict]:
    return await _asgi_request(app, "POST", path, data=data)


async def _post_json(app, path: str, json_body: dict | None = None) -> tuple[int, str, dict]:
    return await _asgi_request(app, "POST", path, json_body=json_body or {})


# --- Dashboard ---

@pytest.mark.asyncio
async def test_dashboard_no_player(app):
    status, body = await _get(app, "/")
    assert status == 200
    assert "Link Your Spyglass" in body


@pytest.mark.asyncio
async def test_dashboard_setup_when_no_home(tmp_path):
    """With home at 0,0 and no player, setup map should appear."""
    from lora_explorer.web.app import create_app
    zero_config = {
        "connection_type": "wifi", "companion_host": "", "companion_port": 4000,
        "home_lat": 0, "home_lon": 0, "db_path": str(tmp_path / "setup.db"),
    }
    db = Database(db_path=zero_config["db_path"])
    adapter = MockRadioAdapter()
    engine = GameEngine(adapter=adapter, home_lat=0, home_lon=0, db=db)
    a = create_app(engine, db, zero_config, radio=adapter)
    await db.connect()
    await db.set_setting("password_hash", hash_password("testpass123"))
    secret = _get_secret(zero_config["db_path"])
    a.state._test_session_cookie = create_session_cookie(secret)
    try:
        status, body = await _get(a, "/")
        assert status == 200
        assert "Establish Your Base Camp" in body
        assert "setup-map" in body
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_setup_home_flow(tmp_path):
    """POST /setup/home creates pending player and awards postcard."""
    from lora_explorer.web.app import create_app
    zero_config = {
        "connection_type": "wifi", "companion_host": "", "companion_port": 4000,
        "home_lat": 0, "home_lon": 0, "db_path": str(tmp_path / "setup.db"),
    }
    db = Database(db_path=zero_config["db_path"])
    adapter = MockRadioAdapter()
    engine = GameEngine(adapter=adapter, home_lat=0, home_lon=0, db=db)
    a = create_app(engine, db, zero_config, radio=adapter)
    await db.connect()
    await db.set_setting("password_hash", hash_password("testpass123"))
    secret = _get_secret(zero_config["db_path"])
    a.state._test_session_cookie = create_session_cookie(secret)
    try:
        status, _, headers = await _post(a, "/setup/home", data={"lat": "33.45", "lon": "-112.07"})
        assert status == 303
        player = await db.get_first_player()
        assert player is not None
        assert abs(player["home_lat"] - 33.45) < 0.01
        postcards = await db.get_all_postcards(player["key"])
        assert any(p["class"] == "Field Training" and p["description"] == "Staking Claim" for p in postcards)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_staking_claim_survives_first_survey(tmp_path):
    """The setup wizard awards "Staking Claim" against the placeholder
    "pending" player key, before any radio key is known. The first survey
    re-keys that player row to the real radio key — the postcard must move
    with it, or the badge reverts to unearned right after setup completes."""
    from lora_explorer.web.app import create_app
    zero_config = {
        "connection_type": "wifi", "companion_host": "", "companion_port": 4000,
        "home_lat": 0, "home_lon": 0, "db_path": str(tmp_path / "setup2.db"),
    }
    db = Database(db_path=zero_config["db_path"])
    adapter = MockRadioAdapter()
    engine = GameEngine(adapter=adapter, home_lat=0, home_lon=0, db=db)
    adapter.engine = engine
    a = create_app(engine, db, zero_config, radio=adapter)
    await db.connect()
    await db.set_setting("password_hash", hash_password("testpass123"))
    secret = _get_secret(zero_config["db_path"])
    a.state._test_session_cookie = create_session_cookie(secret)
    try:
        await engine.start()
        status, _, _ = await _post(a, "/setup/home", data={"lat": "33.45", "lon": "-112.07"})
        assert status == 303

        adapter.mock_position = (33.45, -112.07)
        await adapter.simulate_message("/lora survey", sender="realkey123")

        player = await db.get_first_player()
        assert player["key"] == "realkey123"
        postcards = await db.get_all_postcards(player["key"])
        assert any(
            p["class"] == "Field Training" and p["description"] == "Staking Claim"
            for p in postcards
        )
    finally:
        await engine.stop()
        await db.close()


@pytest.mark.asyncio
async def test_dashboard_with_player(app, engine, adapter):
    await adapter.simulate_message("/lora survey")
    status, body = await _get(app, "/")
    assert status == 200
    assert "Novice" in body
    assert "📦" in body
    assert "🪙" in body
    assert "Field Training" in body


@pytest.mark.asyncio
async def test_dashboard_shows_xp_progress(app, engine, adapter):
    await adapter.simulate_message("/lora survey")
    status, body = await _get(app, "/")
    assert status == 200
    assert "XP" in body
    assert "Rank" in body


@pytest.mark.asyncio
async def test_radio_shows_map(app, engine, adapter):
    await adapter.simulate_message("/lora survey")
    status, body = await _get(app, "/radio")
    assert status == 200
    assert "leaflet" in body.lower()
    assert "40.0" in body
    assert "-105.0" in body


@pytest.mark.asyncio
async def test_service_worker_served_at_root(app):
    status, body, headers = await _asgi_request(app, "GET", "/sw.js")
    assert status == 200
    assert "javascript" in headers.get("content-type", "")
    # Root scope so the worker controls the whole app.
    assert headers.get("service-worker-allowed") == "/"
    # Must never intercept the live game data path.
    assert "/api/" in body


@pytest.mark.asyncio
async def test_service_worker_is_public():
    # Reachable without a session so the browser can register it pre-login.
    from lora_explorer.web.auth import _PUBLIC_PREFIXES
    assert "/sw.js" in _PUBLIC_PREFIXES


@pytest.mark.asyncio
async def test_dashboard_shows_field_training_badges(app, engine, adapter):
    await adapter.simulate_message("/lora survey")
    status, body = await _get(app, "/")
    assert status == 200
    assert "Field Training" in body
    assert "Staking Claim" in body


@pytest.mark.asyncio
async def test_dashboard_shows_society_commission(app, engine, adapter):
    await adapter.simulate_message("/lora survey")
    status, body = await _get(app, "/")
    assert status == 200
    assert "Society Commission" in body
    # All checkpoint steps are surfaced in one card. PvP Combat isn't a
    # separate step — it's folded into Charter License's unlock text, since
    # PvP eligibility and having chartered a first post are the same moment.
    for step in ["Field Training", "Scout Rank", "Charter License",
                 "Frontier Merchant"]:
        assert step in body


@pytest.mark.asyncio
async def test_dashboard_commission_marks_unlocked_steps(app, engine, adapter, db):
    await adapter.simulate_message("/lora survey")
    key = (await db.get_first_player())["key"]
    from lora_explorer.game.engine import CONTRACTS_MIN_LEVEL
    await db._execute(
        "UPDATE players SET rank_level = ? WHERE key = ?", (CONTRACTS_MIN_LEVEL, key)
    )
    status, body = await _get(app, "/")
    assert status == 200
    # Scout Rank gate is met → shown as unlocked.
    assert "Unlocked: Expedition Contracts" in body


@pytest.mark.asyncio
async def test_outposts_shows_camp_upgrade(app, engine, adapter):
    await adapter.simulate_message("/lora survey")
    status, body = await _get(app, "/outposts")
    assert status == 200
    assert "Upgrade" in body


@pytest.mark.asyncio
async def test_dashboard_stats_correct(app, engine, adapter, db):
    from lora_explorer.game.engine import RANK_THRESHOLDS
    await adapter.simulate_message("/lora survey")
    player = await db.get_first_player()
    status, body = await _get(app, "/")
    assert str(player["provisions"]) in body
    # XP renders as progress within the current rank, not the raw total.
    prev_xp = RANK_THRESHOLDS[player["rank_level"]]["xp"]
    xp_in_rank = int(player["xp"] - prev_xp)
    xp_needed = RANK_THRESHOLDS[player["rank_level"] + 1]["xp"] - prev_xp
    assert f"{xp_in_rank} / {xp_needed} XP" in body


# --- Base Camp upgrade (POST, redirects to dashboard) ---

async def _give_resources(db, key, provisions=0):
    if provisions:
        await db.add_provisions(key, provisions)


@pytest.mark.asyncio
async def test_basecamp_upgrade_insufficient(app, engine, adapter):
    await adapter.simulate_message("/lora survey")
    status, body, headers = await _post(app, "/basecamp/upgrade")
    assert status == 303
    assert "flash_msg" in headers.get("location", "")


@pytest.mark.asyncio
async def test_basecamp_upgrade_success(app, engine, adapter, db):
    await adapter.simulate_message("/lora survey")
    key = (await db.get_first_player())["key"]
    await _give_resources(db, key, provisions=500)
    status, body, headers = await _post(app, "/basecamp/upgrade")
    assert status == 303
    assert "success" in headers.get("location", "")
    player = await db.get_player(key)
    assert player["base_camp_level"] == 2


# --- Survey Posts ---

@pytest.mark.asyncio
async def test_outposts_page_no_player(app):
    status, body = await _get(app, "/outposts")
    assert status == 200
    assert "No Explorer Found" in body


@pytest.mark.asyncio
async def test_outposts_page_empty(app, engine, adapter):
    await adapter.simulate_message("/lora survey")
    status, body = await _get(app, "/outposts")
    assert status == 200
    # A fresh rank-1 player has no Charter License yet, so the empty state
    # points at Society Commission instead of the /lora charter instructions.
    assert "unavailable until you earn your Charter License" in body


@pytest.mark.asyncio
async def test_posts_redirect_to_outposts(app):
    status, body, headers = await _asgi_request(app, "GET", "/posts")
    assert status == 301
    assert "/outposts" in headers.get("location", "")


@pytest.mark.asyncio
async def test_collect_provisions_no_posts(app, engine, adapter):
    await adapter.simulate_message("/lora survey")
    status, body, headers = await _post(app, "/posts/collect-provisions")
    assert status == 303
    assert "flash_msg" in headers.get("location", "")


# --- Stats ---

@pytest.mark.asyncio
async def test_stats_page_no_player(app):
    status, body = await _get(app, "/stats")
    assert status == 200
    assert "No Explorer Found" in body


@pytest.mark.asyncio
async def test_stats_page_with_player(app, engine, adapter):
    await adapter.simulate_message("/lora survey")
    status, body = await _get(app, "/stats")
    assert status == 200
    assert "Ledger" in body
    assert "Expedition Log" in body
    assert "survey" in body.lower()
    assert "square miles" in body


@pytest.mark.asyncio
async def test_stats_page_shows_recent_surveys(app, engine, adapter):
    await adapter.simulate_message("/lora survey")
    status, body = await _get(app, "/stats")
    assert status == 200
    assert "Recent Surveys" in body
    assert "mi" in body


@pytest.mark.asyncio
async def test_stats_page_shows_postcards(app, engine, adapter):
    await adapter.simulate_message("/lora survey")
    status, body = await _get(app, "/stats")
    assert status == 200
    assert "Postcards" in body
    assert "25" in body
    assert "Strider" in body
    assert "Trailblazer" in body


@pytest.mark.asyncio
async def test_stats_page_shows_earned_postcard(app, engine, adapter, db):
    await adapter.simulate_message("/lora survey")
    key = (await db.get_first_player())["key"]
    await db.award_postcard(key, "Strider", 2, "Survey at 10+ miles", 10.0, -10.0)
    status, body = await _get(app, "/stats")
    assert status == 200
    assert "has-progress" in body
    # The card face shows the best ACHIEVED metric...
    assert "Survey at 10+ miles" in body
    # ...and the next tier lives in the ⓘ tooltip ladder (data-tip).
    assert "Survey at 15+ miles" in body
    assert "pc-achieved" in body
    assert "data-tip" in body


# --- Database method tests ---

@pytest.mark.asyncio
async def test_get_first_player_empty(db):
    player = await db.get_first_player()
    assert player is None


@pytest.mark.asyncio
async def test_get_first_player(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    player = await db.get_first_player()
    assert player is not None
    assert player["key"] == "key1"


@pytest.mark.asyncio
async def test_get_recent_surveys_empty(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    surveys = await db.get_recent_surveys("key1")
    assert surveys == []


@pytest.mark.asyncio
async def test_get_recent_surveys(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    await db.record_survey(
        "key1", "hex1", 40.07, -104.93, 5.0,
        -8.0, -105, 2, 100, 20, 2, True,
    )
    surveys = await db.get_recent_surveys("key1")
    assert len(surveys) == 1
    assert surveys[0]["hex_id"] == "hex1"
    assert surveys[0]["xp_earned"] == 100


@pytest.mark.asyncio
async def test_get_total_distance(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    await db.record_survey(
        "key1", "hex1", 40.07, -104.93, 5.0,
        -8.0, -105, 2, 100, 20, 2, True,
    )
    await db.record_survey(
        "key1", "hex2", 40.14, -104.86, 10.0,
        -8.0, -105, 2, 100, 20, 2, True,
    )
    total = await db.get_total_distance("key1")
    assert total == 15.0


@pytest.mark.asyncio
async def test_get_total_distance_empty(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    total = await db.get_total_distance("key1")
    assert total == 0.0


# --- Settings ---

@pytest.mark.asyncio
async def test_settings_page(app, engine, adapter):
    await adapter.simulate_message("/lora survey")
    status, body = await _get(app, "/settings")
    assert status == 200
    assert "Settings" in body
    assert "WIFI" in body


@pytest.mark.asyncio
async def test_settings_page_no_player(app):
    status, body = await _get(app, "/settings")
    assert status == 200
    assert "Settings" in body
    assert "Backups" in body


# --- Backups ---

@pytest.mark.asyncio
async def test_settings_shows_backup_section(app, engine, adapter):
    await adapter.simulate_message("/lora survey")
    status, body = await _get(app, "/settings")
    assert status == 200
    assert "Backups" in body
    assert "Create Backup Now" in body


@pytest.mark.asyncio
async def test_create_backup(app, engine, adapter):
    await adapter.simulate_message("/lora survey")
    status, body, headers = await _post(app, "/settings/backup")
    assert status == 303
    assert "success" in headers.get("location", "")


@pytest.mark.asyncio
async def test_backup_restore_cycle(app, engine, adapter, db):
    await adapter.simulate_message("/lora survey")
    key = (await db.get_first_player())["key"]
    original_xp = (await db.get_player(key))["xp"]

    status, _, headers = await _post(app, "/settings/backup")
    assert status == 303

    await db.add_xp(key, 9999)
    assert (await db.get_player(key))["xp"] == original_xp + 9999

    from lora_explorer.game.backup import list_backups
    config = app.state.config
    backups = list_backups(config["db_path"])
    assert len(backups) >= 1
    filename = backups[0]["filename"]

    status, _, headers = await _post(app, f"/settings/restore/{filename}")
    assert status == 303
    assert "success" in headers.get("location", "")

    player = await db.get_player(key)
    assert player["xp"] == original_xp


def test_backup_list_and_prune(tmp_path):
    from lora_explorer.game.backup import create_backup, list_backups, backup_dir
    import os

    import sqlite3
    db_file = tmp_path / "explorer.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES ('test data')")
    conn.commit()
    conn.close()

    bdir = backup_dir(str(db_file))
    bdir.mkdir(parents=True, exist_ok=True)
    for i in range(5):
        dest = bdir / f"explorer-2026010{i}-120000.db"
        dest.write_text("old backup")
        ts = 1700000000 + i * 86400
        os.utime(str(dest), (ts, ts))

    create_backup(str(db_file))

    backups = list_backups(str(db_file))
    assert len(backups) == 3
    assert backups[0]["filename"].startswith("explorer-2026")


# --- Database: postcards and hexes ---

@pytest.mark.asyncio
async def test_get_all_postcards(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    await db.award_postcard("key1", "Strider", 3, "Survey at 15+ miles", 15.0, -12.0)
    await db.award_postcard("key1", "Trailblazer", 1, "5 territories discovered", 5.0, -10.0)
    postcards = await db.get_all_postcards("key1")
    assert len(postcards) == 2
    assert postcards[0]["class"] in ("Strider", "Trailblazer")


@pytest.mark.asyncio
async def test_get_all_hexes(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    await db.discover_hex("key1", "882a100d63fffff")
    hexes = await db.get_all_hexes("key1")
    assert len(hexes) == 1
    assert hexes[0]["hex_id"] == "882a100d63fffff"
    assert hexes[0]["survey_count"] == 0


# --- API endpoints ---

@pytest.mark.asyncio
async def test_api_hexes_empty(app):
    status, body = await _get(app, "/api/hexes")
    assert status == 200
    import json
    data = json.loads(body)
    assert data == {"hexes": [], "repeaters": []}


@pytest.mark.asyncio
async def test_api_hexes_with_data(app, engine, adapter):
    await adapter.simulate_message("/lora survey")
    status, body = await _get(app, "/api/hexes")
    assert status == 200
    import json
    data = json.loads(body)
    hexes = data["hexes"]
    assert len(hexes) == 1
    assert "hex_id" in hexes[0]
    assert "boundary" in hexes[0]
    assert "type" in hexes[0]
    assert hexes[0]["survey_count"] == 1
    assert len(hexes[0]["boundary"]) >= 6
    assert "repeaters" in data


@pytest.mark.asyncio
async def test_api_posts_empty(app, engine, adapter):
    await adapter.simulate_message("/lora survey")
    status, body = await _get(app, "/api/posts")
    assert status == 200
    assert body == "[]"


# --- Polish + Integration ---

@pytest.mark.asyncio
async def test_404_page(app):
    status, body = await _get(app, "/nonexistent")
    assert status == 404
    assert "Not Found" in body
    assert "Return to Briefing" in body


@pytest.mark.asyncio
async def test_all_pages_load_without_player(app):
    for path in ["/", "/outposts", "/stats", "/settings", "/help"]:
        status, body = await _get(app, path)
        assert status == 200, f"{path} returned {status}"


@pytest.mark.asyncio
async def test_help_page(app):
    status, body = await _get(app, "/help")
    assert status == 200
    assert "/lora survey" in body
    assert "/lora charter" in body
    assert "/lora upkeep" in body
    assert "Provisions" in body
    assert "Survey Marks" in body


@pytest.mark.asyncio
async def test_help_page_has_bug_report_section(app):
    status, body = await _get(app, "/help")
    assert status == 200
    assert "Report a Problem" in body
    # Pre-filled GitHub issue target + the client-side diagnostics payload.
    assert "issues/new" in body
    assert '"install"' in body and '"version"' in body
    # Guardrail: the diagnostics snapshot must not leak sensitive fields.
    for leaked in ('"home_lat"', '"home_lon"', '"public_key"', '"pubkey"'):
        assert leaked not in body


def test_install_method_env_override(monkeypatch):
    from lora_explorer.paths import install_method
    monkeypatch.setenv("LORA_INSTALL_METHOD", "docker")
    assert install_method() == "docker"


def test_install_method_returns_known_label(monkeypatch):
    from lora_explorer.paths import install_method
    monkeypatch.delenv("LORA_INSTALL_METHOD", raising=False)
    assert install_method() in ("source/pip", "docker", "windows-installer")


@pytest.mark.asyncio
async def test_integration_lifecycle(app, engine, adapter, db):
    """Full lifecycle: survey → view dashboard → upgrade camp → verify."""
    status, body = await _get(app, "/")
    assert "Link Your Spyglass" in body

    await adapter.simulate_message("/lora survey")

    status, body = await _get(app, "/")
    assert "Novice" in body
    assert "📦" in body

    key = (await db.get_first_player())["key"]
    await _give_resources(db, key, provisions=500)
    status, _, headers = await _post(app, "/basecamp/upgrade")
    assert status == 303
    assert "success" in headers.get("location", "")

    player = await db.get_player(key)
    assert player["base_camp_level"] == 2

    status, body = await _get(app, "/outposts")
    assert "Shelter" in body
    assert "XP Multiplier" in body

    for path in ["/", "/outposts", "/stats", "/settings", "/help"]:
        status, body = await _get(app, path)
        assert status == 200, f"{path} returned {status} after upgrade"


# --- Hex Names ---

def test_hex_name_deterministic():
    name1 = hex_name("882a100d63fffff")
    name2 = hex_name("882a100d63fffff")
    assert name1 == name2
    assert len(name1.split()) == 3


def test_hex_name_different_hexes():
    name1 = hex_name("882a100d63fffff")
    name2 = hex_name("882a100d65fffff")
    assert name1 != name2


# --- Relics ---

@pytest.mark.asyncio
async def test_relic_add_and_list(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    relic = await db.add_relic("key1", "buried_cache", "hex123")
    assert relic["type"] == "buried_cache"
    relics = await db.get_unused_relics("key1")
    assert len(relics) == 1
    assert relics[0]["type"] == "buried_cache"


@pytest.mark.asyncio
async def test_use_buried_cache(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    relic = await db.add_relic("key1", "buried_cache", "hex123")
    relics = await db.get_unused_relics("key1")
    relic_id = relics[0]["id"]
    await db.use_buried_cache(relic_id, "key1", BURIED_CACHE_AMOUNT)
    player = await db.get_player("key1")
    assert player["provisions"] == BURIED_CACHE_AMOUNT
    relics = await db.get_unused_relics("key1")
    assert len(relics) == 0


@pytest.mark.asyncio
async def test_use_wardstone(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    await db.discover_hex("key1", "hex123")
    await db.create_post("key1", "hex123", "TestPost")
    post = await db.get_post_in_hex("key1", "hex123")
    relic = await db.add_relic("key1", "wardstone", "hex456")
    relics = await db.get_unused_relics("key1")
    relic_id = relics[0]["id"]
    await db.ward_post(relic_id, post["id"], "key1", 30 * 86400)
    updated_post = await db.get_post_by_id(post["id"])
    assert updated_post["ruin_frozen_until"] is not None
    assert updated_post["ruin_frozen_until"] > time.time()
    relics = await db.get_unused_relics("key1")
    assert len(relics) == 0


@pytest.mark.asyncio
async def test_warded_post_earns_no_income(engine, db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    hex_id = engine._get_hex_id(40.1, -105.0)
    await db.discover_hex("key1", hex_id)
    post = await db.create_post("key1", hex_id, "TestPost")
    now = int(time.time())
    # A ward covering the whole earning window: the post is dormant, so income
    # is excluded and ruin is frozen (age doesn't advance) — it earns nothing.
    await db._execute(
        "UPDATE survey_posts SET warded_at = ?, ruin_frozen_until = ?, "
        "last_tended_at = ?, last_collected_at = ? WHERE id = ?",
        (now - 8 * 86400, now + 86400, now - 8 * 86400, now - 8 * 86400, post["id"]),
    )
    result = await engine.collect_passive_provisions("key1")
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_relic_count(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    await db.add_relic("key1", "buried_cache", "hex1")
    await db.add_relic("key1", "wardstone", "hex2")
    count = await db.count_relics("key1")
    assert count == 2


@pytest.mark.asyncio
async def test_count_recent_relics_by_type_groups_and_ignores_used(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    await db.add_relic("key1", "buried_cache", "hex1")
    await db.add_relic("key1", "buried_cache", "hex2")
    await db.add_relic("key1", "wardstone", "hex3")
    # An opened Buried Cache must not keep suppressing future drops.
    used = (await db.get_unused_relics("key1"))
    cache_id = next(r["id"] for r in used if r["type"] == "buried_cache")
    await db.use_buried_cache(cache_id, "key1", BURIED_CACHE_AMOUNT)

    counts = await db.count_recent_relics_by_type("key1", 0)
    assert counts == {"buried_cache": 1, "wardstone": 1}


@pytest.mark.asyncio
async def test_count_recent_relics_by_type_respects_window(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    await db.add_relic("key1", "buried_cache", "hex1")
    # Age the relic out of the rolling window.
    await db._execute(
        "UPDATE relics SET found_at = ? WHERE player_key = 'key1'",
        (int(time.time()) - 10 * 86400,),
    )
    since = int(time.time()) - 7 * 86400
    assert await db.count_recent_relics_by_type("key1", since) == {}


# --- Supply Drops log ---

@pytest.mark.asyncio
async def test_supply_run_log_roundtrip(db):
    now = int(time.time())
    await db.log_supply_run(now - 20, 5, ["attack_common", "attack_common", "probe"])
    await db.log_supply_run(now, 8, [])  # a dry run still gets recorded

    runs = await db.get_recent_supply_runs(5)
    assert len(runs) == 2
    # Newest first.
    assert runs[0]["survey_count"] == 8
    assert runs[0]["drop_count"] == 0
    assert runs[0]["drops"] == []
    assert runs[1]["drop_count"] == 3
    assert runs[1]["drops"].count("attack_common") == 2


@pytest.mark.asyncio
async def test_supply_run_log_prunes_to_50(db):
    for i in range(55):
        await db.log_supply_run(i, 1, [])
    runs = await db.get_recent_supply_runs(100)
    assert len(runs) == 50
    # Oldest (smallest ran_at) were pruned; the newest survive.
    assert runs[0]["ran_at"] == 54
    assert min(r["ran_at"] for r in runs) == 5


@pytest.mark.asyncio
async def test_count_surveys_since(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    await db.record_survey("key1", "hexA", 40.0, -105.0, 5.0, None, None, None,
                           10, 5, 0, False)
    await db.record_survey("key1", "hexB", 40.0, -105.0, 5.0, None, None, None,
                           10, 5, 0, False)
    assert await db.count_surveys_since("key1", 0) == 2
    assert await db.count_surveys_since("key1", int(time.time()) + 10) == 0


@pytest.mark.asyncio
async def test_dashboard_shows_relics(app, engine, adapter, db):
    await adapter.simulate_message("/lora survey")
    key = (await db.get_first_player())["key"]
    await db.add_relic(key, "buried_cache", "hex123")
    await db.add_relic(key, "wardstone", "hex456")
    status, body = await _get(app, "/")
    assert status == 200
    assert "Buried Cache" in body
    assert "Wardstone" in body
    assert "Relic Inventory" in body


@pytest.mark.asyncio
async def test_use_cache_route(app, engine, adapter, db):
    await adapter.simulate_message("/lora survey")
    key = (await db.get_first_player())["key"]
    await db.add_relic(key, "buried_cache", "hex123")
    relics = await db.get_unused_relics(key)
    before_count = len(relics)
    cache_relic = next(r for r in relics if r["type"] == "buried_cache")
    status, _, headers = await _post(app, f"/relics/{cache_relic['id']}/use-cache")
    assert status == 303
    relics = await db.get_unused_relics(key)
    assert len(relics) == before_count - 1


@pytest.mark.asyncio
async def test_hex_names_in_map_api(app, engine, adapter):
    import json
    await adapter.simulate_message("/lora survey")
    status, body, _ = await _asgi_request(app, "GET", "/api/hexes")
    assert status == 200
    data = json.loads(body)
    hexes = data["hexes"]
    assert len(hexes) > 0
    assert "name" in hexes[0]
    assert len(hexes[0]["name"].split()) == 3
    assert "on_cooldown" in hexes[0]


# --- Momentum ---

@pytest.mark.asyncio
async def test_dashboard_shows_momentum(app, adapter, engine, db):
    await adapter.simulate_message("/lora survey")
    await db._execute("UPDATE players SET momentum_tier = 3 WHERE key = 'abc123'")
    status, body, _ = await _asgi_request(app, "GET", "/")
    assert status == 200
    assert "Momentum:" in body
    assert "15% XP boost" in body


@pytest.mark.asyncio
async def test_dashboard_hides_momentum_at_zero(app, adapter, engine, db):
    await adapter.simulate_message("/lora survey")
    await db._execute("UPDATE players SET momentum_tier = 0 WHERE key = 'abc123'")
    status, body, _ = await _asgi_request(app, "GET", "/")
    assert status == 200
    assert "Momentum:" not in body


# --- Companion Diagnostics ---

@pytest.mark.asyncio
async def test_settings_shows_companion_status(app, adapter):
    status, body, _ = await _asgi_request(app, "GET", "/settings")
    assert status == 200
    assert "Companion" in body
    assert "Connected" in body
    assert "TCP mock:4000" in body


@pytest.mark.asyncio
async def test_send_test_message(app, adapter, engine, db):
    await adapter.simulate_message("/lora survey")
    status, body, _ = await _post(app, "/api/companion/test-message")
    assert status == 200
    assert json.loads(body)["ok"] is True
    assert any("COMPANION TEST" in msg for _, msg in adapter.sent_messages)


@pytest.mark.asyncio
async def test_send_test_message_no_player(app):
    status, body, _ = await _post(app, "/api/companion/test-message")
    assert status == 200
    assert json.loads(body)["ok"] is False


@pytest.mark.asyncio
async def test_reboot_companion(app):
    status, _, headers = await _post(app, "/settings/reboot-companion")
    assert status == 303
    assert "success" in headers.get("location", "")


# --- Authentication ---

@pytest.mark.asyncio
async def test_unauthenticated_redirects_to_login(app):
    app.state._test_session_cookie = None
    status, body, headers = await _asgi_request(app, "GET", "/")
    assert status == 302
    assert "/login" in headers.get("location", "")


@pytest.mark.asyncio
async def test_login_page_loads(app):
    app.state._test_session_cookie = None
    status, body = await _get(app, "/login")
    assert status == 200
    assert "Log In" in body
    assert "Locked out?" in body


@pytest.mark.asyncio
async def test_login_wrong_password(app):
    app.state._test_session_cookie = None
    status, body, headers = await _asgi_request(app, "POST", "/login", data={"password": "wrongpass"})
    assert status == 401
    assert "Incorrect password" in body


@pytest.mark.asyncio
async def test_login_correct_password(app):
    app.state._test_session_cookie = None
    status, body, headers = await _asgi_request(app, "POST", "/login", data={"password": "testpass123"})
    assert status == 303
    assert "set-cookie" in headers


@pytest.mark.asyncio
async def test_no_password_redirects_to_setup(tmp_path):
    from lora_explorer.web.app import create_app
    cfg = {
        "connection_type": "wifi", "companion_host": "", "companion_port": 4000,
        "home_lat": 0, "home_lon": 0, "db_path": str(tmp_path / "nopass.db"),
    }
    db = Database(db_path=cfg["db_path"])
    adapter = MockRadioAdapter()
    engine = GameEngine(adapter=adapter, home_lat=0, home_lon=0, db=db)
    a = create_app(engine, db, cfg, radio=adapter)
    await db.connect()
    try:
        status, body, headers = await _asgi_request(a, "GET", "/")
        assert status == 302
        assert "/setup" in headers.get("location", "")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_setup_password_flow(tmp_path):
    from lora_explorer.web.app import create_app
    cfg = {
        "connection_type": "wifi", "companion_host": "", "companion_port": 4000,
        "home_lat": 0, "home_lon": 0, "db_path": str(tmp_path / "pwsetup.db"),
    }
    db = Database(db_path=cfg["db_path"])
    adapter = MockRadioAdapter()
    engine = GameEngine(adapter=adapter, home_lat=0, home_lon=0, db=db)
    a = create_app(engine, db, cfg, radio=adapter)
    await db.connect()
    try:
        status, body, headers = await _asgi_request(
            a, "POST", "/setup/password",
            data={"password": "mypassword", "confirm": "mypassword"},
        )
        assert status == 303
        assert "set-cookie" in headers
        pw_hash = await db.get_setting("password_hash")
        assert pw_hash is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_setup_password_too_short(tmp_path):
    from lora_explorer.web.app import create_app
    cfg = {
        "connection_type": "wifi", "companion_host": "", "companion_port": 4000,
        "home_lat": 0, "home_lon": 0, "db_path": str(tmp_path / "pwshort.db"),
    }
    db = Database(db_path=cfg["db_path"])
    adapter = MockRadioAdapter()
    engine = GameEngine(adapter=adapter, home_lat=0, home_lon=0, db=db)
    a = create_app(engine, db, cfg, radio=adapter)
    await db.connect()
    try:
        status, body, headers = await _asgi_request(
            a, "POST", "/setup/password",
            data={"password": "short", "confirm": "short"},
        )
        assert status == 200
        assert "at least 8 characters" in body
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_change_password(app):
    status, body, headers = await _asgi_request(
        app, "POST", "/settings/change-password",
        data={"current_password": "testpass123", "new_password": "newpass1234", "confirm_password": "newpass1234"},
    )
    assert status == 303
    assert "success" in headers.get("location", "")


@pytest.mark.asyncio
async def test_change_password_wrong_current(app):
    status, body, headers = await _asgi_request(
        app, "POST", "/settings/change-password",
        data={"current_password": "wrongpass", "new_password": "newpass1234", "confirm_password": "newpass1234"},
    )
    assert status == 303
    assert "error" in headers.get("location", "")


# --- OIDC Tests ---

async def _make_no_auth_app(tmp_path, db_name="oidc.db"):
    from lora_explorer.web.app import create_app
    cfg = {
        "connection_type": "wifi", "companion_host": "", "companion_port": 4000,
        "home_lat": 0, "home_lon": 0, "db_path": str(tmp_path / db_name),
    }
    db = Database(db_path=cfg["db_path"])
    adapter = MockRadioAdapter()
    engine = GameEngine(adapter=adapter, home_lat=0, home_lon=0, db=db)
    a = create_app(engine, db, cfg, radio=adapter)
    await db.connect()
    return a, db


@pytest.mark.asyncio
async def test_oidc_only_allows_access(tmp_path):
    a, db = await _make_no_auth_app(tmp_path, "oidc_access.db")
    try:
        await db.save_oidc_config({"issuer_url": "https://id.example.com", "client_id": "test", "client_secret": "s"})
        status, body, headers = await _asgi_request(a, "GET", "/login")
        assert status == 200
        assert "Log in with SSO" in body
        assert 'action="/login"' not in body
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_middleware_redirects_setup_when_no_auth(tmp_path):
    a, db = await _make_no_auth_app(tmp_path, "noauth.db")
    try:
        status, body, headers = await _asgi_request(a, "GET", "/")
        assert status == 302
        assert "/setup" in headers.get("location", "")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_oidc_callback_route_is_public(tmp_path):
    a, db = await _make_no_auth_app(tmp_path, "oidc_pub.db")
    try:
        await db.set_setting("password_hash", hash_password("testpass123"))
        status, body, headers = await _asgi_request(a, "GET", "/auth/oidc/callback?error=access_denied&error_description=User+denied")
        assert status == 302
        assert "/login" in headers.get("location", "")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_login_shows_both_when_both_configured(tmp_path):
    a, db = await _make_no_auth_app(tmp_path, "both.db")
    try:
        await db.set_setting("password_hash", hash_password("testpass123"))
        await db.save_oidc_config({"issuer_url": "https://id.example.com", "client_id": "test", "client_secret": "s"})
        status, body, headers = await _asgi_request(a, "GET", "/login")
        assert status == 200
        assert "Log in with SSO" in body
        assert 'action="/login"' in body
        assert "or" in body
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_login_password_only(app):
    app.state._test_session_cookie = None
    status, body = await _get(app, "/login")
    assert status == 200
    assert "Log in with SSO" not in body
    assert 'action="/login"' in body


@pytest.mark.asyncio
async def test_cannot_remove_oidc_when_only_auth(tmp_path):
    a, db = await _make_no_auth_app(tmp_path, "oidc_remove.db")
    try:
        await db.save_oidc_config({"issuer_url": "https://id.example.com", "client_id": "test", "client_secret": "s"})
        secret = _get_secret(str(tmp_path / "oidc_remove.db"))
        a.state._test_session_cookie = create_session_cookie(secret)
        status, body, headers = await _asgi_request(a, "POST", "/settings/oidc/remove")
        assert status == 303
        assert "error" in headers.get("location", "")
        assert await db.get_oidc_config() is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_can_remove_oidc_when_password_exists(tmp_path):
    a, db = await _make_no_auth_app(tmp_path, "oidc_rem2.db")
    try:
        await db.set_setting("password_hash", hash_password("testpass123"))
        await db.save_oidc_config({"issuer_url": "https://id.example.com", "client_id": "test", "client_secret": "s"})
        secret = _get_secret(str(tmp_path / "oidc_rem2.db"))
        a.state._test_session_cookie = create_session_cookie(secret)
        status, body, headers = await _asgi_request(a, "POST", "/settings/oidc/remove")
        assert status == 303
        assert "success" in headers.get("location", "")
        assert await db.get_oidc_config() is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_setup_accepts_oidc_only(tmp_path):
    a, db = await _make_no_auth_app(tmp_path, "oidc_setup.db")
    try:
        status, body = await _get(a, "/setup")
        assert status == 200
        assert "Single Sign-On" in body
        assert "Password" in body
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_settings_shows_oidc_config(tmp_path):
    a, db = await _make_no_auth_app(tmp_path, "oidc_settings.db")
    try:
        await db.set_setting("password_hash", hash_password("testpass123"))
        await db.save_oidc_config({"issuer_url": "https://id.example.com", "client_id": "test", "client_secret": "s"})
        secret = _get_secret(str(tmp_path / "oidc_settings.db"))
        a.state._test_session_cookie = create_session_cookie(secret)
        status, body = await _get(a, "/settings")
        assert status == 200
        assert "id.example.com" in body
        assert "Remove" in body
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_settings_shows_oidc_setup_form(app):
    status, body = await _get(app, "/settings")
    assert status == 200
    assert "Single Sign-On (OIDC)" in body


@pytest.mark.asyncio
async def test_cli_reset_password_mode(tmp_path):
    import sqlite3
    db_path = str(tmp_path / "reset_test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO settings VALUES ('password_hash', 'fakehash')")
    conn.execute("INSERT INTO settings VALUES ('oidc_config', '{}')")
    conn.execute("INSERT INTO settings VALUES ('oidc_sub', 'user123')")
    conn.commit()
    conn.close()
    from lora_explorer.reset_password import main
    import sys
    sys.argv = ["reset", "--data-dir", db_path, "--mode", "password"]
    main()
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT key FROM settings").fetchall()
    keys = {r[0] for r in rows}
    assert "password_hash" not in keys
    assert "oidc_config" in keys
    assert "oidc_sub" in keys
    conn.close()


@pytest.mark.asyncio
async def test_cli_reset_oidc_mode(tmp_path):
    import sqlite3
    db_path = str(tmp_path / "reset_oidc.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO settings VALUES ('password_hash', 'fakehash')")
    conn.execute("INSERT INTO settings VALUES ('oidc_config', '{}')")
    conn.execute("INSERT INTO settings VALUES ('oidc_sub', 'user123')")
    conn.commit()
    conn.close()
    from lora_explorer.reset_password import main
    import sys
    sys.argv = ["reset", "--data-dir", db_path, "--mode", "oidc"]
    main()
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT key FROM settings").fetchall()
    keys = {r[0] for r in rows}
    assert "password_hash" in keys
    assert "oidc_config" not in keys
    assert "oidc_sub" not in keys
    conn.close()


@pytest.mark.asyncio
async def test_radio_redirects_without_player(app):
    status, body, headers = await _asgi_request(app, "GET", "/radio")
    assert status == 302
    assert headers.get("location", "") == "/"


@pytest.mark.asyncio
async def test_spyglass_redirects_to_radio(app, engine, adapter):
    await adapter.simulate_message("/lora survey")
    status, body, headers = await _asgi_request(app, "GET", "/spyglass")
    assert status == 301
    assert headers.get("location", "") == "/radio"


@pytest.mark.asyncio
async def test_radio_with_player(app, engine, adapter):
    await adapter.simulate_message("/lora survey")
    status, body = await _get(app, "/radio")
    assert status == 200
    assert "live-feed" in body


@pytest.mark.asyncio
async def test_sse_endpoint(app, engine, adapter):
    from urllib.parse import urlencode
    cookie = getattr(app.state, "_test_session_cookie", None)
    headers_list = [(b"content-type", b"application/x-www-form-urlencoded")]
    if cookie:
        headers_list.append((b"cookie", f"{COOKIE_NAME}={cookie}".encode()))
    scope = {
        "type": "http", "method": "GET", "path": "/api/events",
        "query_string": b"", "headers": headers_list, "root_path": "", "client": ("127.0.0.1", 0),
    }
    status_code = None
    resp_headers = {}
    body_parts = []
    disconnected = False

    async def receive():
        await asyncio.sleep(0.5)
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal status_code, disconnected
        if message["type"] == "http.response.start":
            status_code = message["status"]
            resp_headers.update({k.decode(): v.decode() for k, v in message.get("headers", [])})
            disconnected = True
        elif message["type"] == "http.response.body":
            body_parts.append(message.get("body", b""))

    import asyncio
    try:
        await asyncio.wait_for(app(scope, receive, send), timeout=2.0)
    except (asyncio.TimeoutError, Exception):
        pass
    assert status_code == 200
    assert "text/event-stream" in resp_headers.get("content-type", "")



@pytest.mark.asyncio
async def test_gear_icon_in_header(app):
    status, body = await _get(app, "/settings")
    assert status == 200
    assert "settings-gear" in body
    assert 'aria-current="page"' in body


@pytest.mark.asyncio
async def test_outposts_shows_camp(app, engine, adapter):
    await adapter.simulate_message("/lora survey")
    status, body = await _get(app, "/outposts")
    assert status == 200
    assert "Base Camp" in body
    assert "Survey Posts" in body
    assert "slots" in body


@pytest.mark.asyncio
async def test_cli_reset_all_mode(tmp_path):
    import sqlite3
    db_path = str(tmp_path / "reset_all.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO settings VALUES ('password_hash', 'fakehash')")
    conn.execute("INSERT INTO settings VALUES ('oidc_config', '{}')")
    conn.execute("INSERT INTO settings VALUES ('oidc_sub', 'user123')")
    conn.commit()
    conn.close()
    from lora_explorer.reset_password import main
    import sys
    sys.argv = ["reset", "--data-dir", db_path, "--mode", "all"]
    main()
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT key FROM settings").fetchall()
    keys = {r[0] for r in rows}
    assert "password_hash" not in keys
    assert "oidc_config" not in keys
    assert "oidc_sub" not in keys
    conn.close()


# --- Contracts ---

@pytest.mark.asyncio
async def test_dashboard_shows_contracts_at_level_5(app, engine, adapter, db):
    await adapter.simulate_message("/lora survey")
    key = (await db.get_first_player())["key"]
    from lora_explorer.game.engine import CONTRACTS_MIN_LEVEL
    await db._execute(
        "UPDATE players SET rank_level = ? WHERE key = ?", (CONTRACTS_MIN_LEVEL, key)
    )
    status, body = await _get(app, "/")
    assert status == 200
    # Target the contracts section header, not the bare phrase — the Society
    # Commission list also mentions "Expedition Contracts" as an unlock.
    assert "📜 Expedition Contracts" in body


@pytest.mark.asyncio
async def test_dashboard_hides_contracts_below_level_5(app, engine, adapter, db):
    await adapter.simulate_message("/lora survey")
    key = (await db.get_first_player())["key"]
    await db._execute("UPDATE players SET rank_level = 4 WHERE key = ?", (key,))
    status, body = await _get(app, "/")
    assert status == 200
    # The contracts section (not the commission unlock text) must stay hidden.
    assert "📜 Expedition Contracts" not in body


@pytest.mark.asyncio
async def test_buy_contract(app, engine, adapter, db):
    await adapter.simulate_message("/lora survey")
    key = (await db.get_first_player())["key"]
    await db._execute("UPDATE players SET provisions = 200 WHERE key = ?", (key,))
    contracts = await engine.ensure_weekly_contracts(key)
    status, body, headers = await _post(app, f"/contracts/{contracts[0]['id']}/buy")
    assert status == 303


# --- Requisitions ---

@pytest.mark.asyncio
async def test_merchant_buy_relic(app, engine, adapter, db):
    await adapter.simulate_message("/lora survey")
    key = (await db.get_first_player())["key"]
    await db._execute(
        "UPDATE players SET provisions = 200, base_camp_level = 5 WHERE key = ?", (key,)
    )
    now = int(__import__("time").time())
    await db._execute(
        "INSERT INTO relics (player_key, type, found_at) VALUES (?, ?, ?)",
        (key, "vigor_tonic", now),
    )
    status, _, _ = await _post(app, "/merchant/buy-relic", {"relic_type": "vigor_tonic"})
    assert status == 303
    player = await db.get_player(key)
    assert player["provisions"] == 100


@pytest.mark.asyncio
async def test_set_title_requires_completed_postcard(app, engine, adapter, db):
    await adapter.simulate_message("/lora survey")
    status, _, _ = await _post(app, "/title/set", {"title": "Strider"})
    assert status == 302
    player = await db.get_first_player()
    assert player.get("active_title") is None


@pytest.mark.asyncio
async def test_set_title_with_completed_postcard(app, engine, adapter, db):
    await adapter.simulate_message("/lora survey")
    key = (await db.get_first_player())["key"]
    now = int(__import__("time").time())
    await db._execute(
        "INSERT INTO postcards (player_key, class, description, stars, earned_at) VALUES (?, ?, ?, ?, ?)",
        (key, "Boundless", "max", 5, now),
    )
    status, _, _ = await _post(app, "/title/set", {"title": "Boundless"})
    assert status == 302
    player = await db.get_player(key)
    assert player["active_title"] == "Boundless"


@pytest.mark.asyncio
async def test_delete_survey_endpoint_rolls_back(app, engine, adapter, db):
    """Regression: DELETE /api/surveys/{id} was fully broken — _execute returned
    None so revoke_postcards_near crashed on .rowcount, and the response body
    referenced an undefined `notes`. Exercise the whole path end to end."""
    adapter.mock_position = (40.1, -105.1)
    await adapter.simulate_message("/lora survey")
    key = (await db.get_first_player())["key"]

    row = await db._fetchone(
        "SELECT id FROM surveys WHERE player_key = ? ORDER BY id DESC LIMIT 1", (key,)
    )
    sid = row["id"]

    status, body, _ = await _asgi_request(app, "DELETE", f"/api/surveys/{sid}")
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True
    rb = data["rolled_back"]
    assert "notes" not in rb
    assert rb["postcards_revoked"] >= 0
    assert await db.get_survey(sid) is None


# --- Update check & version routes -------------------------------------------

@pytest.mark.asyncio
async def test_api_version_reports_current_version(app):
    from lora_explorer import __version__
    status, body = await _get(app, "/api/version")
    assert status == 200
    assert json.loads(body)["version"] == __version__


@pytest.mark.asyncio
async def test_update_check_toggle_defaults_off(app):
    status, body = await _get(app, "/settings")
    assert "s-update-check\" " in body or 'id="s-update-check"' in body
    assert "checked" not in body.split('id="s-update-check"')[1].split(">")[0]


@pytest.mark.asyncio
async def test_update_check_toggle_persists(app, db):
    status, body, _ = await _post_json(app, "/api/update-check/toggle", {"enabled": True})
    assert status == 200
    assert json.loads(body) == {"ok": True, "enabled": True}
    from lora_explorer import update_check
    assert await update_check.is_enabled(db) is True

    status, body, _ = await _post_json(app, "/api/update-check/toggle", {"enabled": False})
    assert json.loads(body)["enabled"] is False
    assert await update_check.is_enabled(db) is False


@pytest.mark.asyncio
async def test_update_check_now_works_regardless_of_toggle(app, db, monkeypatch):
    """A manual check is allowed even while automatic checking is off — the
    click itself is the consent, matching the design in update_check.py."""
    from lora_explorer import update_check

    class _FakeResp:
        def json(self):
            return {"tag_name": "v99.0.0", "html_url": "https://example/releases/v99.0.0"}

        def raise_for_status(self):
            pass

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return _FakeResp()

    monkeypatch.setattr(update_check.httpx, "AsyncClient", lambda **_: _FakeClient())

    assert await update_check.is_enabled(db) is False  # off
    status, body, _ = await _post_json(app, "/api/update-check/now")
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert data["update_available"] is True
    assert data["latest_version"] == "v99.0.0"


@pytest.mark.asyncio
async def test_settings_shows_update_available_banner_from_cache(app, db):
    import time
    await db.set_setting("update_check_cache", json.dumps({
        "ok": True, "update_available": True, "latest_version": "v9.9.9",
        "url": "https://example/releases/v9.9.9", "checked_at": int(time.time()),
    }))
    status, body = await _get(app, "/settings")
    # Exact match, not a substring — "vv9.9.9" would also satisfy an "in"
    # check for "v9.9.9", which is exactly the double-v-prefix bug this
    # guards against (latest_version is already "vX.Y.Z" from GitHub).
    assert "<strong>v9.9.9 is available.</strong>" in body
    assert "vv9.9.9" not in body


@pytest.mark.asyncio
async def test_settings_shows_update_check_error_from_cache(app, db):
    """A failed check must surface *why* it failed, not just that it failed —
    a container owner troubleshooting "Check now" needs the actual reason
    (DNS, TLS, GitHub rate limit, etc.), not a dead-end message."""
    import time
    await db.set_setting("update_check_cache", json.dumps({
        "ok": False, "error": "[Errno -3] Temporary failure in name resolution",
        "checked_at": int(time.time()),
    }))
    status, body = await _get(app, "/settings")
    assert status == 200
    assert "Last check failed: [Errno -3] Temporary failure in name resolution" in body


@pytest.mark.asyncio
async def test_no_update_required_banner_by_default(app):
    """No multiplayer_manager is wired in the test app fixture, so the
    Worker-driven required-update banner must default to absent, not error."""
    status, body = await _get(app, "/")
    assert status == 200
    assert "Update required" not in body
