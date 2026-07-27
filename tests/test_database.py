import pytest
import pytest_asyncio
from lora_explorer.game.database import Database


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(db_path=str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_create_player(db):
    player = await db.get_or_create_player("key1", 40.0, -105.0)
    assert player["key"] == "key1"
    assert player["xp"] == 0
    assert player["provisions"] == 0
    assert "field_notes" not in player  # retired column


@pytest.mark.asyncio
async def test_get_existing_player(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    player = await db.get_or_create_player("key1", 0.0, 0.0)
    assert player["home_lat"] == 40.0  # original coords preserved


@pytest.mark.asyncio
async def test_add_xp(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    total = await db.add_xp("key1", 100)
    assert total == 100
    total = await db.add_xp("key1", 50)
    assert total == 150


@pytest.mark.asyncio
async def test_add_provisions(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    total = await db.add_provisions("key1", 30)
    assert total == 30


@pytest.mark.asyncio
async def test_hex_discovery(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    assert not await db.is_hex_discovered("key1", "hex_a")
    await db.discover_hex("key1", "hex_a")
    assert await db.is_hex_discovered("key1", "hex_a")
    assert await db.count_discovered_hexes("key1") == 1


@pytest.mark.asyncio
async def test_hex_discovery_per_player(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    await db.get_or_create_player("key2", 40.0, -105.0)
    await db.discover_hex("key1", "hex_a")
    assert await db.is_hex_discovered("key1", "hex_a")
    assert not await db.is_hex_discovered("key2", "hex_a")


@pytest.mark.asyncio
async def test_survey_daily_limit(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    assert not await db.was_hex_surveyed_today("key1", "hex_a")
    await db.record_survey(
        player_key="key1", hex_id="hex_a", lat=40.0, lon=-105.0,
        distance_miles=5.0, snr=-8.0, rssi=-105, hops=2,
        xp_earned=40, provisions_earned=18, field_notes_earned=2,
        is_discovery=True,
    )
    assert await db.was_hex_surveyed_today("key1", "hex_a")
    assert not await db.was_hex_surveyed_today("key1", "hex_b")


@pytest.mark.asyncio
async def test_count_surveys_today_counts_todays_surveys(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    assert await db.count_surveys_today("key1") == 0
    await db.record_survey(
        player_key="key1", hex_id="hex_a", lat=40.0, lon=-105.0,
        distance_miles=5.0, snr=-8.0, rssi=-105, hops=2,
        xp_earned=40, provisions_earned=18, field_notes_earned=2,
        is_discovery=True,
    )
    assert await db.count_surveys_today("key1") == 1


@pytest.mark.asyncio
async def test_vigor_tonic_rearms_first_survey_of_day(db):
    # count_surveys_today drives the First Survey of the Day bonus. A Vigor Tonic
    # sets cooldown_override to grant a fresh survey day, so surveys done before
    # the tonic must stop counting — otherwise the bonus never re-arms.
    import time
    now = int(time.time())
    await db.get_or_create_player("key1", 40.0, -105.0)
    await db.record_survey(
        player_key="key1", hex_id="hex_a", lat=40.0, lon=-105.0,
        distance_miles=5.0, snr=-8.0, rssi=-105, hops=2,
        xp_earned=40, provisions_earned=18, field_notes_earned=2,
        is_discovery=True,
    )
    assert await db.count_surveys_today("key1") == 1  # pre-tonic: would suppress bonus

    await db._execute(
        "UPDATE players SET cooldown_override = ? WHERE key = ?", (now + 5, "key1"),
    )
    # Fresh survey day: earlier surveys no longer count, next survey is first-of-day.
    assert await db.count_surveys_today("key1") == 0


@pytest.mark.asyncio
async def test_salvage_relic_grants_provisions(db):
    value = {"vigor_tonic": 50, "wardstone": 75}
    await db.get_or_create_player("key1", 40.0, -105.0)
    await db.add_relic("key1", "vigor_tonic", "hex_a")
    rid = (await db.get_unused_relics("key1"))[0]["id"]

    amount = await db.salvage_relic(rid, "key1", value)
    assert amount == 50
    player = await db.get_player("key1")
    assert player["provisions"] == 50
    # Relic is consumed — a second salvage is a no-op.
    assert await db.salvage_relic(rid, "key1", value) is None


@pytest.mark.asyncio
async def test_salvage_relic_rejects_non_salvageable_type(db):
    value = {"vigor_tonic": 50, "wardstone": 75}
    await db.get_or_create_player("key1", 40.0, -105.0)
    await db.add_relic("key1", "buried_cache", "hex_a")
    cache = (await db.get_unused_relics("key1"))[0]

    assert await db.salvage_relic(cache["id"], "key1", value) is None
    # Buried Cache is untouched — no provisions granted, relic still available.
    assert (await db.get_player("key1"))["provisions"] == 0
    assert any(r["id"] == cache["id"] for r in await db.get_unused_relics("key1"))


@pytest.mark.asyncio
async def test_survey_marks(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    total = await db.add_survey_marks("key1", 1)
    assert total == 1


@pytest.mark.asyncio
async def test_pending_player_adoption(db):
    await db.get_or_create_player("pending", 40.0, -105.0)
    player = await db.get_or_create_player("real_key", 40.0, -105.0)
    assert player["key"] == "real_key"
    assert player["home_lat"] == 40.0
    pending = await db.get_player("pending")
    assert pending is None


@pytest.mark.asyncio
async def test_create_post_assigns_mp_token(db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    post = await db.create_post("key1", "88aa00fffffffff", "Test Post")
    assert post["mp_token"]
    assert post["mp_token"] != post["hex_id"]
    # Resolvable by either the token or (legacy) the hex.
    assert (await db.get_post_by_worker_ref(post["mp_token"]))["id"] == post["id"]
    assert (await db.get_post_by_worker_ref(post["hex_id"]))["id"] == post["id"]


@pytest.mark.asyncio
async def test_token_backfill_registered_keeps_hex(tmp_path):
    """A registered install already pushed real hexes to the Worker, so its
    pre-token posts keep token = hex_id (preserves Worker-side identity)."""
    path = str(tmp_path / "backfill_reg.db")
    db1 = Database(db_path=path)
    await db1.connect()
    await db1.get_or_create_player("key1", 40.0, -105.0)
    await db1.create_post("key1", "88bb00fffffffff", "Old Post")
    # Simulate a pre-token post and a registered install.
    await db1._db.execute("UPDATE survey_posts SET mp_token = NULL")
    await db1._db.execute(
        "INSERT OR REPLACE INTO multiplayer_settings (key, value) VALUES ('player_id', 'abc')"
    )
    await db1._db.commit()
    await db1.close()

    db2 = Database(db_path=path)
    await db2.connect()  # migrations + backfill run here
    post = await db2.get_any_post_in_hex("88bb00fffffffff")
    assert post["mp_token"] == "88bb00fffffffff"
    await db2.close()


@pytest.mark.asyncio
async def test_token_backfill_unregistered_gets_random(tmp_path):
    """A never-registered install never exposed its hexes — pre-token posts get
    fresh random tokens so the hexes stay private for good."""
    path = str(tmp_path / "backfill_unreg.db")
    db1 = Database(db_path=path)
    await db1.connect()
    await db1.get_or_create_player("key1", 40.0, -105.0)
    await db1.create_post("key1", "88cc00fffffffff", "Fresh Post")
    await db1._db.execute("UPDATE survey_posts SET mp_token = NULL")
    await db1._db.commit()
    await db1.close()

    db2 = Database(db_path=path)
    await db2.connect()
    post = await db2.get_any_post_in_hex("88cc00fffffffff")
    assert post["mp_token"]
    assert post["mp_token"] != "88cc00fffffffff"
    await db2.close()
