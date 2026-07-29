import aiosqlite
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from ..paths import default_db_path

log = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH") or default_db_path()

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    key TEXT PRIMARY KEY,
    xp INTEGER NOT NULL DEFAULT 0 CHECK(xp >= 0),
    provisions INTEGER NOT NULL DEFAULT 0 CHECK(provisions >= 0),
    survey_marks INTEGER NOT NULL DEFAULT 0 CHECK(survey_marks >= 0),
    rank_level INTEGER NOT NULL DEFAULT 1,
    base_camp_level INTEGER NOT NULL DEFAULT 1,
    home_lat REAL,
    home_lon REAL,
    home_validated INTEGER NOT NULL DEFAULT 0,
    scan_count INTEGER NOT NULL DEFAULT 0,
    last_survey_lat REAL,
    last_survey_lon REAL,
    last_survey_at INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS hexes (
    hex_id TEXT NOT NULL,
    player_key TEXT NOT NULL,
    discovered_at INTEGER NOT NULL,
    PRIMARY KEY (hex_id, player_key),
    FOREIGN KEY (player_key) REFERENCES players(key)
);

CREATE TABLE IF NOT EXISTS surveys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_key TEXT NOT NULL,
    hex_id TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    distance_miles REAL NOT NULL,
    snr REAL,
    rssi INTEGER,
    hops INTEGER,
    xp_earned INTEGER NOT NULL,
    provisions_earned INTEGER NOT NULL,
    field_notes_earned INTEGER NOT NULL,
    is_discovery INTEGER NOT NULL DEFAULT 0,
    surveyed_at INTEGER NOT NULL,
    FOREIGN KEY (player_key) REFERENCES players(key)
);

CREATE TABLE IF NOT EXISTS survey_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_key TEXT NOT NULL,
    hex_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 1,
    last_tended_at INTEGER NOT NULL,
    last_collected_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (player_key) REFERENCES players(key)
);

CREATE TABLE IF NOT EXISTS postcards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_key TEXT NOT NULL,
    class TEXT NOT NULL,
    stars INTEGER NOT NULL,
    description TEXT NOT NULL,
    distance_miles REAL,
    snr REAL,
    earned_at INTEGER NOT NULL,
    FOREIGN KEY (player_key) REFERENCES players(key)
);

