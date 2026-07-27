import json
import time
import pytest
import pytest_asyncio
from unittest.mock import MagicMock, AsyncMock

from lora_explorer.game.database import Database
from lora_explorer.multiplayer.manager import MultiplayerManager


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(db_path=str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def manager(db):
    m = MultiplayerManager(client=MagicMock(), db=db, engine=MagicMock())
    m._pvp_enabled = True
    return m


async def _add_item(db, item_id, item_type):
    await db._db.execute(
        "INSERT INTO multiplayer_items (id, item_type, assigned_at, used, installed_post_hex) "
        "VALUES (?, ?, ?, 0, NULL)",
        (item_id, item_type, int(time.time())),
    )
    await db._db.commit()


async def _player_with_marks(db, marks=100):
    await db.get_or_create_player("k", 40.0, -105.0)
    await db._execute("UPDATE players SET survey_marks = ? WHERE key = ?", (marks, "k"))


async def _item_used(db, item_id) -> bool:
    async with db._db.execute("SELECT used FROM multiplayer_items WHERE id = ?", (item_id,)) as cur:
        row = await cur.fetchone()
        return bool(row[0])


@pytest.mark.asyncio
async def test_dispatch_raid_commits_items_and_marks(manager, db):
    await _player_with_marks(db, marks=100)
    await _add_item(db, "i1", "attack_common")   # cost 5
    await _add_item(db, "i2", "attack_uncommon")  # cost 10
    manager._client.dispatch_raid = AsyncMock(return_value={
        "ok": True, "raid_id": "r1", "arrives_at": int(time.time()) + 3600, "eta_seconds": 3600,
    })

    result = await manager.dispatch_raid("target", "post_a", ["i1", "i2"])

    assert result["ok"] is True
    assert await _item_used(db, "i1") and await _item_used(db, "i2")
    player = await db.get_first_player()
    assert player["survey_marks"] == 85  # 100 - (5+10)
    attacks = await manager.get_local_attacks()
    assert any(a["id"] == "r1" and a["status"] == "in_flight" for a in attacks)


@pytest.mark.asyncio
async def test_dispatch_raid_rejects_defense_item(manager, db):
    await _player_with_marks(db)
    await _add_item(db, "d1", "defense_common")
    result = await manager.dispatch_raid("target", "post_a", ["d1"])
    assert result["ok"] is False
    assert not await _item_used(db, "d1")


@pytest.mark.asyncio
async def test_dispatch_raid_blocks_on_insufficient_marks(manager, db):
    await _player_with_marks(db, marks=1)
    await _add_item(db, "i1", "attack_epic")  # cost 5
    manager._client.dispatch_raid = AsyncMock()
    result = await manager.dispatch_raid("target", "post_a", ["i1"])
    assert result["ok"] is False
    manager._client.dispatch_raid.assert_not_called()
    assert not await _item_used(db, "i1")


async def _dispatch_inflight(manager, db, raid_id="r1"):
    await _player_with_marks(db, marks=100)
    await _add_item(db, "i1", "attack_common")
    manager._client.dispatch_raid = AsyncMock(return_value={
        "ok": True, "raid_id": raid_id, "arrives_at": int(time.time()) + 3600, "eta_seconds": 3600,
    })
    await manager.dispatch_raid("target", "post_a", ["i1"])


@pytest.mark.asyncio
async def test_get_active_raid_passes_through_in_flight(manager, db):
    inflight = {"raid_id": "r1", "status": "in_flight", "target_player_name": "Def",
                "arrives_at": int(time.time()) + 1800, "raw_power": 15, "item_types": ["attack_common"]}
    manager._client.get_my_raid = AsyncMock(return_value={"ok": True, "raid": inflight})
    raid = await manager.get_active_raid()
    assert raid["status"] == "in_flight"
    assert raid["target_player_name"] == "Def"


@pytest.mark.asyncio
async def test_get_active_raid_reconciles_resolved_row(manager, db):
    await _dispatch_inflight(manager, db)
    manager._send_notification = AsyncMock()
    resolved = {"raid_id": "r1", "status": "resolved", "outcome": "razed",
                "target_player_name": "Def", "damage_dealt": 75, "resolved_at": int(time.time())}
    manager._client.get_my_raid = AsyncMock(return_value={"ok": True, "raid": resolved})

    raid = await manager.get_active_raid()
    assert raid["outcome"] == "razed"

    attacks = await manager.get_local_attacks()
    row = next(a for a in attacks if a["id"] == "r1")
    assert row["status"] == "resolved"
    assert row["outcome"] == "razed"
    manager._send_notification.assert_awaited_once()
    manager._engine._publish_event.assert_any_call(
        "multiplayer_raid_resolved",
        {"raid_id": "r1", "target": "Def", "outcome": "razed", "damage_dealt": 75,
         "spoils_marks": 0},
    )


@pytest.mark.asyncio
async def test_resolved_raid_credits_spoils_marks_once(manager, db):
    await _dispatch_inflight(manager, db)  # spends 5 marks (attack_common), leaves 95
    manager._send_notification = AsyncMock()
    before = (await db.get_first_player())["survey_marks"]
    resolved = {"raid_id": "r1", "status": "resolved", "outcome": "razed",
                "target_player_name": "Def", "damage_dealt": 300,
                "spoils_marks": 30, "resolved_at": int(time.time())}
    manager._client.get_my_raid = AsyncMock(return_value={"ok": True, "raid": resolved})

    await manager.get_active_raid()
    after = (await db.get_first_player())["survey_marks"]
    assert after == before + 30

    # A second poll of the same resolved raid must not double-credit (the local
    # row is already 'resolved', so the reconcile short-circuits).
    await manager.get_active_raid()
    assert (await db.get_first_player())["survey_marks"] == after


@pytest.mark.asyncio
async def test_reconcile_fires_only_once(manager, db):
    await _dispatch_inflight(manager, db)
    manager._send_notification = AsyncMock()
    resolved = {"raid_id": "r1", "status": "resolved", "outcome": "defended",
                "target_player_name": "Def", "damage_dealt": 0, "resolved_at": int(time.time())}
    manager._client.get_my_raid = AsyncMock(return_value={"ok": True, "raid": resolved})

    await manager.get_active_raid()
    await manager.get_active_raid()  # second poll: local row already resolved

    manager._send_notification.assert_awaited_once()


@pytest.mark.asyncio
async def test_deploy_boost_commits_defense_items(manager, db):
    await _player_with_marks(db)
    await _add_item(db, "d1", "defense_rare")
    manager._client.deploy_boost = AsyncMock(return_value={"ok": True, "total_boost_hp": 150})
    result = await manager.deploy_boost("post_a", ["d1"])
    assert result["ok"] is True
    assert await _item_used(db, "d1")


@pytest.mark.asyncio
async def test_deploy_boost_rejects_attack_item(manager, db):
    await _player_with_marks(db)
    await _add_item(db, "a1", "attack_common")
    result = await manager.deploy_boost("post_a", ["a1"])
    assert result["ok"] is False
    assert not await _item_used(db, "a1")


@pytest.mark.asyncio
async def test_preview_raid_with_scout(manager, db):
    await _add_item(db, "i1", "attack_rare")   # power 150
    await db._db.execute(
        "INSERT INTO multiplayer_scouts (target_player_id, target_name, posts_json, scouted_at) "
        "VALUES (?, ?, ?, ?)",
        ("target", "T", json.dumps([{"post_hex": "post_a", "hp": 50, "max_hp": 50, "defense_reduction": 0.25}]), int(time.time())),
    )
    await db._db.commit()

    preview = await manager.preview_raid("target", "post_a", ["i1"])
    assert preview["scouted"] is True
    assert preview["raw_power"] == 150
    assert preview["marks_cost"] == 15
    assert preview["effective_damage"] == 113  # ceil(150 * (1 - 0.25))
    assert preview["target_defense_pct"] == 25
    assert preview["projected"] == "raze"      # 113 >= 50 hp


@pytest.mark.asyncio
async def test_preview_raid_without_scout(manager, db):
    await _add_item(db, "i1", "attack_common")
    preview = await manager.preview_raid("target", "post_a", ["i1"])
    assert preview["scouted"] is False
    assert preview["raw_power"] == 30
    assert "projected" not in preview
