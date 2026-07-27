import time

import pytest
import pytest_asyncio
from unittest.mock import MagicMock, AsyncMock

from lora_explorer.game.database import Database
from lora_explorer.multiplayer.manager import MultiplayerManager
from lora_explorer.game.engine import (
    CHARTER_MIN_LEVEL, CHARTER_MIN_CAMP, _week_start_utc,
)


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(db_path=str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def manager(db):
    return MultiplayerManager(client=MagicMock(), db=db, engine=MagicMock())


@pytest.fixture(autouse=True)
def _stock_all_items(monkeypatch):
    """The Frontier Merchant randomly stocks only a subset of PvP Supplies each
    week; force the full catalog so buy tests can exercise any item
    deterministically. Tests for the stock gate override this locally."""
    from lora_explorer.game.engine import MULTIPLAYER_SHOP_CATALOG
    monkeypatch.setattr(
        "lora_explorer.multiplayer.manager.weekly_merchant_item_types",
        lambda *a, **k: set(MULTIPLAYER_SHOP_CATALOG),
    )


async def _charter_ready_player(db, key="k"):
    await db.get_or_create_player(key, 40.0, -105.0)
    await db._execute(
        "UPDATE players SET rank_level = ?, base_camp_level = ? WHERE key = ?",
        (CHARTER_MIN_LEVEL, CHARTER_MIN_CAMP, key),
    )


@pytest.mark.asyncio
async def test_pvp_readiness_no_player(manager):
    r = await manager.pvp_readiness()
    assert r["ready"] is False


@pytest.mark.asyncio
async def test_pvp_readiness_blocked_below_charter_license(manager, db):
    await db.get_or_create_player("k", 40.0, -105.0)
    r = await manager.pvp_readiness()
    assert r["ready"] is False
    assert "Charter License" in r["reason"]


@pytest.mark.asyncio
async def test_pvp_readiness_blocked_without_post(manager, db):
    await _charter_ready_player(db)
    r = await manager.pvp_readiness()
    assert r["ready"] is False
    assert "Survey Post" in r["reason"]


@pytest.mark.asyncio
async def test_pvp_readiness_ready_with_charter_and_post(manager, db):
    await _charter_ready_player(db)
    await db.create_post("k", "8a2a1072b59ffff", "Test Post")
    r = await manager.pvp_readiness()
    assert r["ready"] is True


@pytest.mark.asyncio
async def test_enable_pvp_blocked_when_not_ready(manager, db):
    # Charter license but no post → readiness gate must block enable_pvp.
    # The MagicMock client makes `registered` truthy, so we reach the gate.
    await _charter_ready_player(db)
    result = await manager.enable_pvp()
    assert result["ok"] is False
    assert manager.pvp_enabled is False


async def _funded_player(db, key="k", provisions=500, marks=100):
    await db.get_or_create_player(key, 40.0, -105.0)
    await db._execute(
        "UPDATE players SET provisions = ?, survey_marks = ? WHERE key = ?",
        (provisions, marks, key),
    )


@pytest.mark.asyncio
async def test_buy_item_success_deducts_marks_and_mints(manager, db):
    await _funded_player(db, marks=100)
    manager._client.buy_item = AsyncMock(
        return_value={"ok": True, "item": {"id": "item-1", "type": "attack_common"}})

    result = await manager.buy_multiplayer_item("attack_common")

    assert result["ok"] is True
    assert result["currency"] == "marks"
    manager._client.buy_item.assert_awaited_once()
    player = await db.get_first_player()
    assert player["survey_marks"] == 100 - 25  # attack_common price
    # Weekly purchase recorded and item cached locally.
    assert "mp_attack_common" in await db.get_merchant_purchases("k", _week_start_utc())
    items = await manager.get_items()
    assert any(i["id"] == "item-1" for i in items)


@pytest.mark.asyncio
async def test_buy_item_probe_deducts_provisions(manager, db):
    await _funded_player(db, provisions=500)
    manager._client.buy_item = AsyncMock(
        return_value={"ok": True, "item": {"id": "p-1", "type": "probe"}})

    result = await manager.buy_multiplayer_item("probe")

    assert result["ok"] is True
    assert result["currency"] == "provisions"
    player = await db.get_first_player()
    assert player["provisions"] == 500 - 60


@pytest.mark.asyncio
async def test_buy_item_insufficient_currency_no_worker_call(manager, db):
    await _funded_player(db, marks=2)
    manager._client.buy_item = AsyncMock()

    result = await manager.buy_multiplayer_item("attack_common")

    assert result["ok"] is False
    assert "Not enough" in result["error"]
    manager._client.buy_item.assert_not_awaited()
    player = await db.get_first_player()
    assert player["survey_marks"] == 2  # untouched


@pytest.mark.asyncio
async def test_buy_item_weekly_limit_blocks_second(manager, db):
    await _funded_player(db, marks=100)
    manager._client.buy_item = AsyncMock(
        return_value={"ok": True, "item": {"id": "d-1", "type": "defense_common"}})

    first = await manager.buy_multiplayer_item("defense_common")
    assert first["ok"] is True
    second = await manager.buy_multiplayer_item("defense_common")

    assert second["ok"] is False
    assert "this week" in second["error"]
    assert manager._client.buy_item.await_count == 1


@pytest.mark.asyncio
async def test_buy_item_rejects_non_sellable(manager, db):
    await _funded_player(db, marks=100)
    manager._client.buy_item = AsyncMock()

    result = await manager.buy_multiplayer_item("attack_epic")

    assert result["ok"] is False
    manager._client.buy_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_buy_item_rejects_item_not_stocked_this_week(manager, db, monkeypatch):
    # The merchant only stocks a weekly random subset; buying an item that is
    # not on the shelf must be refused before any Worker call or charge.
    await _funded_player(db, marks=100)
    manager._client.buy_item = AsyncMock()
    monkeypatch.setattr(
        "lora_explorer.multiplayer.manager.weekly_merchant_item_types",
        lambda *a, **k: {"defense_common"},
    )

    result = await manager.buy_multiplayer_item("attack_common")

    assert result["ok"] is False
    assert "stocked" in result["error"]
    manager._client.buy_item.assert_not_awaited()
    player = await db.get_first_player()
    assert player["survey_marks"] == 100  # untouched


@pytest.mark.asyncio
async def test_weekly_merchant_item_types_stable_and_sized():
    from lora_explorer.game.engine import (
        weekly_merchant_item_types, MULTIPLAYER_SHOP_CATALOG,
    )
    first = weekly_merchant_item_types("player-a")
    assert len(first) == 2
    assert first <= set(MULTIPLAYER_SHOP_CATALOG)
    # Deterministic within a week for the same player.
    assert weekly_merchant_item_types("player-a") == first


@pytest.mark.asyncio
async def test_buy_item_worker_failure_does_not_charge(manager, db):
    await _funded_player(db, marks=100)
    manager._client.buy_item = AsyncMock(return_value={"ok": False, "error": "Worker down"})

    result = await manager.buy_multiplayer_item("attack_common")

    assert result["ok"] is False
    player = await db.get_first_player()
    assert player["survey_marks"] == 100  # not charged on mint failure
    assert "mp_attack_common" not in await db.get_merchant_purchases("k", _week_start_utc())


async def _give_items(db, item_type, n, key="k"):
    for i in range(n):
        await db._db.execute(
            "INSERT INTO multiplayer_items (id, item_type, assigned_at, used, installed_post_hex) "
            "VALUES (?, ?, ?, 0, NULL)",
            (f"{item_type}-{i}", item_type, int(time.time()) + i),
        )
    await db._db.commit()


@pytest.mark.asyncio
async def test_salvage_credits_provisions_and_removes_only_salvaged(manager, db):
    await _funded_player(db, provisions=10)
    await _give_items(db, "attack_common", 3)
    manager._client.salvage_items = AsyncMock(return_value={
        "ok": True, "removed_ids": ["attack_common-0", "attack_common-1"], "removed_count": 2})

    result = await manager.salvage_multiplayer_item("attack_common", 2)

    assert result["ok"] is True
    assert result["count"] == 2
    assert result["currency"] == "provisions"  # salvage now pays provisions, never marks
    assert result["value"] == 2 * 10  # attack_common salvage value
    player = await db.get_first_player()
    assert player["provisions"] == 10 + 20
    # Only the two salvaged items are dropped from the local cache.
    items = await manager.get_items()
    assert len([i for i in items if i["item_type"] == "attack_common"]) == 1


@pytest.mark.asyncio
async def test_salvage_probe_credits_provisions(manager, db):
    await _funded_player(db, provisions=100)
    await _give_items(db, "probe", 1)
    manager._client.salvage_items = AsyncMock(return_value={
        "ok": True, "removed_ids": ["probe-0"], "removed_count": 1})

    result = await manager.salvage_multiplayer_item("probe", 5)

    assert result["ok"] is True
    assert result["currency"] == "provisions"
    player = await db.get_first_player()
    assert player["provisions"] == 100 + 30


@pytest.mark.asyncio
async def test_salvage_prunes_phantoms_and_retries(manager, db):
    # Local cache holds a phantom oldest probe (probe-0) the Worker no longer
    # has, plus two real ones. Oldest-first selection hits the phantom, so the
    # first Worker call removes nothing but echoes the true inventory. The
    # reconcile prunes the phantom and the retry salvages a real probe.
    await _funded_player(db, provisions=100)
    await _give_items(db, "probe", 3)  # probe-0 (oldest) .. probe-2
    real = [
        {"id": "probe-1", "type": "probe", "assigned_at": 2, "used": False},
        {"id": "probe-2", "type": "probe", "assigned_at": 3, "used": False},
    ]
    manager._client.salvage_items = AsyncMock(side_effect=[
        # First attempt selects the phantom probe-0 → nothing removed, but the
        # Worker returns its authoritative inventory (no probe-0).
        {"ok": True, "removed_ids": [], "removed_count": 0, "all_items": real},
        # Retry selects probe-1 (now the oldest real one) → removed.
        {"ok": True, "removed_ids": ["probe-1"], "removed_count": 1,
         "all_items": [real[1]]},
    ])

    result = await manager.salvage_multiplayer_item("probe", 1)

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["value"] == 30
    player = await db.get_first_player()
    assert player["provisions"] == 100 + 30
    # Two Worker calls: the phantom-hitting attempt and the successful retry.
    assert manager._client.salvage_items.await_count == 2
    # Phantom is gone and the cache mirrors the Worker's post-salvage inventory.
    ids = {i["id"] for i in await manager.get_all_items()}
    assert ids == {"probe-2"}


@pytest.mark.asyncio
async def test_salvage_all_ignores_phantoms_in_one_call(manager, db):
    # Salvaging the whole stack sends every id; the Worker removes the real ones
    # and the reconcile clears the leftover phantom, all in a single call.
    await _funded_player(db, provisions=0)
    await _give_items(db, "probe", 3)  # probe-0 phantom, probe-1/2 real
    manager._client.salvage_items = AsyncMock(return_value={
        "ok": True, "removed_ids": ["probe-1", "probe-2"], "removed_count": 2,
        "all_items": []})

    result = await manager.salvage_multiplayer_item("probe", 3)

    assert result["ok"] is True
    assert result["count"] == 2
    player = await db.get_first_player()
    assert player["provisions"] == 60
    assert manager._client.salvage_items.await_count == 1
    assert await manager.get_all_items() == []


@pytest.mark.asyncio
async def test_salvage_no_items_skips_worker(manager, db):
    await _funded_player(db)
    manager._client.salvage_items = AsyncMock()

    result = await manager.salvage_multiplayer_item("attack_common", 1)

    assert result["ok"] is False
    manager._client.salvage_items.assert_not_awaited()


@pytest.mark.asyncio
async def test_salvage_worker_failure_does_not_credit(manager, db):
    await _funded_player(db, marks=10)
    await _give_items(db, "attack_common", 1)
    manager._client.salvage_items = AsyncMock(return_value={"ok": False, "error": "Worker down"})

    result = await manager.salvage_multiplayer_item("attack_common", 1)

    assert result["ok"] is False
    player = await db.get_first_player()
    assert player["survey_marks"] == 10  # not credited on failure
    items = await manager.get_items()
    assert any(i["item_type"] == "attack_common" for i in items)  # item kept


@pytest.mark.asyncio
async def test_check_multiplayer_titles_awards_rank_one(manager, db):
    # Registered as a known player sitting at #1 on the leaderboard.
    manager._client._player_id = "me"
    leaderboard = [
        {"player_id": "me", "total_renown": 999},
        {"player_id": "rival", "total_renown": 100},
    ]
    new_ids = await manager.check_multiplayer_titles(leaderboard)
    assert set(new_ids) == {"warlord", "vanguard"}
    # Persisted, and monotonic — a later drop to rank 4 does not revoke them.
    labels = await manager.get_earned_mp_title_labels()
    assert labels == ["Warlord", "Vanguard"]
    lower = [
        {"player_id": "a", "total_renown": 900},
        {"player_id": "b", "total_renown": 800},
        {"player_id": "c", "total_renown": 700},
        {"player_id": "me", "total_renown": 100},
    ]
    assert await manager.check_multiplayer_titles(lower) == []
    assert set(await manager.get_earned_mp_title_ids()) == {"warlord", "vanguard"}


@pytest.mark.asyncio
async def test_repelled_raids_award_bulwark(manager, db):
    manager._client._player_id = "me"
    # Three inbound raids appear, then vanish while the target post still stands.
    hex_id = "8a2a1072b59ffff"
    incoming = {"posts": [{"post_hex": hex_id, "incoming_raids": [
        {"raid_id": "r1"}, {"raid_id": "r2"}, {"raid_id": "r3"},
    ]}]}
    await manager._detect_defense_changes(incoming, now=1000)
    cleared = {"posts": [{"post_hex": hex_id, "incoming_raids": []}]}
    await manager._detect_defense_changes(cleared, now=1100)
    assert "bulwark" in await manager.get_earned_mp_title_ids()


@pytest.mark.asyncio
async def test_poll_status_idle_returns_not_engaged(manager, db):
    """A quiet status poll (no in-flight or inbound raid) reports not-engaged so
    the loop falls back to the idle cadence, and makes exactly one request."""
    manager._client.get_status = AsyncMock(return_value={
        "ok": True,
        "defense": {"ok": True, "posts": [{"post_hex": "h", "incoming_raids": []}]},
        "raid": {"ok": True, "active_raid_id": None, "raid": None},
    })
    engaged = await manager._poll_status()
    assert engaged is False
    manager._client.get_status.assert_awaited_once()


@pytest.mark.asyncio
async def test_poll_status_engaged_on_inflight_raid(manager, db):
    """An in-flight own raid keeps the loop on the fast cadence."""
    manager._client.get_status = AsyncMock(return_value={
        "ok": True,
        "defense": {"ok": True, "posts": []},
        "raid": {"ok": True, "active_raid_id": "r1",
                 "raid": {"raid_id": "r1", "status": "in_flight"}},
    })
    assert await manager._poll_status() is True


@pytest.mark.asyncio
async def test_poll_status_engaged_on_incoming_raid(manager, db):
    """An inbound raid on any post keeps the loop on the fast cadence."""
    manager._client.get_status = AsyncMock(return_value={
        "ok": True,
        "defense": {"ok": True, "posts": [
            {"post_hex": "h", "incoming_raids": [{"raid_id": "r9", "eta_seconds": 1800}]},
        ]},
        "raid": {"ok": True, "active_raid_id": None, "raid": None},
    })
    assert await manager._poll_status() is True


@pytest.mark.asyncio
async def test_poll_status_failure_is_not_engaged(manager, db):
    """A failed status request degrades to the idle cadence, not a fast spin."""
    manager._client.get_status = AsyncMock(return_value={"ok": False, "error": "boom"})
    assert await manager._poll_status() is False


@pytest.mark.asyncio
async def test_try_push_logs_supply_run(manager, db):
    """A successful bundle push records a Supply Drops run (surveys + drops) so
    the Outposts card can show it after the fact."""
    await _funded_player(db)
    await db.record_survey("k", "hexA", 40.0, -105.0, 5.0, None, None, None,
                           10, 5, 0, False)
    manager._client.push_bundle = AsyncMock(return_value={
        "ok": True,
        "drops": [{"id": "d1", "type": "attack_common"},
                  {"id": "d2", "type": "probe"}],
    })

    result = await manager._try_push()

    assert result["ok"] is True
    runs = await db.get_recent_supply_runs(5)
    assert len(runs) == 1
    assert runs[0]["survey_count"] == 1
    assert runs[0]["drop_count"] == 2
    assert set(runs[0]["drops"]) == {"attack_common", "probe"}


# --- Registration anchors the push cursor (no retroactive drops) ------------

@pytest.mark.asyncio
async def test_register_anchors_push_cursor_excluding_past_surveys(manager, db):
    """Registering for multiplayer must not replay pre-registration surveys.
    The push cursor is anchored at registration time so the Worker never awards
    retroactive supply drops for surveys done before the player joined."""
    from lora_explorer.multiplayer.bundle import build_bundle

    await _funded_player(db)
    # A survey done well before the player ever registered.
    old = int(time.time()) - 86400 * 30
    await db.record_survey("k", "hexOld", 40.0, -105.0, 5.0, None, None, None,
                           10, 5, 0, False)
    await db._execute("UPDATE surveys SET surveyed_at = ? WHERE hex_id = ?",
                      (old, "hexOld"))

    manager._client.register = AsyncMock(return_value={
        "ok": True, "player_id": "p1", "secret": "s1",
    })

    before = int(time.time())
    result = await manager.register("Newcomer")
    assert result["ok"] is True
    assert manager._last_push_at >= before
    # Persisted, so a restart keeps the anchor.
    assert await manager._get_last_push_time() >= before

    # The next bundle from that cursor carries zero of the historical surveys.
    bundle = await build_bundle(db, manager._last_push_at, force=True)
    assert bundle["survey_count"] == 0


# --- PvP combat reconciliation (raze permanence, level-loss) ----------------

@pytest.mark.asyncio
async def test_apply_raid_outcome_razed_deletes_local_post(manager, db):
    await db.get_or_create_player("k", 40.0, -105.0)
    post = await db.create_post("k", "hex_r", "Doomed")
    r = await manager._apply_raid_outcome_local(post["mp_token"], "razed")
    assert r is None
    assert await db.get_post_by_id(post["id"]) is None


@pytest.mark.asyncio
async def test_apply_raid_outcome_damaged_downlevels(manager, db):
    await db.get_or_create_player("k", 40.0, -105.0)
    post = await db.create_post("k", "hex_d", "Hit")
    await db.set_post_level(post["id"], 3)
    await manager._apply_raid_outcome_local(post["mp_token"], "damaged", level_after=2)
    assert (await db.get_post_by_id(post["id"]))["level"] == 2


@pytest.mark.asyncio
async def test_apply_raid_outcome_damaged_never_raises_level(manager, db):
    await db.get_or_create_player("k", 40.0, -105.0)
    post = await db.create_post("k", "hex_d", "Hit")  # level 1
    await manager._apply_raid_outcome_local(post["mp_token"], "damaged", level_after=4)
    assert (await db.get_post_by_id(post["id"]))["level"] == 1


@pytest.mark.asyncio
async def test_apply_raid_outcome_missing_post_is_noop(manager, db):
    await db.get_or_create_player("k", 40.0, -105.0)
    await manager._apply_raid_outcome_local("nope", "razed")  # no exception


@pytest.mark.asyncio
async def test_try_push_reconciles_raze_from_notifications(manager, db, monkeypatch):
    await db.get_or_create_player("k", 40.0, -105.0)
    post = await db.create_post("k", "hex_x", "Doomed")

    manager._client.push_bundle = AsyncMock(return_value={
        "ok": True,
        "drops": [],
        "notifications": [
            {"type": "raid_razed", "data": {"post_hex": post["mp_token"]}},
        ],
    })

    await manager._try_push(force=True)
    assert await db.get_post_by_id(post["id"]) is None