CREATE TABLE IF NOT EXISTS relics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_key TEXT NOT NULL,
    type TEXT NOT NULL,
    hex_id TEXT,
    found_at INTEGER NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    used_at INTEGER,
    target_post_id INTEGER,
    FOREIGN KEY (player_key) REFERENCES players(key)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, db_path: str | None = None):
        self._path = db_path or DB_PATH
        self._db: aiosqlite.Connection | None = None
        self._in_transaction = False

    async def connect(self) -> None:
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        await self._apply_migrations()
        await self._check_integrity()
        log.info("Database ready at %s", self._path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def _apply_migrations(self) -> None:
        migrations = [
            "ALTER TABLE players ADD COLUMN last_survey_lat REAL",
            "ALTER TABLE players ADD COLUMN last_survey_lon REAL",
            "ALTER TABLE players ADD COLUMN last_survey_at INTEGER",
            "ALTER TABLE survey_posts ADD COLUMN last_collected_at INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE survey_posts ADD COLUMN ruin_frozen_until INTEGER",
            "ALTER TABLE players ADD COLUMN momentum_tier INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE players ADD COLUMN last_survey_sender TEXT",
            """CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_key TEXT NOT NULL,
                action TEXT NOT NULL,
                summary TEXT NOT NULL,
                detail TEXT,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (player_key) REFERENCES players(key)
            )""",
            "ALTER TABLE players ADD COLUMN cooldown_override INTEGER",
            """CREATE TABLE IF NOT EXISTS known_nodes (
                key TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                last_seen INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS contracts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_key TEXT NOT NULL,
                objective TEXT NOT NULL,
                target INTEGER NOT NULL,
                cost INTEGER NOT NULL,
                reward_type TEXT NOT NULL,
                reward_amount INTEGER NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                purchased INTEGER NOT NULL DEFAULT 0,
                completed INTEGER NOT NULL DEFAULT 0,
                week_start INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (player_key) REFERENCES players(key)
            )""",
            """CREATE TABLE IF NOT EXISTS merchant_purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_key TEXT NOT NULL,
                purchase_type TEXT NOT NULL,
                week_start INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (player_key) REFERENCES players(key)
            )""",
            "ALTER TABLE players ADD COLUMN active_title TEXT",
            """CREATE TABLE IF NOT EXISTS mesh_repeaters (
                public_key TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                path_len INTEGER NOT NULL DEFAULT -1,
                updated_at INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS multiplayer_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS multiplayer_items (
                id TEXT PRIMARY KEY,
                item_type TEXT NOT NULL,
                assigned_at INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                installed_post_token TEXT,
                bundle_timestamp INTEGER
            )""",
            """CREATE TABLE IF NOT EXISTS multiplayer_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS multiplayer_scouts (
                target_player_id TEXT PRIMARY KEY,
                target_name TEXT NOT NULL,
                posts_json TEXT NOT NULL,
                scouted_at INTEGER NOT NULL,
                distance_mi INTEGER
            )""",
            """CREATE TABLE IF NOT EXISTS multiplayer_attacks (
                id TEXT PRIMARY KEY,
                direction TEXT NOT NULL,
                target_player TEXT,
                target_post_token TEXT,
                status TEXT NOT NULL,
                renown_committed INTEGER,
                travel_end_at INTEGER,
                resolved_at INTEGER,
                outcome TEXT,
                created_at INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS multiplayer_cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                ts INTEGER NOT NULL,
                data TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_events_id ON events(id)",
            # Warding: start timestamp of the current/last ward window. Paired
            # with ruin_frozen_until (the ward end) to exclude income during
            # dormancy. See ward_post / collect_passive_provisions.
            "ALTER TABLE survey_posts ADD COLUMN warded_at INTEGER",
            # Per-survey marks and the full reward-math breakdown (JSON), so the
            # Ledger can show an exact, historically-accurate calculation for
            # each survey entry.
            "ALTER TABLE surveys ADD COLUMN marks_earned INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE surveys ADD COLUMN reward_breakdown TEXT",
            # Supply Drops: one row per hourly bundle push, so the Outposts card
            # can show a short history of runs (surveys sent + items received)
            # even if the player wasn't watching the live feed when it landed.
            """CREATE TABLE IF NOT EXISTS supply_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ran_at INTEGER NOT NULL,
                survey_count INTEGER NOT NULL,
                drop_count INTEGER NOT NULL,
                drops_json TEXT
            )""",
            # 2026-07-22 cleanup: retire two dead columns. `players.field_notes`
            # was merged into provisions long ago; `hexes.ley_line` backed a
            # removed feature. DROP COLUMN needs SQLite 3.35+ and no-ops (caught
            # below) on a fresh DB where the column was never created. Nothing in
            # the codebase reads either one.
            "ALTER TABLE players DROP COLUMN field_notes",
            "ALTER TABLE hexes DROP COLUMN ley_line",
            # Fuzzed distance (miles) a scout revealed for this rival, rounded to
            # the nearest 50mi (NULL = unknown / not yet revealed). Distance is
            # secret on the leaderboard now and only learned by scouting.
            "ALTER TABLE multiplayer_scouts ADD COLUMN distance_mi INTEGER",
            # Snapshot (JSON) of the damage projection the player was shown when
            # they dispatched this raid. The projection is computed locally from
            # cached scout intel — the Worker never sees it — so the only way the
            # Frontlines card can show "you expected to raze" next to the actual
            # outcome is to keep our own copy. NULL for raids dispatched from
            # another install (or before this column existed).
            "ALTER TABLE multiplayer_attacks ADD COLUMN projection TEXT",
            # Contracts whose reward is a tier-IV combat munition can't be paid
            # out locally — the item is minted on the Worker's authoritative
            # store. This flag stays 0 from completion until the multiplayer
            # manager confirms the mint on the next sync, so the grant survives
            # an offline completion without ever being minted twice.
            "ALTER TABLE contracts ADD COLUMN reward_granted INTEGER NOT NULL DEFAULT 0",
            # Opaque per-post token — the post's identity everywhere outside this
            # install (Worker bundles, leaderboard, raids, scouts). An H3 hex id
            # decodes straight to coordinates, so the real hex_id must never
            # cross the trust boundary; this token carries no geography.
            "ALTER TABLE survey_posts ADD COLUMN mp_token TEXT",
            # These columns hold a post's opaque token, never an H3 hex — the old
            # "_hex" names were a leftover from before tokens existed and read as
            # if coordinates were stored. No-ops on a fresh install (the CREATE
            # TABLE statements above already use the new names) and on an install
            # that has already been renamed; both raise and are swallowed below.
            "ALTER TABLE multiplayer_items RENAME COLUMN installed_post_hex TO installed_post_token",
            "ALTER TABLE multiplayer_attacks RENAME COLUMN target_post_hex TO target_post_token",
            # The "community server" feature was never wired up — the client that
            # would have used these was removed before release. Dropped so a fresh
            # schema carries no columns nothing reads. No-ops where already absent.
            "ALTER TABLE players DROP COLUMN discord_linked",
            "ALTER TABLE players DROP COLUMN community_api_key",
            "ALTER TABLE players DROP COLUMN community_linked_at",
        ]
        for sql in migrations:
            try:
                await self._db.execute(sql)
            except Exception:
                pass
        await self._backfill_post_tokens()
        await self._db.execute(
            "UPDATE survey_posts SET last_collected_at = created_at WHERE last_collected_at = 0"
        )
        # Legacy pre-rework combat rows. 'traveling' predates the instant→travel
        # combat revision; a still-'in_flight' outgoing raid older than 48h can
        # only be a mirror the Worker resolved while we were offline (travel caps
        # at 12h) — clear it so the war room isn't wedged showing a ghost raid.
        await self._db.execute(
            "DELETE FROM multiplayer_attacks WHERE status = 'traveling'"
        )
        await self._db.execute(
            "DELETE FROM multiplayer_attacks "
            "WHERE status = 'in_flight' AND direction = 'outgoing' "
            "AND created_at < ?",
            (int(time.time()) - 48 * 3600,),
        )
        # Orphaned relic from the removed ley_line feature — never usable; drop it.
        await self._db.execute("DELETE FROM relics WHERE type = 'ley_line'")
        await self._db.commit()

    async def _backfill_post_tokens(self) -> None:
        """Assign a random mp_token to any post that predates the column.

        A post's token is its identity everywhere outside this install. It is
        always random: the real hex_id decodes straight to coordinates and must
        never cross the trust boundary.
        """
        async with self._db.execute(
            "SELECT id FROM survey_posts WHERE mp_token IS NULL"
        ) as cursor:
            ids = [r[0] for r in await cursor.fetchall()]
        if not ids:
            return

        for post_id in ids:
            await self._db.execute(
                "UPDATE survey_posts SET mp_token = ? WHERE id = ?",
                (secrets.token_hex(8), post_id),
            )
        await self._db.commit()

    async def _check_integrity(self) -> None:
        cursor = await self._db.execute("PRAGMA integrity_check")
        result = await cursor.fetchone()
        if result and result[0] != "ok":
            log.error("DATABASE INTEGRITY CHECK FAILED: %s", result[0])
        else:
            log.info("Database integrity check passed")

    async def get_or_create_player(
        self, key: str, home_lat: float, home_lon: float
    ) -> dict:
        row = await self._fetchone(
            "SELECT * FROM players WHERE key = ?", (key,)
        )
        if row:
            return dict(row)
        existing = await self._fetchone(
            "SELECT * FROM players ORDER BY created_at ASC LIMIT 1"
        )
        if existing and existing["key"] == "pending":
            await self._execute(
                "UPDATE players SET key = ? WHERE key = 'pending'", (key,)
            )
            # The web setup wizard awards "Staking Claim" against the
            # placeholder "pending" key before the real radio key is known
            # (see engine.set_home). Re-key that postcard along with the
            # player row, or it's orphaned under "pending" and the badge
            # reverts to unearned the moment the first survey arrives.
            await self._execute(
                "UPDATE postcards SET player_key = ? WHERE player_key = 'pending'",
                (key,),
            )
            result = dict(existing)
            result["key"] = key
            return result
        if existing:
            return dict(existing)
        now = int(time.time())
        await self._execute(
            """INSERT INTO players (key, home_lat, home_lon, created_at)
               VALUES (?, ?, ?, ?)""",
            (key, home_lat, home_lon, now),
        )
        return {
            "key": key, "xp": 0, "provisions": 0,
            "survey_marks": 0, "rank_level": 1, "base_camp_level": 1,
            "home_lat": home_lat, "home_lon": home_lon,
            "last_survey_lat": None, "last_survey_lon": None,
            "last_survey_at": None, "created_at": now,
        }

    async def _adjust(self, key: str, column: str, amount: int) -> int:
        await self._execute(
            f"UPDATE players SET {column} = {column} + ? WHERE key = ?",
            (amount, key),
        )
        row = await self._fetchone(
            f"SELECT {column} FROM players WHERE key = ?", (key,)
        )
        return row[column]

    async def add_xp(self, key: str, amount: int) -> int:
        return await self._adjust(key, "xp", amount)

    async def add_provisions(self, key: str, amount: int) -> int:
        return await self._adjust(key, "provisions", amount)

    async def add_survey_marks(self, key: str, amount: int) -> int:
        return await self._adjust(key, "survey_marks", amount)

    async def apply_survey_rewards(
        self, key: str, xp: int, provisions: int,
        survey_marks: int,
        lat: float = 0.0, lon: float = 0.0,
        sender_key: str | None = None,
    ) -> dict:
        now = int(time.time())
        await self._execute(
            """UPDATE players SET
               xp = xp + ?, provisions = provisions + ?,
               survey_marks = survey_marks + ?,
               last_survey_lat = ?, last_survey_lon = ?, last_survey_at = ?,
               last_survey_sender = ?
               WHERE key = ?""",
            (xp, provisions, survey_marks, lat, lon, now, sender_key, key),
        )
        row = await self._fetchone(
            "SELECT xp, provisions FROM players WHERE key = ?", (key,)
        )
        return {
            "xp": row["xp"],
            "provisions": row["provisions"],
        }

    async def is_hex_discovered(self, player_key: str, hex_id: str) -> bool:
        row = await self._fetchone(
            "SELECT 1 FROM hexes WHERE player_key = ? AND hex_id = ?",
            (player_key, hex_id),
        )
        return row is not None

    async def discover_hex(self, player_key: str, hex_id: str) -> None:
        await self._execute(
            "INSERT OR IGNORE INTO hexes (hex_id, player_key, discovered_at) VALUES (?, ?, ?)",
            (hex_id, player_key, int(time.time())),
        )

    async def count_discovered_hexes(self, player_key: str) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) as cnt FROM hexes WHERE player_key = ?",
            (player_key,),
        )
        return row["cnt"]

    async def count_surveys_today(self, player_key: str) -> int:
        # A Vigor Tonic sets cooldown_override to grant a fresh survey day; honor
        # it here so the tonic also re-arms the First Survey of the Day bonus
        # (mirrors was_hex_surveyed_today / get_all_hexes cooldown handling).
        day_start = self._day_start()
        player = await self._fetchone(
            "SELECT cooldown_override FROM players WHERE key = ?", (player_key,),
        )
        cutoff = day_start
        if player and player["cooldown_override"] and player["cooldown_override"] >= day_start:
            cutoff = player["cooldown_override"]
        row = await self._fetchone(
            "SELECT COUNT(*) as cnt FROM surveys WHERE player_key = ? AND surveyed_at >= ?",
            (player_key, cutoff),
        )
        return row["cnt"]

    async def was_hex_surveyed_today(self, player_key: str, hex_id: str) -> bool:
        day_start = self._day_start()
        player = await self._fetchone(
            "SELECT cooldown_override FROM players WHERE key = ?", (player_key,),
        )
        cutoff = day_start
        if player and player["cooldown_override"] and player["cooldown_override"] >= day_start:
            cutoff = player["cooldown_override"]
        row = await self._fetchone(
            """SELECT 1 FROM surveys
               WHERE player_key = ? AND hex_id = ? AND surveyed_at >= ?""",
            (player_key, hex_id, cutoff),
        )
        return row is not None

    async def record_survey(
        self,
        player_key: str,
        hex_id: str,
        lat: float,
        lon: float,
        distance_miles: float,
        snr: float | None,
        rssi: int | None,
        hops: int | None,
        xp_earned: int,
        provisions_earned: int,
        field_notes_earned: int,
        is_discovery: bool,
        marks_earned: int = 0,
        reward_breakdown: str | None = None,
    ) -> None:
        await self._execute(
            """INSERT INTO surveys
               (player_key, hex_id, lat, lon, distance_miles, snr, rssi, hops,
                xp_earned, provisions_earned, field_notes_earned, is_discovery,
                marks_earned, reward_breakdown, surveyed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                player_key, hex_id, lat, lon, distance_miles, snr, rssi, hops,
                xp_earned, provisions_earned, field_notes_earned,
                1 if is_discovery else 0, marks_earned, reward_breakdown,
                int(time.time()),
            ),
        )

    async def get_post_in_hex(self, player_key: str, hex_id: str) -> dict | None:
        row = await self._fetchone(
            "SELECT * FROM survey_posts WHERE player_key = ? AND hex_id = ?",
            (player_key, hex_id),
        )
        return dict(row) if row else None

    async def get_any_post_in_hex(self, hex_id: str) -> dict | None:
        row = await self._fetchone(
            "SELECT * FROM survey_posts WHERE hex_id = ?",
            (hex_id,),
        )
        return dict(row) if row else None

    async def get_post_by_worker_ref(self, ref: str) -> dict | None:
        """Resolve a Worker-side post reference (mp_token) to the local post."""
        row = await self._fetchone(
            "SELECT * FROM survey_posts WHERE mp_token = ?",
            (ref,),
        )
        return dict(row) if row else None

    async def count_player_posts(self, player_key: str) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) as cnt FROM survey_posts WHERE player_key = ?",
            (player_key,),
        )
        return row["cnt"]

    async def create_post(
        self, player_key: str, hex_id: str, name: str
    ) -> dict:
        now = int(time.time())
        # mp_token is the post's identity outside this install (Worker,
        # leaderboard, raids); random so it reveals nothing about the hex.
        await self._execute(
            """INSERT INTO survey_posts
               (player_key, hex_id, name, level, last_tended_at, last_collected_at, created_at, mp_token)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?)""",
            (player_key, hex_id, name, now, now, now, secrets.token_hex(8)),
        )
        return dict(
            await self._fetchone(
                "SELECT * FROM survey_posts WHERE player_key = ? AND hex_id = ?",
                (player_key, hex_id),
            )
        )

    async def tend_post(self, post_id: int, bonus_days: int = 0) -> None:
        ts = int(time.time()) + bonus_days * 86400
        await self._execute(
            "UPDATE survey_posts SET last_tended_at = ? WHERE id = ?",
            (ts, post_id),
        )

    async def deduct_provisions(self, key: str, amount: int) -> int:
        return await self._adjust(key, "provisions", -amount)

    async def deduct_survey_marks(self, key: str, amount: int) -> int:
        return await self._adjust(key, "survey_marks", -amount)

    async def get_player(self, key: str) -> dict | None:
        row = await self._fetchone(
            "SELECT * FROM players WHERE key = ?", (key,)
        )
        return dict(row) if row else None

    async def get_first_player(self) -> dict | None:
        row = await self._fetchone(
            "SELECT * FROM players ORDER BY created_at ASC LIMIT 1"
        )
        return dict(row) if row else None

    async def get_recent_surveys(
        self, player_key: str, limit: int = 10
    ) -> list[dict]:
        rows = await self._fetchall(
            """SELECT hex_id, distance_miles, snr, rssi, hops, xp_earned,
                      provisions_earned, field_notes_earned, is_discovery,
                      marks_earned, reward_breakdown, surveyed_at
               FROM surveys WHERE player_key = ?
               ORDER BY surveyed_at DESC, id DESC LIMIT ?""",
            (player_key, limit),
        )
        return [dict(r) for r in rows]

    async def fetch_surveys_since(
        self, player_key: str, since_timestamp: int
    ) -> list[dict]:
        rows = await self._fetchall(
            """SELECT hex_id, distance_miles, xp_earned,
                      provisions_earned, field_notes_earned, is_discovery, surveyed_at
               FROM surveys WHERE player_key = ? AND surveyed_at > ?
               ORDER BY surveyed_at ASC""",
            (player_key, since_timestamp),
        )
        return [dict(r) for r in rows]

    async def count_surveys_since(self, player_key: str, since_timestamp: int) -> int:
        """How many surveys are queued for the next Supply Drop bundle — i.e.
        logged since the last push. Lighter than fetching the rows."""
        row = await self._fetchone(
            "SELECT COUNT(*) as cnt FROM surveys WHERE player_key = ? AND surveyed_at > ?",
            (player_key, since_timestamp),
        )
        return row["cnt"]

    async def rollback_survey_rewards(
        self, key: str, xp: int, provisions: int,
        survey_marks: int,
    ) -> None:
        await self._execute(
            """UPDATE players SET
               xp = MAX(0, xp - ?), provisions = MAX(0, provisions - ?),
               survey_marks = MAX(0, survey_marks - ?)
               WHERE key = ?""",
            (xp, provisions, survey_marks, key),
        )

    async def get_survey(self, survey_id: int) -> dict | None:
        row = await self._fetchone(
            "SELECT * FROM surveys WHERE id = ?", (survey_id,),
        )
        return dict(row) if row else None

    async def delete_survey(self, survey_id: int) -> None:
        await self._execute("DELETE FROM surveys WHERE id = ?", (survey_id,))

    async def get_hex_survey_count(self, player_key: str, hex_id: str) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) as cnt FROM surveys WHERE player_key = ? AND hex_id = ?",
            (player_key, hex_id),
        )
        return row["cnt"]

    async def delete_hex_discovery(self, player_key: str, hex_id: str) -> None:
        await self._execute(
            "DELETE FROM hexes WHERE player_key = ? AND hex_id = ?",
            (player_key, hex_id),
        )

    async def revoke_postcards_near(self, player_key: str, surveyed_at: int, window: int = 5) -> int:
        result = await self._execute(
            "DELETE FROM postcards WHERE player_key = ? AND earned_at BETWEEN ? AND ?",
            (player_key, surveyed_at, surveyed_at + window),
        )
        return result.rowcount

    async def set_rank(self, key: str, rank: int) -> None:
        await self._execute(
            "UPDATE players SET rank_level = ? WHERE key = ?", (rank, key),
        )

    async def get_total_distance(self, player_key: str) -> float:
        row = await self._fetchone(
            "SELECT COALESCE(SUM(distance_miles), 0) as total FROM surveys WHERE player_key = ?",
            (player_key,),
        )
        return row["total"]

    async def get_post_by_id(self, post_id: int) -> dict | None:
        row = await self._fetchone(
            "SELECT * FROM survey_posts WHERE id = ?", (post_id,)
        )
        return dict(row) if row else None

    async def rename_post(self, post_id: int, name: str) -> None:
        await self._execute(
            "UPDATE survey_posts SET name = ? WHERE id = ?", (name, post_id),
        )

    async def delete_post(self, post_id: int) -> None:
        """Remove a Survey Post. Used to reconcile a PvP raze — the Worker
        permanently destroyed the outpost, so the local record must follow."""
        await self._execute("DELETE FROM survey_posts WHERE id = ?", (post_id,))

    async def set_post_level(self, post_id: int, level: int) -> None:
        """Set a post's level outright (no cost). Used to reconcile a PvP
        level-loss knockdown to the Worker's authoritative level."""
        await self._execute(
            "UPDATE survey_posts SET level = ? WHERE id = ?", (level, post_id),
        )

    async def get_all_posts(self, player_key: str) -> list[dict]:
        rows = await self._fetchall(
            """SELECT sp.*
               FROM survey_posts sp
               WHERE sp.player_key = ?""",
            (player_key,),
        )
        return [dict(r) for r in rows]

    async def get_max_distance(self, player_key: str) -> float:
        row = await self._fetchone(
            "SELECT MAX(distance_miles) as max_dist FROM surveys WHERE player_key = ?",
            (player_key,),
        )
        return row["max_dist"] or 0.0

    async def get_furthest_survey(self, player_key: str) -> dict | None:
        row = await self._fetchone(
            "SELECT id, lat, lon, distance_miles FROM surveys WHERE player_key = ? ORDER BY distance_miles DESC LIMIT 1",
            (player_key,),
        )
        if row and row["distance_miles"]:
            return {"id": row["id"], "lat": row["lat"], "lon": row["lon"], "distance_miles": row["distance_miles"]}
        return None

    async def get_survey_streak(self, player_key: str) -> int:
        day_start = self._day_start()
        day_len = 86400
        streak = 0
        check_day = day_start
        while True:
            row = await self._fetchone(
                """SELECT 1 FROM surveys
                   WHERE player_key = ? AND surveyed_at >= ? AND surveyed_at < ?""",
                (player_key, check_day, check_day + day_len),
            )
            if row is None:
                break
            streak += 1
            check_day -= day_len
        return streak

    async def update_momentum_tier(self, player_key: str) -> int:
        """Advance the daily-streak momentum tier. Called on each survey, BEFORE
        the current survey is recorded. Derives the streak from the `surveys`
        table (the authoritative record) rather than the mutable `last_survey_at`
        column, which is shared with the velocity check and written in a separate
        transaction — reading it here made momentum spuriously decay when it
        lagged. Semantics unchanged: +1 tier per consecutive survey day (cap 5),
        -1 per missed day, one change per UTC day."""
        player = await self.get_player(player_key)
        if not player:
            return 0
        current_tier = player.get("momentum_tier", 0)
        day_start = self._day_start()

        # Only the first survey of the day moves momentum.
        already_today = await self._fetchone(
            "SELECT 1 FROM surveys WHERE player_key = ? AND surveyed_at >= ? LIMIT 1",
            (player_key, day_start),
        )
        if already_today:
            return current_tier

        # Most recent day (before today) the player actually surveyed.
        row = await self._fetchone(
            "SELECT MAX(surveyed_at) AS last FROM surveys WHERE player_key = ? AND surveyed_at < ?",
            (player_key, day_start),
        )
        last_survey = row["last"] if row else None

        if not last_survey:
            new_tier = 1
        else:
            last_survey_day = last_survey - (last_survey % 86400)
            days_gap = (day_start - last_survey_day) // 86400
            if days_gap <= 1:
                new_tier = min(current_tier + 1, 5)
            else:
                new_tier = max(0, current_tier - (days_gap - 1))

        await self._execute(
            "UPDATE players SET momentum_tier = ? WHERE key = ?",
            (new_tier, player_key),
        )
        return new_tier

    async def get_all_hexes(self, player_key: str) -> list[dict]:
        day_start = self._day_start()
        player = await self._fetchone(
            "SELECT cooldown_override FROM players WHERE key = ?", (player_key,),
        )
        cutoff = day_start
        if player and player["cooldown_override"] and player["cooldown_override"] >= day_start:
            cutoff = player["cooldown_override"]
        rows = await self._fetchall(
            """SELECT h.hex_id, h.discovered_at,
                      COUNT(s.id) as survey_count,
                      sp.name as post_name, sp.level as post_level, sp.id as post_id,
                      ls.lat as last_survey_lat, ls.lon as last_survey_lon, ls.survey_id, ls.distance_miles, ls.surveyed_at,
                      EXISTS(
                          SELECT 1 FROM surveys s2
                          WHERE s2.hex_id = h.hex_id
                            AND s2.player_key = h.player_key
                            AND s2.surveyed_at >= ?
                      ) as on_cooldown
               FROM hexes h
               LEFT JOIN surveys s ON s.hex_id = h.hex_id AND s.player_key = h.player_key
               LEFT JOIN survey_posts sp ON sp.hex_id = h.hex_id AND sp.player_key = h.player_key
               LEFT JOIN (
                   SELECT hex_id, player_key, lat, lon, id as survey_id, distance_miles, surveyed_at
                   FROM surveys
                   WHERE id IN (
                       SELECT MAX(id) FROM surveys
                       WHERE player_key = ?
                       GROUP BY hex_id
                   )
               ) ls ON ls.hex_id = h.hex_id AND ls.player_key = h.player_key
               WHERE h.player_key = ?
               GROUP BY h.hex_id
               ORDER BY h.discovered_at DESC""",
            (cutoff, player_key, player_key),
        )
        return [dict(r) for r in rows]

    async def get_all_postcards(self, player_key: str) -> list[dict]:
        rows = await self._fetchall(
            "SELECT * FROM postcards WHERE player_key = ? ORDER BY earned_at DESC",
            (player_key,),
        )
        return [dict(r) for r in rows]

    async def get_postcards_by_class(
        self, player_key: str, pc_class: str
    ) -> list[dict]:
        rows = await self._fetchall(
            "SELECT * FROM postcards WHERE player_key = ? AND class = ?",
            (player_key, pc_class),
        )
        return [dict(r) for r in rows]

    async def award_postcard(
        self, player_key: str, pc_class: str, stars: int,
        description: str, distance: float | None, snr: float | None,
    ) -> dict:
        now = int(time.time())
        await self._execute(
            """INSERT INTO postcards
               (player_key, class, stars, description, distance_miles, snr, earned_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (player_key, pc_class, stars, description, distance, snr, now),
        )
        return {
            "class": pc_class, "stars": stars,
            "description": description, "earned_at": now,
        }

    async def rank_up_with_reward(
        self, key: str, new_rank: int, prov_reward: int
    ) -> None:
        await self._execute(
            """UPDATE players SET
               rank_level = ?, provisions = provisions + ?
               WHERE key = ?""",
            (new_rank, prov_reward, key),
        )

    async def upgrade_base_camp(
        self, key: str, new_level: int, prov_cost: int
    ) -> None:
        await self._execute(
            """UPDATE players SET
               base_camp_level = ?, provisions = provisions - ?
               WHERE key = ?""",
            (new_level, prov_cost, key),
        )

    async def upgrade_post(
        self, post_id: int, new_level: int, prov_cost: int, player_key: str
    ) -> None:
        await self._execute(
            "UPDATE survey_posts SET level = ? WHERE id = ?",
            (new_level, post_id),
        )
        await self._execute(
            "UPDATE players SET provisions = provisions - ? WHERE key = ?",
            (prov_cost, player_key),
        )

    async def collect_passive_provisions(
        self, player_key: str, amount: int, post_ids: list[int]
    ) -> None:
        now = int(time.time())
        await self._execute(
            "UPDATE players SET provisions = provisions + ? WHERE key = ?",
            (amount, player_key),
        )
        for pid in post_ids:
            await self._execute(
                "UPDATE survey_posts SET last_collected_at = ? WHERE id = ?",
                (now, pid),
            )

    # --- Relics ---

    async def add_relic(
        self, player_key: str, relic_type: str, hex_id: str,
    ) -> dict:
        now = int(time.time())
        await self._execute(
            """INSERT INTO relics (player_key, type, hex_id, found_at)
               VALUES (?, ?, ?, ?)""",
            (player_key, relic_type, hex_id, now),
        )
        return {"type": relic_type, "hex_id": hex_id, "found_at": now}

    async def get_unused_relics(self, player_key: str) -> list[dict]:
        rows = await self._fetchall(
            "SELECT * FROM relics WHERE player_key = ? AND used = 0 ORDER BY found_at DESC",
            (player_key,),
        )
        return [dict(r) for r in rows]

    async def get_discovered_relic_types(self, player_key: str) -> set[str]:
        rows = await self._fetchall(
            "SELECT DISTINCT type FROM relics WHERE player_key = ?",
            (player_key,),
        )
        return {r["type"] for r in rows}

    async def count_recent_relics_by_type(
        self, player_key: str, since: int,
    ) -> dict[str, int]:
        """Count still-held relics found since ``since``, grouped by type.

        Drives per-type diminishing returns on drops. Only unused relics count:
        opening a Buried Cache (or spending a Wardstone/Vigor Tonic) should not
        keep suppressing future drops — the anti-farm targets hoarding fresh
        finds, not consuming them.
        """
        rows = await self._fetchall(
            "SELECT type, COUNT(*) as cnt FROM relics "
            "WHERE player_key = ? AND found_at >= ? AND used = 0 GROUP BY type",
            (player_key, since),
        )
        return {r["type"]: r["cnt"] for r in rows}

    async def get_survey_count(self, player_key: str) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) as cnt FROM surveys WHERE player_key = ?",
            (player_key,),
        )
        return row["cnt"]

    async def count_relics(self, player_key: str) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) as cnt FROM relics WHERE player_key = ?",
            (player_key,),
        )
        return row["cnt"]

    async def use_buried_cache(self, relic_id: int, player_key: str, amount: int) -> bool:
        relic = await self._fetchone(
            "SELECT * FROM relics WHERE id = ? AND player_key = ? AND used = 0 AND type = 'buried_cache'",
            (relic_id, player_key),
        )
        if not relic:
            return False
        now = int(time.time())
        async with self.transaction():
            await self._execute(
                "UPDATE relics SET used = 1, used_at = ? WHERE id = ?",
                (now, relic_id),
            )
            await self._execute(
                "UPDATE players SET provisions = provisions + ? WHERE key = ?",
                (amount, player_key),
            )
        return True

    async def salvage_relic(
        self, relic_id: int, player_key: str, value_by_type: dict[str, int],
    ) -> int | None:
        """Break down an unused relic for provisions. Returns the provisions
        granted, or None if the relic is missing/already used, or its type isn't
        salvageable (not present in value_by_type)."""
        relic = await self._fetchone(
            "SELECT * FROM relics WHERE id = ? AND player_key = ? AND used = 0",
            (relic_id, player_key),
        )
        if not relic or relic["type"] not in value_by_type:
            return None
        amount = value_by_type[relic["type"]]
        now = int(time.time())
        async with self.transaction():
            await self._execute(
                "UPDATE relics SET used = 1, used_at = ? WHERE id = ?",
                (now, relic_id),
            )
            await self._execute(
                "UPDATE players SET provisions = provisions + ? WHERE key = ?",
                (amount, player_key),
            )
        return amount

    async def ward_post(self, relic_id: int, post_id: int, player_key: str, duration: int) -> bool:
        """Consume a wardstone to put an outpost dormant for `duration` seconds.

        Warded posts freeze ruin, earn no income, and cannot be raided. The
        ward window is [warded_at, ruin_frozen_until]; income exclusion keys off
        both (see engine._ward_overlap).
        """
        relic = await self._fetchone(
            "SELECT * FROM relics WHERE id = ? AND player_key = ? AND used = 0 AND type = 'wardstone'",
            (relic_id, player_key),
        )
        if not relic:
            return False
        post = await self._fetchone(
            "SELECT * FROM survey_posts WHERE id = ? AND player_key = ?",
            (post_id, player_key),
        )
        if not post:
            return False
        now = int(time.time())
        warded_until = now + duration
        async with self.transaction():
            await self._execute(
                "UPDATE relics SET used = 1, used_at = ?, target_post_id = ? WHERE id = ?",
                (now, post_id, relic_id),
            )
            await self._execute(
                "UPDATE survey_posts SET warded_at = ?, ruin_frozen_until = ? WHERE id = ?",
                (now, warded_until, post_id),
            )
        return True

    async def use_vigor_tonic(self, relic_id: int, player_key: str) -> bool:
        relic = await self._fetchone(
            "SELECT * FROM relics WHERE id = ? AND player_key = ? AND used = 0 AND type = 'vigor_tonic'",
            (relic_id, player_key),
        )
        if not relic:
            return False
        now = int(time.time())
        async with self.transaction():
            await self._execute(
                "UPDATE relics SET used = 1, used_at = ? WHERE id = ?",
                (now, relic_id),
            )
            await self._execute(
                "UPDATE players SET cooldown_override = ? WHERE key = ?",
                (now, player_key),
            )
        return True

    async def log_activity(self, player_key: str, action: str, summary: str, detail: str | None = None) -> None:
        now = int(time.time())
        await self._execute(
            "INSERT INTO activity_log (player_key, action, summary, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (player_key, action, summary, detail, now),
        )
        # Keep only the most recent entries per player so the table can't grow
        # unbounded (mirrors the supply_runs cap).
        await self._execute(
            "DELETE FROM activity_log WHERE player_key = ? AND id NOT IN "
            "(SELECT id FROM activity_log WHERE player_key = ? ORDER BY created_at DESC LIMIT 100)",
            (player_key, player_key),
        )

    # --- Supply Drops (hourly bundle history) ---

    async def log_supply_run(
        self, ran_at: int, survey_count: int, drops: list[str],
    ) -> None:
        """Record one bundle push for the Outposts Supply Drops log. `drops` is
        the list of item types received. Keeps only the most recent runs so the
        table can't grow unbounded."""
        await self._execute(
            "INSERT INTO supply_runs (ran_at, survey_count, drop_count, drops_json) "
            "VALUES (?, ?, ?, ?)",
            (ran_at, survey_count, len(drops), json.dumps(drops)),
        )
        await self._execute(
            "DELETE FROM supply_runs WHERE id NOT IN "
            "(SELECT id FROM supply_runs ORDER BY ran_at DESC LIMIT 50)"
        )

    async def get_recent_supply_runs(self, limit: int = 5) -> list[dict]:
        rows = await self._fetchall(
            "SELECT ran_at, survey_count, drop_count, drops_json "
            "FROM supply_runs ORDER BY ran_at DESC LIMIT ?",
            (limit,),
        )
        runs = []
        for r in rows:
            run = dict(r)
            try:
                run["drops"] = json.loads(run.pop("drops_json") or "[]")
            except (ValueError, TypeError):
                run["drops"] = []
            runs.append(run)
        return runs

    # --- Settings ---

    async def get_setting(self, key: str) -> str | None:
        row = await self._fetchone(
            "SELECT value FROM settings WHERE key = ?", (key,)
        )
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self._execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )

    async def delete_setting(self, key: str) -> None:
        await self._execute("DELETE FROM settings WHERE key = ?", (key,))

    async def get_oidc_config(self) -> dict | None:
        row = await self._fetchone(
            "SELECT value FROM settings WHERE key = 'oidc_config'"
        )
        if not row:
            return None
        import json
        return json.loads(row["value"])

    async def save_oidc_config(self, config: dict | None) -> None:
        import json
        if config is None:
            await self._execute(
                "DELETE FROM settings WHERE key IN ('oidc_config', 'oidc_sub')"
            )
        else:
            await self._execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('oidc_config', ?)",
                (json.dumps(config),),
            )

    async def get_companion_config(self) -> dict | None:
        row = await self._fetchone(
            "SELECT value FROM settings WHERE key = 'companion_config'"
        )
        if not row:
            return None
        import json
        return json.loads(row["value"])

    async def save_companion_config(self, config: dict) -> None:
        import json
        await self._execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('companion_config', ?)",
            (json.dumps(config),),
        )

    async def upsert_known_node(self, key: str, name: str) -> None:
        now = int(time.time())
        await self._execute(
            "INSERT INTO known_nodes (key, name, last_seen) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET name = ?, last_seen = ?",
            (key, name, now, name, now),
        )

    async def rename_known_node(self, key: str, name: str) -> None:
        """Update a known node's display name without touching last_seen.

        Used to reconcile stored spyglass names against the companion's
        contact list (e.g. after the operator renames the node over BLE),
        where we haven't actually heard from the node."""
        await self._execute(
            "UPDATE known_nodes SET name = ? WHERE key = ?",
            (name, key),
        )

    async def get_known_nodes(self) -> list[dict]:
        rows = await self._fetchall(
            "SELECT key, name, last_seen FROM known_nodes ORDER BY name"
        )
        return [dict(r) for r in rows]

    @asynccontextmanager
    async def transaction(self):
        self._in_transaction = True
        try:
            yield
            await self._db.commit()
        except BaseException:
            await self._db.rollback()
            raise
        finally:
            self._in_transaction = False

    # --- Contracts ---

    async def get_current_contracts(self, player_key: str, week_start: int) -> list[dict]:
        rows = await self._fetchall(
            "SELECT * FROM contracts WHERE player_key = ? AND week_start = ? ORDER BY id",
            (player_key, week_start),
        )
        return [dict(r) for r in rows]

    async def create_contract(
        self, player_key: str, objective: str, target: int,
        cost: int, reward_type: str, reward_amount: int, week_start: int,
    ) -> dict:
        now = int(time.time())
        await self._execute(
            """INSERT INTO contracts (player_key, objective, target, cost,
               reward_type, reward_amount, week_start, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (player_key, objective, target, cost, reward_type, reward_amount, week_start, now),
        )
        row = await self._fetchone(
            "SELECT * FROM contracts WHERE player_key = ? ORDER BY id DESC LIMIT 1",
            (player_key,),
        )
        return dict(row)

    async def purchase_contract(self, contract_id: int, player_key: str) -> bool:
        row = await self._fetchone(
            "SELECT * FROM contracts WHERE id = ? AND player_key = ?",
            (contract_id, player_key),
        )
        if not row or row["purchased"]:
            return False
        player = await self._fetchone("SELECT provisions FROM players WHERE key = ?", (player_key,))
        if not player or player["provisions"] < row["cost"]:
            return False
        await self._execute(
            "UPDATE players SET provisions = provisions - ? WHERE key = ?",
            (row["cost"], player_key),
        )
        await self._execute(
            "UPDATE contracts SET purchased = 1 WHERE id = ?", (contract_id,),
        )
        return True

    async def update_contract_progress(self, contract_id: int, progress: int) -> None:
        await self._execute(
            "UPDATE contracts SET progress = ? WHERE id = ?", (progress, contract_id),
        )

    async def complete_contract(self, contract_id: int, player_key: str) -> dict | None:
        row = await self._fetchone(
            "SELECT * FROM contracts WHERE id = ? AND player_key = ? AND purchased = 1 AND completed = 0",
            (contract_id, player_key),
        )
        if not row:
            return None
        contract = dict(row)
        await self._execute("UPDATE contracts SET completed = 1 WHERE id = ?", (contract_id,))
        rt = contract["reward_type"]
        amt = contract["reward_amount"]
        if rt == "provisions":
            await self._execute(
                "UPDATE players SET provisions = provisions + ? WHERE key = ?", (amt, player_key),
            )
        elif rt == "survey_marks":
            await self._execute(
                "UPDATE players SET survey_marks = survey_marks + ? WHERE key = ?", (amt, player_key),
            )
        return contract

    async def get_pending_contract_item_grants(
        self, player_key: str, item_types: tuple[str, ...],
    ) -> list[dict]:
        """Completed contracts whose tier-IV munition reward has not yet been
        minted on the Worker (reward_granted = 0). The manager drains these on
        the next sync."""
        if not item_types:
            return []
        placeholders = ",".join("?" for _ in item_types)
        rows = await self._fetchall(
            f"""SELECT * FROM contracts
                WHERE player_key = ? AND completed = 1 AND reward_granted = 0
                AND reward_type IN ({placeholders})
                ORDER BY id""",
            (player_key, *item_types),
        )
        return [dict(r) for r in rows]

    async def mark_contract_reward_granted(self, contract_id: int) -> None:
        await self._execute(
            "UPDATE contracts SET reward_granted = 1 WHERE id = ?", (contract_id,),
        )

    async def get_merchant_purchases(self, player_key: str, week_start: int) -> list[str]:
        rows = await self._fetchall(
            "SELECT purchase_type FROM merchant_purchases WHERE player_key = ? AND week_start = ?",
            (player_key, week_start),
        )
        return [r["purchase_type"] for r in rows]

    async def add_merchant_purchase(self, player_key: str, purchase_type: str, week_start: int) -> None:
        await self._execute(
            "INSERT INTO merchant_purchases (player_key, purchase_type, week_start, created_at) VALUES (?, ?, ?, ?)",
            (player_key, purchase_type, week_start, int(time.time())),
        )

    async def set_active_title(self, player_key: str, title: str | None) -> None:
        await self._execute(
            "UPDATE players SET active_title = ? WHERE key = ?",
            (title, player_key),
        )

    async def replace_mesh_repeaters(self, repeaters: list[dict]) -> None:
        await self._db.execute("DELETE FROM mesh_repeaters")
        for r in repeaters:
            await self._db.execute(
                """INSERT INTO mesh_repeaters (public_key, name, lat, lon, path_len, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (r["public_key"], r["name"], r["lat"], r["lon"], r["path_len"], r["updated_at"]),
            )
        await self._db.commit()

    async def get_mesh_repeaters(self) -> list[dict]:
        rows = await self._fetchall("SELECT * FROM mesh_repeaters")
        return [dict(r) for r in rows]

    EVENT_MAX_ROWS = 500

    async def insert_event(
        self, event_type: str, ts: int, data: str, event_id: int | None = None
    ) -> int:
        # An explicit event_id lets the engine assign a client-facing id
        # synchronously (before this async insert lands) and persist that same id.
        if event_id is not None:
            cursor = await self._db.execute(
                "INSERT INTO events (id, type, ts, data) VALUES (?, ?, ?, ?)",
                (event_id, event_type, ts, data),
            )
        else:
            cursor = await self._db.execute(
                "INSERT INTO events (type, ts, data) VALUES (?, ?, ?)",
                (event_type, ts, data),
            )
        row_id = event_id if event_id is not None else cursor.lastrowid
        if not self._in_transaction:
            await self._db.commit()
        return row_id

    async def get_max_event_id(self) -> int:
        row = await self._fetchone("SELECT MAX(id) as m FROM events")
        return (row["m"] if row and row["m"] is not None else 0)

    async def get_events_since(self, last_id: int, limit: int = 50) -> list[dict]:
        rows = await self._fetchall(
            "SELECT id, type, ts, data FROM events WHERE id > ? ORDER BY id ASC LIMIT ?",
            (last_id, limit),
        )
        return [dict(r) for r in rows]

    async def get_recent_events(
        self, limit: int = 20, min_ts: int | None = None
    ) -> list[dict]:
        if min_ts is not None:
            rows = await self._fetchall(
                "SELECT id, type, ts, data FROM events WHERE ts >= ? "
                "ORDER BY id DESC LIMIT ?",
                (min_ts, limit),
            )
        else:
            rows = await self._fetchall(
                "SELECT id, type, ts, data FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        return [dict(r) for r in reversed(rows)]

    async def prune_events(self) -> None:
        count = await self._fetchone("SELECT COUNT(*) as c FROM events")
        if count and count["c"] > self.EVENT_MAX_ROWS:
            cutoff = await self._fetchone(
                "SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?",
                (self.EVENT_MAX_ROWS,),
            )
            if cutoff:
                await self._execute(
                    "DELETE FROM events WHERE id <= ?", (cutoff["id"],)
                )

    def _day_start(self) -> int:
        now = datetime.now(timezone.utc)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(midnight.timestamp())

    async def _execute(self, sql: str, params: tuple = ()):
        cursor = await self._db.execute(sql, params)
        if not self._in_transaction:
            await self._db.commit()
        return cursor

    async def _fetchone(self, sql: str, params: tuple = ()):
        cursor = await self._db.execute(sql, params)
        return await cursor.fetchone()

    async def _fetchall(self, sql: str, params: tuple = ()):
        cursor = await self._db.execute(sql, params)
        return await cursor.fetchall()
