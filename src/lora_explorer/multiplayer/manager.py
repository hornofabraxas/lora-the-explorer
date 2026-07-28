import asyncio
import json
import logging
import math
import time
import uuid

from ..game.database import Database
from ..game.engine import (
    GameEngine, ATTACK_MARK_COST, ATTACK_ITEM_POWER, CHARTER_MIN_LEVEL,
    CHARTER_MIN_CAMP, MULTIPLAYER_SHOP_CATALOG, MULTIPLAYER_ITEM_SALVAGE,
    _week_start_utc, weekly_merchant_item_types, CONTRACT_ITEM_REWARD_TYPES,
)
from ..game.titles import MP_TITLE_LABELS, evaluate_multiplayer_titles
from .client import WorkerClient
from .bundle import build_bundle

log = logging.getLogger(__name__)

# Poll cadences (seconds). These govern steady-state Worker/DO request volume, so
# they're the scaling dials — tune here as the player base grows (players can't and
# shouldn't set these; left to them they'd all poll as fast as possible). Rough
# per-install-per-day request cost ≈ 86400/interval for each loop.
#
# How often local game state is bundled up and pushed to the Worker. Drives
# gameplay sync (surveys → renown, item drops), so kept tight; ~24/day, negligible.
BUNDLE_INTERVAL = 3600
# Combined defense+raid status poll while *engaged* (own raid in flight or an
# inbound raid detected) — the reaction loop, kept responsive.
POLL_INTERVAL_ACTIVE = 60
# ...and while idle. The dominant steady-state cost and the main scaling dial.
# Raids travel ≥1h (TRAVEL_MIN_SECONDS on the Worker), so even a 20-minute idle
# cadence still leaves ~40+ minutes of warning before the closest possible raid
# lands; the hourly bundle and 6h cron are further backstops. Raise this first if
# request volume needs to come down.
POLL_INTERVAL_IDLE = 1200
# The leaderboard changes slowly (renown accrues over days); hourly is plenty fresh.
LEADERBOARD_INTERVAL = 3600


class MultiplayerManager:
    def __init__(self, client: WorkerClient, db: Database, engine: GameEngine):
        self._client = client
        self._db = db
        self._engine = engine
        self._push_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._leaderboard_task: asyncio.Task | None = None
        self._running = False
        self._last_push_at: int | None = None
        # When the hourly bundle *check* next runs. This ticks forward every
        # loop iteration whether or not there were surveys to send, so the
        # Outposts "Next Drop" countdown reflects the real recurring cadence
        # instead of stalling at the last successful push (which never advances
        # while 0 surveys are queued).
        self._next_push_at: int | None = None
        self._pvp_enabled: bool = False
        self._last_notification_at: int = 0
        self._prev_defense_snapshot: dict = {}
        self._seen_incoming: set[str] = set()
        # rid -> post hex for incoming raids still in flight, so a raid that
        # vanishes without razing its target can be counted as repelled (Bulwark).
        self._active_incoming: dict[str, str] = {}
        # Set when the Worker rejects a request with 426 (client below its
        # MIN_CLIENT_VERSION floor) — see WorkerClient._error_result. Cleared the
        # next time any tracked call succeeds, so it self-heals once the player
        # updates without needing a restart. Read by the dashboard to show a
        # persistent "update required" banner instead of silently stalled sync.
        self._update_required: bool = False
        self._min_client_version: str | None = None

    def _note_worker_result(self, result: dict) -> None:
        """Update the update-required flag from any Worker call's result dict.
        Called from the two recurring background loops (push, status poll) —
        the paths that run regardless of user action and so are the ones that
        can both discover the block and later discover it's lifted on their
        own, without the player having to do anything."""
        if result.get("update_required"):
            self._update_required = True
            if result.get("min_version"):
                self._min_client_version = result["min_version"]
        elif result.get("ok"):
            self._update_required = False

    async def start(self) -> None:
        settings = await self._load_settings()
        player_id = settings.get("player_id")
        secret = settings.get("secret")

        self._pvp_enabled = settings.get("pvp_enabled") == "1"

        if player_id and secret:
            self._client.set_credentials(player_id, secret)
            self._last_push_at = await self._get_last_push_time()
            log.info("Multiplayer connected as %s", player_id)
        else:
            log.info("Multiplayer not registered — use the web UI to register")

        self._running = True
        # Seed the next-check time so the Outposts countdown has something to
        # show immediately; the push loop keeps it fresh from here on.
        self._next_push_at = int(time.time()) + BUNDLE_INTERVAL
        self._push_task = asyncio.create_task(self._push_loop())
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._leaderboard_task = asyncio.create_task(self._leaderboard_loop())

    async def stop(self) -> None:
        self._running = False
        for task in (self._push_task, self._poll_task, self._leaderboard_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self._client.close()

    @property
    def registered(self) -> bool:
        return self._client._player_id is not None

    @property
    def player_id(self) -> str | None:
        return self._client._player_id

    @property
    def pvp_enabled(self) -> bool:
        return self._pvp_enabled

    @property
    def update_required(self) -> bool:
        return self._update_required

    @property
    def min_client_version(self) -> str | None:
        return self._min_client_version

    async def pvp_readiness(self) -> dict:
        """A player may only enable PvP once they hold a Charter License
        (rank + camp gate) and have built at least one Survey Post. Enabling PvP
        with zero posts is pure upside (can attack, can't be attacked)."""
        player = await self._db.get_first_player()
        if not player:
            return {"ready": False, "reason": "Survey first to begin your expedition"}
        if player["rank_level"] < CHARTER_MIN_LEVEL or player["base_camp_level"] < CHARTER_MIN_CAMP:
            return {"ready": False, "reason": "Earn your Charter License first (reach the Charter checkpoint)"}
        if await self._db.count_player_posts(player["key"]) < 1:
            return {"ready": False, "reason": "Charter at least one Survey Post before enabling PvP"}
        return {"ready": True, "reason": ""}

    async def enable_pvp(self) -> dict:
        if self._pvp_enabled:
            return {"ok": False, "error": "PvP is already enabled"}
        if not self.registered:
            return {"ok": False, "error": "Not registered"}
        readiness = await self.pvp_readiness()
        if not readiness["ready"]:
            return {"ok": False, "error": readiness["reason"]}
        self._pvp_enabled = True
        await self._save_setting("pvp_enabled", "1")
        log.info("PvP enabled for player %s", self.player_id)
        self._engine._publish_event("multiplayer_pvp_enabled", {})
        return {"ok": True}

    async def force_sync(self) -> dict | None:
        if not self.registered:
            return None
        try:
            return await self._try_push(force=True)
        except Exception:
            log.exception("Force sync failed")
            return None

    async def reanchor_push_cursor(self) -> None:
        """Snap the survey push cursor to now. Called after a backup restore:
        the restored DB carries an *older* last_push_at, which would otherwise
        replay already-counted surveys to the Worker (re-running drops against
        the daily cap). Anchoring to now discards that stale window — the rolled
        back surveys were already synced before the backup was taken."""
        now = int(time.time())
        self._last_push_at = now
        await self._set_last_push_time(now)
        log.info("Push cursor re-anchored to %d after restore", now)

    async def get_items(self) -> list[dict]:
        try:
            async with self._db._db.execute(
                "SELECT id, item_type, assigned_at, used, installed_post_token "
                "FROM multiplayer_items WHERE used = 0 ORDER BY assigned_at DESC"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []

    async def get_all_items(self) -> list[dict]:
        try:
            async with self._db._db.execute(
                "SELECT id, item_type, assigned_at, used, installed_post_token "
                "FROM multiplayer_items ORDER BY assigned_at DESC"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []

    async def scout_target(self, target_player_id: str, probe_item_id: str) -> dict:
        if not self.registered:
            return {"ok": False, "error": "Not registered"}
        result = await self._client.scout(target_player_id, probe_item_id)
        if result.get("ok"):
            await self._db._db.execute(
                "UPDATE multiplayer_items SET used = 1 WHERE id = ?",
                (probe_item_id,),
            )
            posts = result.get("posts", [])
            await self._db._db.execute(
                "INSERT OR REPLACE INTO multiplayer_scouts "
                "(target_player_id, target_name, posts_json, scouted_at, distance_mi) "
                "VALUES (?, ?, ?, ?, ?)",
                (target_player_id, "", json.dumps(posts), int(time.time()),
                 result.get("distance_mi")),
            )
            await self._db._db.commit()
            self._engine._publish_event("multiplayer_scouted", {
                "target": target_player_id,
                "post_level": result.get("post_level"),
                "post_count": result.get("post_count"),
            })
        return result

    async def get_cached_scouts(self) -> dict[str, list[dict]]:
        try:
            async with self._db._db.execute(
                "SELECT target_player_id, posts_json FROM multiplayer_scouts"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: json.loads(row[1]) for row in rows}
        except Exception:
            return {}

    async def get_scout_distances(self) -> dict[str, int | None]:
        """Fuzzed distance (miles, nearest 50) each scouted rival sits at. NULL
        when the Worker couldn't place one (missing centroid). Absent from the
        map entirely until the rival has been scouted — the Warfront shows '?'
        for those, since proximity is hidden on the leaderboard now."""
        try:
            async with self._db._db.execute(
                "SELECT target_player_id, distance_mi FROM multiplayer_scouts"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}
        except Exception:
            return {}

    async def get_scout_times(self) -> dict[str, int]:
        """When each target was last scouted, so the war room can show how stale
        the cached snapshot is (and prompt a re-scout)."""
        try:
            async with self._db._db.execute(
                "SELECT target_player_id, scouted_at FROM multiplayer_scouts"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}
        except Exception:
            return {}

    async def install_item(self, post_token: str, item_id: str) -> dict:
        if not self.registered:
            return {"ok": False, "error": "Not registered"}
        result = await self._client.install_item(post_token, item_id)
        if result.get("ok"):
            await self._db._db.execute(
                "UPDATE multiplayer_items SET used = 1, installed_post_token = ? WHERE id = ?",
                (post_token, item_id),
            )
            await self._db._db.commit()
        return result

    async def buy_multiplayer_item(self, item_type: str) -> dict:
        if not self.registered:
            return {"ok": False, "error": "Not registered"}
        spec = MULTIPLAYER_SHOP_CATALOG.get(item_type)
        if not spec:
            return {"ok": False, "error": "Item is not for sale"}

        player = await self._db.get_first_player()
        if not player:
            return {"ok": False, "error": "No player"}

        if item_type not in weekly_merchant_item_types(player["key"]):
            return {"ok": False, "error": "Not stocked this week"}

        week_start = _week_start_utc()
        purchase_key = f"mp_{item_type}"
        purchases = await self._db.get_merchant_purchases(player["key"], week_start)
        if purchases.count(purchase_key) >= spec["limit"]:
            return {"ok": False, "error": "Already purchased this week"}

        price = spec["price"]
        if spec["currency"] == "provisions":
            if player["provisions"] < price:
                return {"ok": False, "error": f"Not enough provisions (need {price} 📦)"}
        else:
            if player["survey_marks"] < price:
                return {"ok": False, "error": f"Not enough survey marks (need {price} 🪙)"}

        # Mint on the Worker first (authoritative store); only charge on success.
        purchase_id = str(uuid.uuid4())
        result = await self._client.buy_item(item_type, purchase_id)
        if not result.get("ok"):
            return result

        if spec["currency"] == "provisions":
            await self._db.deduct_provisions(player["key"], price)
        else:
            await self._db.deduct_survey_marks(player["key"], price)
        await self._db.add_merchant_purchase(player["key"], purchase_key, week_start)

        # Insert into the local display cache for instant feedback; the next
        # Worker sync overwrites this table with the authoritative inventory.
        item = result.get("item", {})
        await self._db._db.execute(
            "INSERT OR IGNORE INTO multiplayer_items "
            "(id, item_type, assigned_at, used, installed_post_token) "
            "VALUES (?, ?, ?, 0, NULL)",
            (item.get("id", purchase_id), item_type, int(time.time())),
        )
        await self._db._db.commit()

        self._engine._publish_event("multiplayer_item_purchased", {
            "item_type": item_type, "price": price, "currency": spec["currency"],
        })
        return {
            "ok": True,
            "item_type": item_type,
            "price": price,
            "currency": spec["currency"],
        }

    async def salvage_multiplayer_item(self, item_type: str, count: int) -> dict:
        """Salvage up to `count` free (unused, uninstalled) items of a type for
        currency in a single Worker call. The Worker removes them from the
        authoritative inventory; we credit from how many it actually removed, so
        a retry never double-pays.

        The local cache can drift from the Worker — e.g. items the Worker no
        longer holds can linger here. Because we salvage oldest-first, a batch of
        such phantom rows would shadow the real, salvageable items and brick the
        control ("Nothing salvaged" forever). The Worker echoes its
        authoritative inventory on every salvage, so we reconcile against it to
        prune phantoms, then retry once — the retry's selection then lands on
        items that actually exist."""
        if not self.registered:
            return {"ok": False, "error": "Not registered"}
        spec = MULTIPLAYER_ITEM_SALVAGE.get(item_type)
        if not spec:
            return {"ok": False, "error": "Item cannot be salvaged"}

        player = await self._db.get_first_player()
        if not player:
            return {"ok": False, "error": "No player"}

        count = max(1, int(count))
        removed: list[str] = []
        for _attempt in range(2):
            async with self._db._db.execute(
                "SELECT id FROM multiplayer_items WHERE item_type = ? AND used = 0 "
                "AND installed_post_token IS NULL ORDER BY assigned_at ASC LIMIT ?",
                (item_type, count),
            ) as cursor:
                ids = [row[0] for row in await cursor.fetchall()]
            if not ids:
                return {"ok": False, "error": "No items to salvage"}

            result = await self._client.salvage_items(ids)
            if not result.get("ok"):
                return result
            removed = result.get("removed_ids") or []

            # Reconcile the local cache with the Worker's authoritative inventory
            # so any phantom rows we just tried to salvage get pruned. Without
            # this, oldest-first selection keeps picking the same stale ids and
            # nothing ever salvages.
            all_items = result.get("all_items")
            if all_items is not None:
                await self._sync_items_from_worker(all_items)

            # Succeeded, or a legacy Worker without all_items (nothing to
            # reconcile against, so a retry would be pointless) — stop here.
            if removed or all_items is None:
                break
            # Otherwise the reconcile pruned phantoms; loop to retry against the
            # freshened cache, which now holds only items the Worker really has.

        if not removed:
            return {"ok": False, "error": "Nothing salvaged"}

        total = len(removed) * spec["value"]
        if spec["currency"] == "provisions":
            await self._db.add_provisions(player["key"], total)
        else:
            await self._db.add_survey_marks(player["key"], total)

        # Drop the salvaged items from the local display cache. When the Worker
        # echoed all_items above the reconcile already removed them, so this is a
        # harmless no-op there; it still covers the legacy no-all_items path.
        placeholders = ",".join("?" for _ in removed)
        await self._db._db.execute(
            f"DELETE FROM multiplayer_items WHERE id IN ({placeholders})",
            removed,
        )
        await self._db._db.commit()

        self._engine._publish_event("multiplayer_item_salvaged", {
            "item_type": item_type, "count": len(removed),
            "value": total, "currency": spec["currency"],
        })
        return {
            "ok": True,
            "item_type": item_type,
            "count": len(removed),
            "value": total,
            "currency": spec["currency"],
        }

    async def restore_hp(self, post_token: str, provisions_spent: int) -> dict:
        if not self.registered:
            return {"ok": False, "error": "Not registered"}
        return await self._client.restore_hp(post_token, provisions_spent)

    async def get_defense(self) -> dict:
        if not self.registered:
            return {"ok": False, "error": "Not registered"}
        return await self._client.get_defense()

    async def _fetch_unused_items(self, item_ids: list[str]) -> list[dict]:
        """Return {id, item_type} rows for the given ids that are unused."""
        if not item_ids:
            return []
        placeholders = ",".join("?" for _ in item_ids)
        async with self._db._db.execute(
            f"SELECT id, item_type FROM multiplayer_items "
            f"WHERE id IN ({placeholders}) AND used = 0",
            tuple(item_ids),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    def _scouted_post(self, cached_scouts: dict, target_player_id: str, post_token: str) -> dict | None:
        for post in cached_scouts.get(target_player_id, []):
            if post.get("post_token") == post_token:
                return post
        return None

    async def preview_raid(self, target_player_id: str, target_post_token: str,
                           item_ids: list[str]) -> dict:
        """Client-side damage preview from scouted intel + public item powers.
        No Worker round-trip. Projection assumes the target does not reinforce."""
        rows = await self._fetch_unused_items(item_ids)
        raw_power = sum(ATTACK_ITEM_POWER.get(r["item_type"], 0) for r in rows)
        marks_cost = sum(ATTACK_MARK_COST.get(r["item_type"], 0) for r in rows)

        scouts = await self.get_cached_scouts()
        post = self._scouted_post(scouts, target_player_id, target_post_token)
        preview = {
            "ok": True,
            "item_count": len(rows),
            "raw_power": raw_power,
            "marks_cost": marks_cost,
            "scouted": post is not None,
        }
        if post is not None:
            dr = post.get("defense_reduction", 0) or 0
            hp = post.get("hp", 0)
            # Scouts reveal the *count* of live boosts but not their HP, so a raze
            # can't be guaranteed against a defender who's actively boosting — the
            # projection downgrades "raze" to "uncertain" when boosts are present.
            active_boosts = post.get("active_boosts", 0) or 0
            scout_times = await self.get_scout_times()
            effective = max(1, math.ceil(raw_power * (1 - dr))) if raw_power > 0 else 0
            if effective <= 0:
                projected = "none"
            elif effective >= hp:
                projected = "uncertain" if active_boosts > 0 else "raze"
            else:
                projected = "damage"
            preview.update({
                "target_hp": hp,
                "target_max_hp": post.get("max_hp", hp),
                "target_defense_pct": round(dr * 100),
                "target_active_boosts": active_boosts,
                "scouted_at": scout_times.get(target_player_id),
                "effective_damage": effective,
                "projected": projected,
            })
        return preview

    async def dispatch_raid(self, target_player_id: str, target_post_token: str,
                            item_ids: list[str]) -> dict:
        """Dispatch an atomic multi-item raid: commit items + marks, send to Worker."""
        if not self.registered:
            return {"ok": False, "error": "Not registered"}
        if not self._pvp_enabled:
            return {"ok": False, "error": "PvP is not enabled"}
        if not item_ids:
            return {"ok": False, "error": "Select at least one attack item"}

        rows = await self._fetch_unused_items(item_ids)
        if len(rows) != len(set(item_ids)):
            return {"ok": False, "error": "One or more items are missing or already used"}
        if any(not r["item_type"].startswith("attack_") for r in rows):
            return {"ok": False, "error": "Only attack items can be sent on a raid"}

        marks_cost = sum(ATTACK_MARK_COST.get(r["item_type"], 0) for r in rows)
        player = await self._db.get_first_player()
        if marks_cost > 0 and (not player or player["survey_marks"] < marks_cost):
            return {"ok": False, "error": f"Not enough survey marks (need {marks_cost} 🪙)"}

        # Snapshot the projection the player was just shown in the raid picker.
        # It's derived from local scout intel the Worker never receives, so this
        # is the only record of what they expected when they committed the party.
        projection = await self.preview_raid(target_player_id, target_post_token, item_ids)

        result = await self._client.dispatch_raid(target_player_id, target_post_token, item_ids)
        if result.get("ok"):
            now = int(time.time())
            placeholders = ",".join("?" for _ in item_ids)
            await self._db._db.execute(
                f"UPDATE multiplayer_items SET used = 1 WHERE id IN ({placeholders})",
                tuple(item_ids),
            )
            await self._db._db.execute(
                "INSERT INTO multiplayer_attacks "
                "(id, direction, target_player, target_post_token, status, "
                "travel_end_at, created_at, projection) "
                "VALUES (?, 'outgoing', ?, ?, 'in_flight', ?, ?, ?)",
                (result["raid_id"], target_player_id, target_post_token,
                 result.get("arrives_at", now), now, json.dumps(projection)),
            )
            await self._db._db.commit()
            if marks_cost > 0:
                await self._db.deduct_survey_marks(player["key"], marks_cost)
            self._engine._publish_event("multiplayer_raid_dispatched", {
                "raid_id": result["raid_id"],
                "target": target_player_id,
                "arrives_at": result.get("arrives_at"),
                "eta_seconds": result.get("eta_seconds"),
                "items": len(item_ids),
            })
        return result

    async def deploy_boost(self, post_token: str, item_ids: list[str]) -> dict:
        """Deploy defense items as temporary flat-HP boosts on one of your posts."""
        if not self.registered:
            return {"ok": False, "error": "Not registered"}
        if not item_ids:
            return {"ok": False, "error": "Select at least one defense item"}

        rows = await self._fetch_unused_items(item_ids)
        if len(rows) != len(set(item_ids)):
            return {"ok": False, "error": "One or more items are missing or already used"}
        if any(not r["item_type"].startswith("defense_") for r in rows):
            return {"ok": False, "error": "Only defense items can be deployed as boosts"}

        result = await self._client.deploy_boost(post_token, item_ids)
        if result.get("ok"):
            placeholders = ",".join("?" for _ in item_ids)
            await self._db._db.execute(
                f"UPDATE multiplayer_items SET used = 1 WHERE id IN ({placeholders})",
                tuple(item_ids),
            )
            await self._db._db.commit()
            self._engine._publish_event("multiplayer_boost_deployed", {
                "post_token": post_token, "items": len(item_ids),
                "total_boost_hp": result.get("total_boost_hp"),
            })
        return result

    async def get_local_attacks(self) -> list[dict]:
        try:
            async with self._db._db.execute(
                "SELECT id, direction, target_player, target_post_token, status, "
                "resolved_at, outcome, created_at "
                "FROM multiplayer_attacks ORDER BY created_at DESC LIMIT 20"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []

    async def get_active_raid(self) -> dict | None:
        """The player's current raiding party, if any — the single in-flight raid
        or its landed result. Fetches the authoritative record from the Worker and
        reconciles the local row (so an in_flight→resolved transition is caught even
        on page render, not just the poll loop). Returns None when idle."""
        if not self.registered or not self._pvp_enabled:
            return None
        result = await self._client.get_my_raid()
        if not result.get("ok"):
            return None
        raid = result.get("raid")
        if raid and raid.get("status") == "resolved":
            await self._reconcile_resolved_raid(raid, int(time.time()))
        if raid:
            raid["projection"] = await self._raid_projection(raid.get("raid_id"))
        return raid

    async def _raid_projection(self, raid_id: str | None) -> dict | None:
        """The damage projection snapshotted when this raid was dispatched, or
        None if it was launched from a different install (the snapshot is local)."""
        if not raid_id:
            return None
        try:
            async with self._db._db.execute(
                "SELECT projection FROM multiplayer_attacks WHERE id = ?", (raid_id,)
            ) as cur:
                row = await cur.fetchone()
            return json.loads(row["projection"]) if row and row["projection"] else None
        except Exception:
            return None

    async def _reconcile_resolved_raid(self, raid: dict, now: int) -> None:
        """Mark a landed raid resolved in the local table exactly once, and alert
        the attacker (event + mesh/webhook) on the in_flight→resolved transition."""
        raid_id = raid.get("raid_id")
        if not raid_id:
            return
        async with self._db._db.execute(
            "SELECT status, target_player FROM multiplayer_attacks WHERE id = ?", (raid_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row or row["status"] != "in_flight":
            return  # already reconciled (or not ours) — fire once

        outcome = raid.get("outcome")
        await self._db._db.execute(
            "UPDATE multiplayer_attacks SET status = 'resolved', outcome = ?, "
            "resolved_at = ? WHERE id = ?",
            (outcome, raid.get("resolved_at", now), raid_id),
        )
        await self._db._db.commit()

        # Raid spoils: the Worker records how many marks this raid earned; credit
        # them locally exactly once (this reconcile only fires on the in_flight →
        # resolved transition, so it can't double-pay). Marks live on the game
        # server, so this is where an attack finally pays back into the economy.
        spoils = int(raid.get("spoils_marks") or 0)
        if spoils > 0:
            player = await self._db.get_first_player()
            if player:
                await self._db.add_survey_marks(player["key"], spoils)

        self._engine._publish_event("multiplayer_raid_resolved", {
            "raid_id": raid_id,
            "target": raid.get("target_player_name") or row["target_player"],
            "outcome": outcome,
            "damage_dealt": raid.get("damage_dealt"),
            "spoils_marks": spoils,
        })

        target = raid.get("target_player_name") or "the target"
        spoils_tag = f" +{spoils}🪙" if spoils > 0 else ""
        verb = {
            "razed": f"Your raiding party razed {target}'s outpost!{spoils_tag} 💥",
            "damaged": f"Your raid knocked {target}'s outpost down a level.{spoils_tag} ⚠️",
            "defended": f"Your raid on {target} was turned back — defenses held. 🛡️",
        }.get(outcome, f"Your raid on {target} has returned.")
        await self._send_notification(verb, now)

    async def _poll_status(self) -> bool:
        """One combined poll of defense state + the player's own raid (a single
        /api/status request). Applies the same defense-change detection and raid
        reconciliation the two separate polls used to. Returns True while the
        player is *engaged* — a raid in flight or an inbound raid detected — so the
        loop holds the fast cadence; False lets it fall back to the idle cadence."""
        now = int(time.time())
        status = await self._client.get_status()
        self._note_worker_result(status)
        if not status.get("ok"):
            return False

        engaged = False

        defense = status.get("defense") or {}
        if defense.get("ok"):
            await self._detect_defense_changes(defense, now)
            await self._cache_put("defense", json.dumps(defense), now)
            for post in defense.get("posts", []):
                if post.get("incoming_raids"):
                    engaged = True

        raid_result = status.get("raid") or {}
        if raid_result.get("ok"):
            if raid_result.get("active_raid_id"):
                engaged = True
            raid = raid_result.get("raid")
            if raid and raid.get("status") == "resolved":
                await self._reconcile_resolved_raid(raid, now)

        return engaged

    async def get_cached_defense(self) -> dict | None:
        """The last defense snapshot the poll loop stored, tagged with the time
        it was cached (`_cached_at`), so callers like the dashboard dispatch can
        surface inbound raids without a Worker round-trip. The poll loop drops to
        a 60s cadence whenever a raid is inbound, so this stays fresh exactly when
        it matters. Returns None if nothing has been cached yet."""
        try:
            async with self._db._db.execute(
                "SELECT value, updated_at FROM multiplayer_cache WHERE key = 'defense'"
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    data = json.loads(row[0])
                    data["_cached_at"] = row[1]
                    return data
        except Exception:
            pass
        return None

    async def get_cached_leaderboard(self) -> list[dict]:
        try:
            async with self._db._db.execute(
                "SELECT value FROM multiplayer_cache WHERE key = 'leaderboard'"
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return json.loads(row[0])
        except Exception:
            pass
        return []

    async def register(self, display_name: str, invite_code: str | None = None) -> dict:
        result = await self._client.register(display_name, invite_code=invite_code)
        if result.get("ok"):
            self._client.set_credentials(result["player_id"], result["secret"])
            await self._save_setting("player_id", result["player_id"])
            await self._save_setting("secret", result["secret"])
            # Anchor the push cursor at registration time so only surveys made
            # AFTER joining multiplayer earn supply drops. Without this the first
            # push (the force-sync below) would run with since=0, replay the
            # player's entire survey history, and the Worker would award a
            # retroactive flood of item drops for surveys done pre-registration.
            now = int(time.time())
            self._last_push_at = now
            await self._set_last_push_time(now)
            log.info("Registered with Worker as %s", result["player_id"])
        return result

    async def _push_loop(self) -> None:
        await asyncio.sleep(5)
        while self._running:
            try:
                if self.registered:
                    await self._try_push()
            except Exception:
                log.exception("Bundle push error")
            # Record when the next check fires so the UI can count down to the
            # recurring run, not the last push. This advances every iteration,
            # even the ones with nothing to send.
            self._next_push_at = int(time.time()) + BUNDLE_INTERVAL
            await asyncio.sleep(BUNDLE_INTERVAL)

    async def _poll_loop(self) -> None:
        await asyncio.sleep(10)
        while self._running:
            interval = POLL_INTERVAL_IDLE
            try:
                if self.registered and self._pvp_enabled:
                    engaged = await self._poll_status()
                    interval = POLL_INTERVAL_ACTIVE if engaged else POLL_INTERVAL_IDLE
            except Exception:
                log.exception("Status poll error")
            await asyncio.sleep(interval)

    async def _leaderboard_loop(self) -> None:
        await asyncio.sleep(15)
        while self._running:
            try:
                if self.registered:
                    await self._poll_leaderboard()
            except Exception:
                log.exception("Leaderboard poll error")
            await asyncio.sleep(LEADERBOARD_INTERVAL)

    async def _poll_leaderboard(self) -> None:
        now = int(time.time())
        result = await self._client.get_leaderboard()
        if "players" in result:
            await self._cache_put("leaderboard", json.dumps(result["players"]), now)
            await self.check_multiplayer_titles(result["players"])

    # --- Multiplayer titles --------------------------------------------------
    async def get_earned_mp_title_ids(self) -> set[str]:
        """Multiplayer title ids the player has earned (persisted once earned)."""
        settings = await self._load_settings()
        raw = settings.get("earned_titles", "")
        return {t for t in raw.split(",") if t}

    async def get_earned_mp_title_labels(self) -> list[str]:
        """Display labels for earned multiplayer titles (registry order)."""
        earned = await self.get_earned_mp_title_ids()
        return [MP_TITLE_LABELS[tid] for tid in MP_TITLE_LABELS if tid in earned]

    def _my_rank(self, players: list[dict]) -> int | None:
        for i, p in enumerate(players):
            if p.get("player_id") == self.player_id:
                return i + 1
        return None

    async def _count_raids_won(self) -> int:
        try:
            async with self._db._db.execute(
                "SELECT COUNT(*) FROM multiplayer_attacks "
                "WHERE direction = 'outgoing' AND outcome IN ('razed', 'damaged')"
            ) as cur:
                row = await cur.fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return 0

    async def _increment_raids_repelled(self, n: int) -> None:
        settings = await self._load_settings()
        total = int(settings.get("raids_repelled", "0") or 0) + n
        await self._save_setting("raids_repelled", str(total))
        await self.check_multiplayer_titles()

    async def _count_scouts(self) -> int:
        try:
            async with self._db._db.execute(
                "SELECT COUNT(*) FROM multiplayer_scouts"
            ) as cur:
                row = await cur.fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return 0

    async def check_multiplayer_titles(self, players: list[dict] | None = None) -> list[str]:
        """Evaluate multiplayer title conditions against live data and persist any
        newly-earned ids. ``players`` is a fresh leaderboard list when available
        (from the poll); otherwise the cached leaderboard is used so the rank
        checks still run on page render. Returns newly-earned title ids."""
        if not self.registered:
            return []
        if players is None:
            players = await self.get_cached_leaderboard()

        settings = await self._load_settings()
        rank = self._my_rank(players) if players else None
        eligible = evaluate_multiplayer_titles(
            rank=rank,
            raids_won=await self._count_raids_won(),
            raids_repelled=int(settings.get("raids_repelled", "0") or 0),
            scouts=await self._count_scouts(),
        )
        earned = {t for t in settings.get("earned_titles", "").split(",") if t}
        new_ids = eligible - earned
        if new_ids:
            merged = ",".join(sorted(earned | new_ids))
            await self._save_setting("earned_titles", merged)
            for tid in new_ids:
                label = MP_TITLE_LABELS.get(tid, tid)
                self._engine._publish_event("multiplayer_title_earned", {
                    "title_id": tid, "title": label,
                })
        return list(new_ids)

    async def _try_push(self, force: bool = False) -> dict | None:
        since = self._last_push_at

        # Tier-IV munitions won from Expedition Contracts are minted here (on the
        # Worker's authoritative store), not by the game engine. Ride them along
        # with the survey bundle; a pending grant forces a push even with no new
        # surveys, so a contract completed while idle still pays out.
        player = await self._db.get_first_player()
        grants = []
        if player:
            grants = await self._db.get_pending_contract_item_grants(
                player["key"], CONTRACT_ITEM_REWARD_TYPES,
            )

        bundle = await build_bundle(self._db, since, force=force or bool(grants))
        if not bundle:
            log.debug("No new surveys to push")
            return None

        if grants:
            # Idempotent grant id derived from the contract row, so a retried push
            # (or a duplicate bundle) can never mint the item twice.
            bundle["item_grants"] = [
                {"id": f"contract-{g['id']}-{g['reward_type']}", "type": g["reward_type"]}
                for g in grants
            ]

        result = await self._client.push_bundle(bundle)
        self._note_worker_result(result)
        if result.get("ok"):
            now = int(time.time())
            self._last_push_at = now
            await self._set_last_push_time(now)

            # The Worker minted (or already held) the contract munitions — settle
            # them locally so they aren't re-sent. The all_items sync below pulls
            # the freshly minted item into the local inventory cache.
            for g in grants:
                await self._db.mark_contract_reward_granted(g["id"])
            if grants:
                await self._db._db.commit()

            # Reconcile combat outcomes the Worker reports back. This is the
            # authoritative, exactly-once delivery (the defense poll's snapshot
            # diff can miss a raze that landed while we were offline, since the
            # post was never in a prior snapshot).
            for note in result.get("notifications", []):
                data = note.get("data") or {}
                ntype = note.get("type", "")
                if ntype == "raid_razed":
                    await self._apply_raid_outcome_local(data.get("post_token"), "razed")
                elif ntype == "raid_damaged":
                    await self._apply_raid_outcome_local(
                        data.get("post_token"), "damaged", data.get("level_after"),
                    )

            all_items = result.get("all_items")
            if all_items is not None:
                await self._sync_items_from_worker(all_items)
            else:
                drops = result.get("drops", [])
                if drops:
                    await self._store_drops(drops, bundle["timestamp"])
                    log.info("Received %d item drops from Worker", len(drops))

            drops = result.get("drops", [])
            self._engine._publish_event("multiplayer_bundle_pushed", {
                "survey_count": bundle["survey_count"],
                "drops": len(drops),
            })

            # Record the run for the Outposts Supply Drops log so a player who
            # wasn't watching the live feed can still see what landed and when.
            await self._db.log_supply_run(
                now, bundle["survey_count"], [d["type"] for d in drops],
            )

            if drops:
                self._engine._publish_event("multiplayer_items_received", {
                    "items": [{"type": d["type"], "id": d["id"]} for d in drops],
                    "survey_count": bundle["survey_count"],
                })
            return result
        else:
            log.warning("Bundle push failed: %s", result.get("error", "unknown"))
            return result

    async def _sync_items_from_worker(self, worker_items: list[dict]) -> None:
        await self._db._db.execute("DELETE FROM multiplayer_items")
        for item in worker_items:
            await self._db._db.execute(
                "INSERT OR IGNORE INTO multiplayer_items "
                "(id, item_type, assigned_at, used, installed_post_token) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    item["id"],
                    item["type"],
                    item.get("assigned_at", int(time.time())),
                    1 if item.get("used", False) else 0,
                    item.get("installed_post_token") or None,
                ),
            )
        await self._db._db.commit()
        log.info("Synced %d items from Worker (local overwrite)", len(worker_items))

    async def _store_drops(self, drops: list[dict], bundle_timestamp: int) -> None:
        for drop in drops:
            try:
                await self._db._db.execute(
                    "INSERT OR IGNORE INTO multiplayer_items "
                    "(id, item_type, assigned_at, used, bundle_timestamp) "
                    "VALUES (?, ?, ?, 0, ?)",
                    (drop["id"], drop["type"], int(time.time()), bundle_timestamp),
                )
            except Exception:
                log.debug("Failed to store drop %s", drop.get("id"))
        await self._db._db.commit()

    async def _apply_raid_outcome_local(
        self, post_token: str | None, outcome: str, level_after: int | None = None,
    ) -> None:
        """Reconcile a local Survey Post to a Worker combat outcome. A raze
        permanently destroys the outpost (delete it locally so it stops being
        re-pushed and resurrected); a level-loss knocks it down one level. The
        Worker is authoritative here, so this always follows its verdict.
        Idempotent — a missing/already-reconciled post is a no-op."""
        if not post_token:
            return
        # The Worker refers to posts only by their opaque mp_token — resolve it
        # to the local record.
        post = await self._db.get_post_by_worker_ref(post_token)
        if not post:
            return
        if outcome == "razed":
            await self._db.delete_post(post["id"])
        elif outcome == "damaged" and level_after is not None:
            new_level = max(1, int(level_after))
            if new_level < post["level"]:
                await self._db.set_post_level(post["id"], new_level)

    async def _detect_defense_changes(self, new_defense: dict, now: int) -> None:
        new_posts = {p["post_token"]: p for p in new_defense.get("posts", [])}
        old_posts = self._prev_defense_snapshot
        alerts = []

        for post_token, old in old_posts.items():
            new = new_posts.get(post_token)
            if not new:
                alerts.append(f"Post razed: {post_token[:8]}")
                # The Worker destroyed this outpost — drop the local record so it
                # isn't re-pushed (and resurrected) on the next bundle.
                await self._apply_raid_outcome_local(post_token, "razed")
            elif new.get("hp", old.get("hp", 0)) < old.get("hp", 0):
                if new.get("level", 0) < old.get("level", 0):
                    alerts.append(f"Post {post_token[:8]} lost a level! HP: {new['hp']}/{new['max_hp']}")
                    await self._apply_raid_outcome_local(post_token, "damaged", new.get("level"))
                else:
                    alerts.append(f"Post {post_token[:8]} under attack! HP: {new['hp']}/{new['max_hp']}")

        # Inbound raids — warn once per raid, with ETA and coarse threat band,
        # and track which raids are still in flight (rid -> target hex) so we can
        # detect repels below.
        current_incoming: dict[str, str] = {}
        for post_token, post in new_posts.items():
            for raid in post.get("incoming_raids", []):
                rid = raid.get("raid_id")
                if not rid:
                    continue
                current_incoming[rid] = post_token
                if rid in self._seen_incoming:
                    continue
                self._seen_incoming.add(rid)
                eta_min = max(1, round(raid.get("eta_seconds", 0) / 60))
                threat = {
                    "raze": "projected to RAZE",
                    "heavy": "projected heavy damage",
                    "hold": "your defenses should hold",
                }.get(raid.get("threat", "hold"), "inbound")
                alerts.append(f"Raid inbound on {post_token[:8]} — ETA {eta_min}m, {threat}")

        # A tracked incoming raid that has vanished has landed: if its target post
        # still stands (wasn't razed off the map) the assault was repelled — the
        # currency for the Bulwark title.
        repelled = 0
        for rid, post_token in self._active_incoming.items():
            if rid in current_incoming:
                continue
            if post_token in new_posts:
                repelled += 1
            self._seen_incoming.discard(rid)
        if repelled:
            await self._increment_raids_repelled(repelled)
        self._active_incoming = current_incoming

        self._prev_defense_snapshot = new_posts

        for msg in alerts:
            log.info("PvP alert: %s", msg)
            self._engine._publish_event("multiplayer_pvp_alert", {"message": msg})
            await self._send_notification(msg, now)

    async def _send_notification(self, message: str, now: int) -> None:
        MESH_COOLDOWN = 300  # 5 minutes between mesh notifications

        if await self._mesh_notify_enabled() and now - self._last_notification_at >= MESH_COOLDOWN:
            try:
                player = await self._db.get_first_player()
                if player and self._engine._adapter:
                    sent = await self._engine._adapter.send_message(
                        player["key"], f"[PvP] {message}"
                    )
                    if sent:
                        self._last_notification_at = now
                        log.info("Sent mesh notification: %s", message)
            except Exception:
                log.debug("Mesh notification failed")

        webhook_url = await self._get_webhook_url()
        if webhook_url:
            await self._fire_webhook(webhook_url, message)

    async def _get_webhook_url(self) -> str | None:
        settings = await self._load_settings()
        url = settings.get("webhook_url", "")
        return url if url else None

    async def _fire_webhook(self, url: str, message: str) -> None:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(url, json={
                    "content": f"[LoRa PvP] {message}",
                    "text": f"[LoRa PvP] {message}",
                })
        except Exception:
            log.debug("Webhook notification failed")

    async def set_webhook_url(self, url: str) -> None:
        await self._save_setting("webhook_url", url.strip())

    async def _mesh_notify_enabled(self) -> bool:
        """Whether PvP alerts should be relayed over the LoRa mesh. Default on."""
        settings = await self._load_settings()
        return settings.get("mesh_notify", "true") != "false"

    async def set_mesh_notify(self, enabled: bool) -> None:
        await self._save_setting("mesh_notify", "true" if enabled else "false")

    async def _cache_put(self, key: str, value: str, timestamp: int) -> None:
        try:
            await self._db._db.execute(
                "INSERT OR REPLACE INTO multiplayer_cache (key, value, updated_at) "
                "VALUES (?, ?, ?)",
                (key, value, timestamp),
            )
            await self._db._db.commit()
        except Exception:
            log.debug("Failed to cache %s", key)

    async def _load_settings(self) -> dict:
        try:
            async with self._db._db.execute(
                "SELECT key, value FROM multiplayer_settings"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}
        except Exception:
            return {}

    async def _save_setting(self, key: str, value: str) -> None:
        await self._db._db.execute(
            "INSERT OR REPLACE INTO multiplayer_settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        await self._db._db.commit()

    async def _get_last_push_time(self) -> int | None:
        settings = await self._load_settings()
        val = settings.get("last_push_at")
        return int(val) if val else None

    async def _set_last_push_time(self, timestamp: int) -> None:
        await self._save_setting("last_push_at", str(timestamp))
