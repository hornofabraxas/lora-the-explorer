import json
import time
import pytest
import pytest_asyncio
from unittest.mock import patch
from lora_explorer.radio.adapter import RadioAdapter, IncomingMessage, MessageHandler, PositionResult, PositionFailure
from lora_explorer.game.engine import GameEngine, CHARTER_PROVISION_COST, CHARTER_MARK_COST, get_daily_dispatch, generate_analysts_report
from lora_explorer.game.database import Database


# ~5 miles from base camp (40.0, -105.0) — past the 3-mile charter minimum
FAR_POSITION = (40.07, -104.93)
# ~1 mile from base camp — inside the charter minimum
NEAR_POSITION = (40.01, -105.0)


class MockRadioAdapter(RadioAdapter):
    def __init__(self):
        self.handler: MessageHandler | None = None
        self.sent_messages: list[tuple[str, str]] = []
        self.connected = False
        self.mock_position: tuple[float, float] | None = FAR_POSITION
        self.engine: "GameEngine | None" = None
        self.contacts: dict = {}

    def get_contacts(self) -> dict:
        return dict(self.contacts)

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def send_message(self, recipient_key: str, text: str) -> bool:
        self.sent_messages.append((recipient_key, text))
        return True

    async def set_message_handler(self, handler: MessageHandler) -> None:
        self.handler = handler

    async def request_position(self, node_key: str, progress_callback=None) -> PositionResult:
        if self.mock_position is None:
            return PositionResult(failure=PositionFailure.TIMEOUT)
        return PositionResult(position=self.mock_position)

    async def simulate_message(
        self,
        text: str,
        sender: str = "abc123",
        snr: float = -8.0,
        rssi: int = -105,
        hops: int = 2,
    ) -> str | None:
        msg = IncomingMessage(
            sender_key=sender,
            text=text,
            snr=snr,
            rssi=rssi,
            hops=hops,
        )
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

    async def await_survey(self) -> None:
        """Wait for any in-flight backgrounded command task to complete."""
        if self.engine and self.engine._command_task:
            await self.engine._command_task


@pytest.fixture
def adapter():
    return MockRadioAdapter()


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(db_path=str(tmp_path / "test.db"))
    yield database
    await database.close()


@pytest_asyncio.fixture
async def engine(adapter, db):
    e = GameEngine(
        adapter=adapter,
        home_lat=40.0,
        home_lon=-105.0,
        db=db,
    )
    adapter.engine = e
    await e.start()
    yield e
    await e.stop()


async def _give_resources(db, key, provisions=100, marks=10, camp_level=3, rank_level=8):
    await db.get_or_create_player(key, 40.0, -105.0)
    await db.add_provisions(key, provisions)
    await db.add_survey_marks(key, marks)
    await db._execute(
        "UPDATE players SET base_camp_level = ?, rank_level = ? WHERE key = ?",
        (camp_level, rank_level, key),
    )


# ── Survey tests ──

@pytest.mark.asyncio
async def test_survey_returns_xp_and_provisions(adapter, engine):
    response = await adapter.simulate_message("/lora survey")
    assert "SURVEY LOGGED" in response
    assert "mi" in response
    assert "xp" in response
    assert "prov" in response


@pytest.mark.asyncio
async def test_survey_first_hex_is_discovery(adapter, engine):
    response = await adapter.simulate_message("/lora survey")
    assert "NEW TERRITORY" in response
    assert "#1 discovered" in response


@pytest.mark.asyncio
async def test_survey_second_hex_is_also_discovery(adapter, engine):
    base_time = time.time()
    with patch("lora_explorer.game.database.time") as mock_time, \
         patch("lora_explorer.game.engine.time") as mock_etime:
        mock_time.time.return_value = base_time
        mock_etime.time.return_value = base_time
        await adapter.simulate_message("/lora survey")
        adapter.mock_position = (40.02, -105.01)
        mock_time.time.return_value = base_time + 3600
        mock_etime.time.return_value = base_time + 3600
        response = await adapter.simulate_message("/lora survey")
    assert "NEW TERRITORY" in response
    assert "#2 discovered" in response


async def _clear_survey_cooldown(engine):
    """Advance last_survey_at past the uniform SURVEY_MIN_INTERVAL_S so a
    follow-up survey in the same test isn't rejected by the rate cap."""
    await engine._db._execute(
        "UPDATE players SET last_survey_at = last_survey_at - 7200 WHERE key != ''",
    )


@pytest.mark.asyncio
async def test_survey_already_surveyed_today(adapter, engine):
    await adapter.simulate_message("/lora survey")
    await _clear_survey_cooldown(engine)
    response = await adapter.simulate_message("/lora survey")
    assert "ALREADY SURVEYED" in response


@pytest.mark.asyncio
async def test_survey_same_hex_allowed_next_day(adapter, engine, db):
    from datetime import datetime, timedelta, timezone

    await adapter.simulate_message("/lora survey")
    await _clear_survey_cooldown(engine)
    assert "ALREADY SURVEYED" in await adapter.simulate_message("/lora survey")

    tomorrow = datetime.now(timezone.utc) + timedelta(days=1, hours=1)
    await _clear_survey_cooldown(engine)
    with patch("lora_explorer.game.database.datetime") as mock_dt:
        mock_dt.now.return_value = tomorrow
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        response = await adapter.simulate_message("/lora survey")
    assert "SURVEY LOGGED" in response


@pytest.mark.asyncio
async def test_survey_rate_cap_radio_returns_hold_message(adapter, engine):
    # A second radio survey within SURVEY_MIN_INTERVAL_S is rejected with a
    # gentle "hold" message rather than being processed.
    adapter.mock_position = (40.0, -105.0)
    first = await adapter.simulate_message("/lora survey")
    assert "SURVEY LOGGED" in first
    adapter.mock_position = (40.2, -105.0)  # a different hex
    second = await adapter.simulate_message("/lora survey")
    assert "TRANSMITTER COOLING" in second


@pytest.mark.asyncio
async def test_survey_rate_cap_clears_after_interval(adapter, engine):
    await adapter.simulate_message("/lora survey")
    await _clear_survey_cooldown(engine)
    adapter.mock_position = (40.2, -105.0)
    again = await adapter.simulate_message("/lora survey")
    assert "SURVEY LOGGED" in again


@pytest.mark.asyncio
async def test_web_auto_survey_rate_limited_is_silent(adapter, engine, db):
    # Auto-surveys that arrive too soon are a silent no-op — no "hold" message,
    # no survey_rejected feed event — so hands-free driving doesn't spam the feed.
    pk = "abc123"
    await db.get_or_create_player(pk, 40.0, -105.0)
    await db._execute(
        "UPDATE players SET last_survey_at = ? WHERE key = ?", (time.time(), pk),
    )
    engine._event_history.clear()
    result = await engine.web_survey(pk, auto=True)
    assert result["ok"] is False and result.get("reason") == "rate"
    assert not [e for e in engine._event_history if e["type"] == "survey_rejected"]


@pytest.mark.asyncio
async def test_web_manual_survey_rate_limited_publishes_rejection(adapter, engine, db):
    pk = "abc123"
    await db.get_or_create_player(pk, 40.0, -105.0)
    await db._execute(
        "UPDATE players SET last_survey_at = ? WHERE key = ?", (time.time(), pk),
    )
    engine._event_history.clear()
    result = await engine.web_survey(pk, auto=False)
    assert result["ok"] is False
    assert "cooling" in result["error"].lower()
    assert [e for e in engine._event_history if e["type"] == "survey_rejected"]


@pytest.mark.asyncio
async def test_survey_daily_cap_warning_in_sse(adapter, engine, db):
    from lora_explorer.game.engine import DAILY_SURVEY_WARNING_THRESHOLD, DAILY_SURVEY_CAP

    pk = "abc123"
    await db.get_or_create_player(pk, 40.0, -105.0)
    for i in range(DAILY_SURVEY_WARNING_THRESHOLD):
        await db.record_survey(
            player_key=pk, hex_id=f"fake_hex_{i:04d}",
            lat=40.0 + i * 0.01, lon=-105.0, distance_miles=1.0,
            snr=-8.0, rssi=-105, hops=2,
            xp_earned=20, provisions_earned=12, field_notes_earned=3,
            is_discovery=False,
        )

    engine._event_history.clear()
    await adapter.simulate_message("/lora survey")

    survey_events = [e for e in engine._event_history if e["type"] == "survey"]
    assert len(survey_events) == 1
    remaining = DAILY_SURVEY_CAP - (DAILY_SURVEY_WARNING_THRESHOLD + 1)
    assert survey_events[0]["data"]["surveys_remaining"] == remaining


@pytest.mark.asyncio
async def test_survey_no_cap_warning_below_threshold(adapter, engine, db):
    engine._event_history.clear()
    await adapter.simulate_message("/lora survey")

    survey_events = [e for e in engine._event_history if e["type"] == "survey"]
    assert len(survey_events) == 1
    assert "surveys_remaining" not in survey_events[0]["data"]


@pytest.mark.asyncio
async def test_survey_xp_increases_with_distance(adapter, engine, db):
    adapter.mock_position = NEAR_POSITION
    r1 = await adapter.simulate_message("/lora survey")
    xp_close = _extract_xp(r1)

    await db._execute(
        "UPDATE players SET last_survey_at = last_survey_at - 7200 WHERE key != ''",
    )
    adapter.mock_position = (40.15, -105.0)
    r2 = await adapter.simulate_message("/lora survey")
    xp_far = _extract_xp(r2)

    assert xp_far > xp_close


@pytest.mark.asyncio
async def test_survey_accumulates_totals(adapter, engine):
    base_time = time.time()
    with patch("lora_explorer.game.database.time") as mock_time, \
         patch("lora_explorer.game.engine.time") as mock_etime:
        mock_time.time.return_value = base_time
        mock_etime.time.return_value = base_time
        r1 = await adapter.simulate_message("/lora survey", sender="player_a")
        assert "Total:" in r1

        adapter.mock_position = (40.02, -105.01)
        mock_time.time.return_value = base_time + 3600
        mock_etime.time.return_value = base_time + 3600
        r2 = await adapter.simulate_message("/lora survey", sender="player_a")
    totals = _extract_totals(r2)
    assert totals["xp"] > 0
    assert totals["prov"] > 0


@pytest.mark.asyncio
async def test_first_survey_of_day_grants_flat_mark_bonus(adapter, engine, db):
    from lora_explorer.game.engine import (
        SURVEY_MARK_BASE, DISCOVERY_SURVEY_MARKS, FIRST_SURVEY_MARK_BONUS,
    )

    # Neutralise the daily dispatch so no extra mark/xp modifiers interfere.
    with patch("lora_explorer.game.engine.get_daily_dispatch",
               return_value={"id": "none", "message": ""}):
        adapter.mock_position = FAR_POSITION
        r1 = await adapter.simulate_message("/lora survey")
        assert "FIRST SURVEY OF THE DAY" in r1
        p1 = await db.get_player("abc123")
        # Every survey mints the base mark; this one is also a discovery and the
        # first of the day, so base + discovery + first-of-day.
        assert p1["survey_marks"] == (
            SURVEY_MARK_BASE + DISCOVERY_SURVEY_MARKS + FIRST_SURVEY_MARK_BONUS
        )

        # The survey event flags first-of-day; the full breakdown is persisted
        # (for the Ledger) rather than sent over the SSE.
        ev = [e for e in engine._event_history if e["type"] == "survey"][-1]["data"]
        assert ev["first_today"] is True
        assert "breakdown" not in ev

        surveys = await db.get_recent_surveys("abc123", limit=1)
        assert surveys[0]["marks_earned"] == p1["survey_marks"]
        bd = json.loads(surveys[0]["reward_breakdown"])
        assert bd["marks"]["first_survey"] == FIRST_SURVEY_MARK_BONUS
        assert bd["marks"]["total"] == p1["survey_marks"]

        # A second survey the same day (new hex) gets the discovery mark only.
        await db._execute(
            "UPDATE players SET last_survey_at = last_survey_at - 7200 WHERE key != ''"
        )
        adapter.mock_position = (40.15, -105.0)
        r2 = await adapter.simulate_message("/lora survey")
        assert "FIRST SURVEY OF THE DAY" not in r2
        p2 = await db.get_player("abc123")
        # Second survey: base + discovery, no first-of-day bonus.
        assert p2["survey_marks"] == p1["survey_marks"] + SURVEY_MARK_BASE + DISCOVERY_SURVEY_MARKS
        surveys2 = await db.get_recent_surveys("abc123", limit=1)
        bd2 = json.loads(surveys2[0]["reward_breakdown"])
        assert bd2["marks"]["first_survey"] == 0


@pytest.mark.asyncio
async def test_non_lora_message_ignored(adapter, engine):
    response = await adapter.simulate_message("hello there")
    assert response is None


@pytest.mark.asyncio
async def test_no_gps_returns_error(adapter, engine):
    adapter.mock_position = None
    response = await adapter.simulate_message("/lora survey")
    assert "NO RESPONSE" in response or "NO GPS FIX" in response or "CONNECTION ERROR" in response


@pytest.mark.asyncio
async def test_survey_no_ack_result_via_send(adapter, engine):
    """Handler returns None (no ACK); survey result arrives via send_message."""
    msg = IncomingMessage(sender_key="abc123", text="/lora survey", snr=-8.0, rssi=-105, hops=2)
    response = await engine._handle_message(msg)
    assert response is None
    await adapter.await_survey()
    assert any("SURVEY LOGGED" in text for _, text in adapter.sent_messages)


@pytest.mark.asyncio
async def test_survey_in_flight_silently_drops_duplicate(adapter, engine):
    """Second survey while first is in-flight is silently dropped."""
    import asyncio

    original_request = adapter.request_position
    position_ready = asyncio.Event()

    async def slow_position(node_key, progress_callback=None):
        await position_ready.wait()
        return await original_request(node_key)

    adapter.request_position = slow_position

    # Distinct timestamps so dedup doesn't fire — this exercises the in-flight
    # (busy) guard, not the duplicate-message guard.
    msg1 = IncomingMessage(sender_key="abc123", text="/lora survey", timestamp=1, snr=-8.0, rssi=-105, hops=2)
    r1 = await engine._handle_message(msg1)
    assert r1 is None

    msg2 = IncomingMessage(sender_key="abc123", text="/lora survey", timestamp=2, snr=-8.0, rssi=-105, hops=2)
    r2 = await engine._handle_message(msg2)
    assert r2 is None

    position_ready.set()
    await adapter.await_survey()
    survey_results = [text for _, text in adapter.sent_messages if "SURVEY LOGGED" in text]
    assert len(survey_results) == 1


@pytest.mark.asyncio
async def test_message_dedup_drops_retries(adapter, engine):
    """Flood retransmits (same sender + text + sender_timestamp) within 30s are dropped."""
    msg = IncomingMessage(sender_key="abc123", text="/lora upkeep", timestamp=5000, snr=-8.0, rssi=-105, hops=2)
    r1 = await engine._handle_message(msg)
    r2 = await engine._handle_message(msg)
    r3 = await engine._handle_message(msg)
    # All commands are backgrounded, so _handle_message always returns None.
    assert r1 is None and r2 is None and r3 is None
    await adapter.await_survey()  # drain the single in-flight command before teardown
    # Three identical (retransmitted) messages → exactly one command processed.
    assert len(adapter.sent_messages) == 1


@pytest.mark.asyncio
async def test_message_dedup_allows_new_timestamp(adapter, engine):
    """A genuine repeat command carries a new sender_timestamp and must be
    processed, not mistaken for a flood retransmit."""
    m1 = IncomingMessage(sender_key="abc123", text="/lora upkeep", timestamp=5000)
    await engine._handle_message(m1)
    await adapter.await_survey()
    m2 = IncomingMessage(sender_key="abc123", text="/lora upkeep", timestamp=5001)
    await engine._handle_message(m2)
    await adapter.await_survey()
    # Both processed despite identical sender + text.
    assert len(adapter.sent_messages) == 2


@pytest.mark.asyncio
async def test_radio_and_web_commands_share_one_slot(adapter, engine, db):
    """One physical companion → one GPS command at a time. A held command slot
    blocks both a dashboard command and a radio command, from either source."""
    import asyncio
    await db.get_or_create_player("abc123", 40.0, -105.0)

    # Hold the shared command slot with a task that stays pending.
    gate = asyncio.Event()
    held = asyncio.create_task(gate.wait())
    engine._command_task = held

    # A dashboard command is refused while the slot is held.
    web_result = await engine.web_survey("abc123")
    assert web_result["ok"] is False
    assert "in progress" in web_result["error"].lower()

    # A radio command is dropped (busy) and does not replace the held slot.
    msg = IncomingMessage(sender_key="abc123", text="/lora survey", timestamp=1)
    assert await engine._handle_message(msg) is None
    assert engine._command_task is held

    gate.set()
    await held


@pytest.mark.asyncio
async def test_distance_calculation(adapter, engine):
    adapter.mock_position = (40.15, -105.0)
    response = await adapter.simulate_message("/lora survey")
    assert "SURVEY LOGGED" in response


@pytest.mark.asyncio
async def test_discovery_bonus_xp(adapter, engine):
    response = await adapter.simulate_message("/lora survey")
    xp = _extract_xp(response)
    assert xp >= 75


# ── Survey with post bonus ──

@pytest.mark.asyncio
async def test_survey_at_outpost_hex_gives_post_bonus(adapter, engine, db):
    await _give_resources(db, "abc123")
    await adapter.simulate_message("/lora survey")
    await adapter.simulate_message("/lora charter")
    await adapter.simulate_message("/lora Trailhead")
    # Next day, survey from the outpost's own hex — the active-survey post bonus
    # (level-1 post → 1.1x) is applied to the reward.
    from datetime import datetime, timedelta, timezone
    await _clear_survey_cooldown(engine)
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1, hours=1)
    with patch("lora_explorer.game.database.datetime") as mock_dt:
        mock_dt.now.return_value = tomorrow
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        r_with_post = await adapter.simulate_message("/lora survey")
    assert "SURVEY LOGGED" in r_with_post
    surveys = await db.get_recent_surveys("abc123", limit=1)
    bd = json.loads(surveys[0]["reward_breakdown"])
    assert bd["provisions"]["post_mult"] == 1.1


@pytest.mark.asyncio
async def test_survey_at_outpost_does_not_reset_ruin(adapter, engine, db):
    await _give_resources(db, "abc123")
    await adapter.simulate_message("/lora survey")
    await adapter.simulate_message("/lora charter")
    await adapter.simulate_message("/lora Watchtower")

    post_before = await db.get_post_in_hex("abc123", engine._get_hex_id(*FAR_POSITION))
    old_tended = post_before["last_tended_at"]

    from datetime import datetime, timedelta, timezone
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1, hours=1)
    with patch("lora_explorer.game.database.datetime") as mock_dt:
        mock_dt.now.return_value = tomorrow
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        with patch("lora_explorer.game.database.time") as mock_time:
            mock_time.time.return_value = time.time() + 86400
            await adapter.simulate_message("/lora survey")

    post_after = await db.get_post_in_hex("abc123", engine._get_hex_id(*FAR_POSITION))
    # Surveying past a post no longer tends it — only /lora upkeep does.
    assert post_after["last_tended_at"] == old_tended


# ── Charter tests ──

@pytest.mark.asyncio
async def test_charter_flow_full(adapter, engine, db):
    await _give_resources(db, "abc123")
    await adapter.simulate_message("/lora survey")

    player_before = await db.get_player("abc123")
    prov_before = player_before["provisions"]
    marks_before = player_before["survey_marks"]

    dispatch = get_daily_dispatch()
    discounted = dispatch["id"] == "charter_discount"
    expected_prov = CHARTER_PROVISION_COST // 2 if discounted else CHARTER_PROVISION_COST
    expected_marks = max(1, CHARTER_MARK_COST // 2) if discounted else CHARTER_MARK_COST

    response = await adapter.simulate_message("/lora charter")
    assert "CHARTER READY" in response
    assert "Name your outpost" in response
    assert f"{expected_prov}prov" in response

    response = await adapter.simulate_message("/lora Hotdog")
    assert "OUTPOST CHARTERED" in response
    assert "Hotdog" in response

    player = await db.get_player("abc123")
    assert player["provisions"] == prov_before - expected_prov
    # First charter also grants the Charter License checkpoint bonus marks.
    from lora_explorer.game.engine import CHARTER_CHECKPOINT_MARKS
    assert player["survey_marks"] == marks_before - expected_marks + CHARTER_CHECKPOINT_MARKS


@pytest.mark.asyncio
async def test_charter_too_close_to_camp(adapter, engine, db):
    await _give_resources(db, "abc123")
    adapter.mock_position = NEAR_POSITION
    await adapter.simulate_message("/lora survey")
    response = await adapter.simulate_message("/lora charter")
    assert "TOO CLOSE TO CAMP" in response


@pytest.mark.asyncio
async def test_charter_insufficient_provisions(adapter, engine, db):
    await _give_resources(db, "abc123", provisions=0, marks=10)
    await adapter.simulate_message("/lora survey")
    await db._execute("UPDATE players SET provisions = 5 WHERE key = ?", ("abc123",))
    response = await adapter.simulate_message("/lora charter")
    assert "INSUFFICIENT PROVISIONS" in response


@pytest.mark.asyncio
async def test_charter_insufficient_marks(adapter, engine, db):
    await _give_resources(db, "abc123", provisions=100, marks=0)
    await adapter.simulate_message("/lora survey")
    await db._execute("UPDATE players SET survey_marks = 0 WHERE key = ?", ("abc123",))
    response = await adapter.simulate_message("/lora charter")
    assert "INSUFFICIENT SURVEY MARKS" in response


@pytest.mark.asyncio
async def test_charter_undiscovered_hex(adapter, engine, db):
    await _give_resources(db, "abc123")
    response = await adapter.simulate_message("/lora charter")
    assert "UNCHARTED TERRITORY" in response


@pytest.mark.asyncio
async def test_charter_at_post_limit(adapter, engine, db):
    await _give_resources(db, "abc123", provisions=2000, marks=50, camp_level=3)
    base_time = time.time()
    positions = [FAR_POSITION, (40.15, -105.0), (40.22, -105.0), (40.29, -105.0)]
    with patch("lora_explorer.game.database.time") as mock_time, \
         patch("lora_explorer.game.engine.time") as mock_etime:
        for i in range(3):
            mock_time.time.return_value = base_time + i * 3600
            mock_etime.time.return_value = base_time + i * 3600
            adapter.mock_position = positions[i]
            await adapter.simulate_message("/lora survey")
            await adapter.simulate_message("/lora charter")
            await adapter.simulate_message(f"/lora Post{i+1}")

        mock_time.time.return_value = base_time + 4 * 3600
        mock_etime.time.return_value = base_time + 4 * 3600
        adapter.mock_position = positions[3]
        await adapter.simulate_message("/lora survey")
        response = await adapter.simulate_message("/lora charter")
    assert "ALL CHARTERS CLAIMED" in response
    assert "3/3" in response


@pytest.mark.asyncio
async def test_charter_hex_already_occupied(adapter, engine, db):
    await _give_resources(db, "abc123", provisions=500, marks=50, camp_level=3)
    await adapter.simulate_message("/lora survey")
    await adapter.simulate_message("/lora charter")
    await adapter.simulate_message("/lora First")

    response = await adapter.simulate_message("/lora charter")
    assert "TERRITORY OCCUPIED" in response


@pytest.mark.asyncio
async def test_charter_name_without_pending(adapter, engine):
    response = await adapter.simulate_message("/lora Hotdog")
    assert "UNKNOWN COMMAND" in response


@pytest.mark.asyncio
async def test_charter_typo_becomes_unknown(adapter, engine):
    """A typo like /lora surve should not silently vanish."""
    response = await adapter.simulate_message("/lora surve")
    assert "UNKNOWN COMMAND" in response
    assert "/lora survey" in response


@pytest.mark.asyncio
async def test_charter_recheck_balance(adapter, engine, db):
    """If player spends resources between charter and naming, charter fails."""
    await _give_resources(db, "abc123")
    await adapter.simulate_message("/lora survey")
    response = await adapter.simulate_message("/lora charter")
    assert "CHARTER READY" in response

    key = (await db.get_first_player())["key"]
    player = await db.get_player(key)
    await db.deduct_provisions(key, player["provisions"])

    response = await adapter.simulate_message("/lora Outpost Alpha")
    assert "CHARTER FAILED" in response
    assert "provisions" in response.lower()


@pytest.mark.asyncio
async def test_charter_hex_mismatch(adapter, engine, db):
    await _give_resources(db, "abc123")
    await adapter.simulate_message("/lora survey")
    response = await adapter.simulate_message("/lora charter")
    assert "CHARTER READY" in response

    adapter.mock_position = (34.0, -111.0)
    response = await adapter.simulate_message("/lora Hotdog")
    assert "CHARTER EXPIRED" in response or "mismatch" in response.lower()


@pytest.mark.asyncio
async def test_charter_camp_too_low(adapter, engine, db):
    await _give_resources(db, "abc123", camp_level=1, rank_level=8)
    await adapter.simulate_message("/lora survey")
    response = await adapter.simulate_message("/lora charter")
    assert "CAMP TOO LOW" in response


@pytest.mark.asyncio
async def test_charter_rank_too_low(adapter, engine, db):
    await _give_resources(db, "abc123", camp_level=3, rank_level=5)
    await adapter.simulate_message("/lora survey")
    response = await adapter.simulate_message("/lora charter")
    assert "RANK TOO LOW" in response


# ── Reinforce tests ──

@pytest.mark.asyncio
async def test_upkeep_resets_ruin_timer(adapter, engine, db):
    await _give_resources(db, "abc123")
    await adapter.simulate_message("/lora survey")
    await adapter.simulate_message("/lora charter")
    await adapter.simulate_message("/lora Beacon")

    response = await adapter.simulate_message("/lora upkeep")
    assert "UPKEEP DONE" in response
    assert "Beacon" in response
    assert "Full income restored" in response


@pytest.mark.asyncio
async def test_upkeep_no_outpost(adapter, engine, db):
    await db.get_or_create_player("abc123", 40.0, -105.0)
    response = await adapter.simulate_message("/lora upkeep")
    assert "NO OUTPOST HERE" in response


# ── Ruin (income-decay) tests ──

def test_ruin_income_factor_curve():
    from lora_explorer.game.engine import (
        ruin_income_factor, RUIN_GRACE_DAYS, RUIN_RAMP_DAYS,
    )
    assert ruin_income_factor(0) == 1.0
    assert ruin_income_factor(RUIN_GRACE_DAYS) == 1.0
    mid = RUIN_GRACE_DAYS + RUIN_RAMP_DAYS / 2
    assert abs(ruin_income_factor(mid) - 0.5) < 1e-9
    assert ruin_income_factor(RUIN_GRACE_DAYS + RUIN_RAMP_DAYS) == 0.0
    assert ruin_income_factor(1000) == 0.0


def test_ruin_effective_days_integral():
    from lora_explorer.game.engine import (
        ruin_effective_days, RUIN_GRACE_DAYS, RUIN_RAMP_DAYS,
    )
    # A window entirely within grace earns full days.
    assert abs(ruin_effective_days(0, 5) - 5) < 1e-9
    # From fresh through the full ramp: grace days + the ramp triangle (RAMP/2).
    total = ruin_effective_days(0, RUIN_GRACE_DAYS + RUIN_RAMP_DAYS)
    assert abs(total - (RUIN_GRACE_DAYS + RUIN_RAMP_DAYS / 2)) < 1e-9
    # A window entirely past the ramp earns nothing.
    assert ruin_effective_days(RUIN_GRACE_DAYS + RUIN_RAMP_DAYS, 40) == 0.0


@pytest.mark.asyncio
async def test_ruined_post_earns_nothing_but_survives(engine, db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    hex_id = engine._get_hex_id(40.1, -105.0)
    post = await db.create_post("key1", hex_id, "Old Fort")
    now = int(time.time())
    # Untended for 30 days (well past ruin), last collected yesterday: the whole
    # earning window sits in the dead zone.
    await db._execute(
        "UPDATE survey_posts SET level = 3, last_tended_at = ?, last_collected_at = ? WHERE id = ?",
        (now - 30 * 86400, now - 86400, post["id"]),
    )
    result = await engine.collect_passive_provisions("key1")
    assert result["total"] == 0
    # In ruin means zero income — never destruction or level loss.
    survivor = await db.get_post_in_hex("key1", hex_id)
    assert survivor is not None
    assert survivor["level"] == 3


@pytest.mark.asyncio
async def test_fresh_post_earns_full_income(engine, db):
    await db.get_or_create_player("key1", 40.0, -105.0)
    hex_id = engine._get_hex_id(40.1, -105.0)
    post = await db.create_post("key1", hex_id, "New Fort")
    now = int(time.time())
    # Tended and collected 2 days ago — squarely inside the grace window.
    await db._execute(
        "UPDATE survey_posts SET last_tended_at = ?, last_collected_at = ? WHERE id = ?",
        (now - 2 * 86400, now - 2 * 86400, post["id"]),
    )
    dist = engine._distance_from_hex(hex_id, 40.0, -105.0)
    expected = int(2 * 1 * (1 + int(dist // 5)))
    result = await engine.collect_passive_provisions("key1")
    assert result["total"] == expected


# ── Helpers ──

def _extract_xp(response: str) -> int:
    for part in response.split():
        if part.startswith("+") and "xp" in part:
            return int(part.replace("+", "").replace("xp", ""))
    return 0


def _extract_totals(response: str) -> dict:
    result = {"xp": 0, "prov": 0}
    for line in response.split("\n"):
        if "Total:" in line:
            for part in line.split("|"):
                part = part.strip()
                if "xp" in part:
                    result["xp"] = int("".join(c for c in part if c.isdigit()))
                if "prov" in part:
                    result["prov"] = int("".join(c for c in part if c.isdigit()))
    return result


# ── Anti-abuse: velocity check ──

@pytest.mark.asyncio
async def test_velocity_rejects_teleportation(adapter, engine):
    base_time = time.time()
    with patch("lora_explorer.game.database.time") as mock_time, \
         patch("lora_explorer.game.engine.time") as mock_etime:
        mock_time.time.return_value = base_time
        mock_etime.time.return_value = base_time
        await adapter.simulate_message("/lora survey")
        adapter.mock_position = (45.0, -105.0)
        mock_time.time.return_value = base_time + 60
        mock_etime.time.return_value = base_time + 60
        response = await adapter.simulate_message("/lora survey")
    assert "SURVEY REJECTED" in response


@pytest.mark.asyncio
async def test_velocity_allows_normal_travel(adapter, engine):
    base_time = time.time()
    with patch("lora_explorer.game.database.time") as mock_time, \
         patch("lora_explorer.game.engine.time") as mock_etime:
        mock_time.time.return_value = base_time
        mock_etime.time.return_value = base_time
        await adapter.simulate_message("/lora survey")
        adapter.mock_position = (40.015, -105.0)
        mock_time.time.return_value = base_time + 3600
        mock_etime.time.return_value = base_time + 3600
        response = await adapter.simulate_message("/lora survey")
    assert "SURVEY LOGGED" in response


@pytest.mark.asyncio
async def test_velocity_first_survey_always_passes(adapter, engine):
    response = await adapter.simulate_message("/lora survey")
    assert "SURVEY LOGGED" in response


@pytest.mark.asyncio
async def test_velocity_stores_position(adapter, engine, db):
    await adapter.simulate_message("/lora survey")
    player = await db.get_player("abc123")
    assert player["last_survey_lat"] == FAR_POSITION[0]
    assert player["last_survey_lon"] == FAR_POSITION[1]
    assert player["last_survey_at"] is not None


# ── Camp multiplier ──

@pytest.mark.asyncio
async def test_camp_multiplier_matches_gdd(engine):
    from lora_explorer.game.engine import BASE_CAMP_TABLE
    expected = {1: 1.0, 2: 1.1, 3: 1.2, 4: 1.3, 5: 1.4,
                6: 1.5, 7: 1.6, 8: 1.7, 9: 1.8, 10: 2.0}
    for level, mult in expected.items():
        assert engine._camp_multiplier(level) == mult
        assert BASE_CAMP_TABLE[level]["mult"] == mult


# ── Auto-promote ──

@pytest.mark.asyncio
async def test_auto_promote_single_rank(adapter, engine, db):
    await db.get_or_create_player("abc123", 40.0, -105.0)
    await db._execute(
        "UPDATE players SET xp = 200, provisions = 50 WHERE key = ?",
        ("abc123",),
    )
    promotions = await engine._auto_promote("abc123", 1, 200)
    assert len(promotions) == 1
    assert promotions[0]["level"] == 2
    assert promotions[0]["name"] == "Novice"
    player = await db.get_player("abc123")
    assert player["rank_level"] == 2
    assert player["provisions"] == 50 + promotions[0]["reward_prov"]


@pytest.mark.asyncio
async def test_auto_promote_insufficient_xp(adapter, engine, db):
    await db.get_or_create_player("abc123", 40.0, -105.0)
    await db._execute(
        "UPDATE players SET xp = 10 WHERE key = ?",
        ("abc123",),
    )
    promotions = await engine._auto_promote("abc123", 1, 10)
    assert len(promotions) == 0
    player = await db.get_player("abc123")
    assert player["rank_level"] == 1


@pytest.mark.asyncio
async def test_auto_promote_max_rank(adapter, engine, db):
    await db.get_or_create_player("abc123", 40.0, -105.0)
    await db._execute(
        "UPDATE players SET rank_level = 50, xp = 999999 WHERE key = ?",
        ("abc123",),
    )
    promotions = await engine._auto_promote("abc123", 50, 999999)
    assert len(promotions) == 0


@pytest.mark.asyncio
async def test_auto_promote_multi_level(adapter, engine, db):
    await db.get_or_create_player("abc123", 40.0, -105.0)
    await db._execute(
        "UPDATE players SET xp = 50000, provisions = 500 WHERE key = ?",
        ("abc123",),
    )
    promotions = await engine._auto_promote("abc123", 1, 50000)
    assert len(promotions) > 1
    assert promotions[0]["level"] == 2
    for i in range(1, len(promotions)):
        assert promotions[i]["level"] == promotions[i - 1]["level"] + 1
    player = await db.get_player("abc123")
    assert player["rank_level"] == promotions[-1]["level"]


@pytest.mark.asyncio
async def test_auto_promote_rewards_resources(adapter, engine, db):
    await db.get_or_create_player("abc123", 40.0, -105.0)
    await db._execute(
        "UPDATE players SET rank_level = 5, xp = 6000, provisions = 100 WHERE key = ?",
        ("abc123",),
    )
    promotions = await engine._auto_promote("abc123", 5, 6000)
    assert len(promotions) >= 1
    total_prov = sum(p["reward_prov"] for p in promotions)
    player = await db.get_player("abc123")
    assert player["provisions"] == 100 + total_prov


# ── Base camp upgrade ──

@pytest.mark.asyncio
async def test_base_camp_upgrade_success(adapter, engine, db):
    await db.get_or_create_player("abc123", 40.0, -105.0)
    await db._execute(
        "UPDATE players SET provisions = 200 WHERE key = ?",
        ("abc123",),
    )
    result = await engine.upgrade_base_camp("abc123")
    assert result["success"] is True
    assert result["new_level"] == 2
    assert result["mult"] == 1.1
    player = await db.get_player("abc123")
    assert player["base_camp_level"] == 2


@pytest.mark.asyncio
async def test_base_camp_upgrade_insufficient_resources(adapter, engine, db):
    await db.get_or_create_player("abc123", 40.0, -105.0)
    result = await engine.upgrade_base_camp("abc123")
    assert result["success"] is False


@pytest.mark.asyncio
async def test_base_camp_upgrade_max_level(adapter, engine, db):
    await db.get_or_create_player("abc123", 40.0, -105.0)
    await db._execute(
        "UPDATE players SET base_camp_level = 10 WHERE key = ?",
        ("abc123",),
    )
    result = await engine.upgrade_base_camp("abc123")
    assert result["success"] is False
    assert "max" in result["reason"].lower()


@pytest.mark.asyncio
async def test_base_camp_upgrade_affects_xp(adapter, engine, db):
    adapter.mock_position = FAR_POSITION
    r1 = await adapter.simulate_message("/lora survey")
    xp_base = _extract_xp(r1)

    await db._execute(
        "UPDATE players SET base_camp_level = 5, last_survey_at = last_survey_at - 7200 WHERE key != ''",
    )
    adapter.mock_position = (40.16, -105.0)
    r2 = await adapter.simulate_message("/lora survey")
    xp_upgraded = _extract_xp(r2)
    assert xp_upgraded > xp_base


# ── Post upgrade ──

@pytest.mark.asyncio
async def test_post_upgrade_success(adapter, engine, db):
    await _give_resources(db, "abc123", provisions=500, marks=50)
    await adapter.simulate_message("/lora survey")
    await adapter.simulate_message("/lora charter")
    await adapter.simulate_message("/lora Fort")
    post = await db.get_post_in_hex("abc123", engine._get_hex_id(*FAR_POSITION))
    result = await engine.upgrade_post("abc123", post["id"])
    assert result["success"] is True
    assert result["new_level"] == 2
    assert result["cost"] == 40


@pytest.mark.asyncio
async def test_post_upgrade_insufficient_provisions(adapter, engine, db):
    await _give_resources(db, "abc123")
    await adapter.simulate_message("/lora survey")
    await adapter.simulate_message("/lora charter")
    await adapter.simulate_message("/lora Fort")
    await db._execute("UPDATE players SET provisions = 5 WHERE key = ?", ("abc123",))
    post = await db.get_post_in_hex("abc123", engine._get_hex_id(*FAR_POSITION))
    result = await engine.upgrade_post("abc123", post["id"])
    assert result["success"] is False


@pytest.mark.asyncio
async def test_post_upgrade_max_level(adapter, engine, db):
    await _give_resources(db, "abc123", provisions=5000)
    await adapter.simulate_message("/lora survey")
    await adapter.simulate_message("/lora charter")
    await adapter.simulate_message("/lora Fort")
    post = await db.get_post_in_hex("abc123", engine._get_hex_id(*FAR_POSITION))
    await db._execute("UPDATE survey_posts SET level = 5 WHERE id = ?", (post["id"],))
    result = await engine.upgrade_post("abc123", post["id"])
    assert result["success"] is False
    assert "max" in result["reason"].lower()


# ── Postcards ──

@pytest.mark.asyncio
async def test_postcard_strider(adapter, engine, db):
    await db.get_or_create_player("abc123", 40.0, -105.0)
    cards = await engine._check_postcards("abc123", 5.0, False)
    assert any(c["class"] == "Strider" for c in cards)


@pytest.mark.asyncio
async def test_postcard_trailblazer(adapter, engine, db):
    await db.get_or_create_player("abc123", 40.0, -105.0)
    for i in range(5):
        await db.discover_hex("abc123", f"fake_hex_{i}")
    cards = await engine._check_postcards("abc123", 5.0, True)
    assert any(c["class"] == "Trailblazer" for c in cards)


@pytest.mark.asyncio
async def test_postcard_no_duplicates(adapter, engine, db):
    await db.get_or_create_player("abc123", 40.0, -105.0)
    cards1 = await engine._check_postcards("abc123", 15.0, False)
    cards2 = await engine._check_postcards("abc123", 15.0, False)
    strider_1 = [c for c in cards1 if c["class"] == "Strider"]
    strider_2 = [c for c in cards2 if c["class"] == "Strider"]
    assert len(strider_1) > 0
    assert len(strider_2) == 0


@pytest.mark.asyncio
async def test_postcard_relentless(adapter, engine, db):
    await db.get_or_create_player("abc123", 40.0, -105.0)
    day_start = db._day_start()
    day = 86400
    for i in range(10):
        survey_time = day_start - i * day + 3600
        await db._execute(
            """INSERT INTO surveys
               (player_key, hex_id, lat, lon, distance_miles, snr, xp_earned,
                provisions_earned, field_notes_earned, is_discovery, surveyed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("abc123", f"hex_{i}", 40.0, -105.0, 5.0, -8.0, 20, 12, 3, 0, survey_time),
        )
    cards = await engine._check_postcards("abc123", 5.0, False)
    assert any(c["class"] == "Relentless" for c in cards)


@pytest.mark.asyncio
async def test_postcard_multiple_per_survey(adapter, engine, db):
    await db.get_or_create_player("abc123", 40.0, -105.0)
    cards = await engine._check_postcards("abc123", 35.0, False)
    classes = {c["class"] for c in cards}
    assert len(classes) >= 2


# ── Passive provisions ──

@pytest.mark.asyncio
async def test_passive_provisions_collected_on_survey(adapter, engine, db):
    await _give_resources(db, "abc123")
    await adapter.simulate_message("/lora survey")
    await adapter.simulate_message("/lora charter")
    await adapter.simulate_message("/lora Outpost")
    post = await db.get_post_in_hex("abc123", engine._get_hex_id(*FAR_POSITION))
    two_days_ago = int(time.time()) - 2 * 86400
    await db._execute(
        "UPDATE survey_posts SET last_collected_at = ? WHERE id = ?",
        (two_days_ago, post["id"]),
    )
    result = await engine.collect_passive_provisions("abc123")
    assert result["total"] > 0


@pytest.mark.asyncio
async def test_passive_provisions_formula(adapter, engine, db):
    await _give_resources(db, "abc123")
    await adapter.simulate_message("/lora survey")
    await adapter.simulate_message("/lora charter")
    await adapter.simulate_message("/lora Outpost")
    post = await db.get_post_in_hex("abc123", engine._get_hex_id(*FAR_POSITION))
    dist = engine._distance_from_hex(post["hex_id"])
    import math as m
    one_day_ago = int(time.time()) - 86400
    await db._execute(
        "UPDATE survey_posts SET last_collected_at = ? WHERE id = ?",
        (one_day_ago, post["id"]),
    )
    result = await engine.collect_passive_provisions("abc123")
    expected = int(1.0 * 1 * (1 + m.floor(dist / 5)))
    assert result["total"] == expected


@pytest.mark.asyncio
async def test_passive_provisions_zero_when_fresh(adapter, engine, db):
    await _give_resources(db, "abc123")
    await adapter.simulate_message("/lora survey")
    await adapter.simulate_message("/lora charter")
    await adapter.simulate_message("/lora Outpost")
    result = await engine.collect_passive_provisions("abc123")
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_warded_post_pauses_income(adapter, engine, db):
    await _give_resources(db, "abc123")
    await adapter.simulate_message("/lora survey")
    await adapter.simulate_message("/lora charter")
    await adapter.simulate_message("/lora Outpost")
    post = await db.get_post_in_hex("abc123", engine._get_hex_id(*FAR_POSITION))
    now = int(time.time())
    # Last collected 2 days ago, but the post has been warded that whole span.
    await db._execute(
        "UPDATE survey_posts SET last_collected_at = ?, warded_at = ?, ruin_frozen_until = ? WHERE id = ?",
        (now - 2 * 86400, now - 2 * 86400, now + 5 * 86400, post["id"]),
    )
    result = await engine.collect_passive_provisions("abc123")
    assert result["total"] == 0  # dormant window fully excluded


@pytest.mark.asyncio
async def test_income_accrues_before_ward_window(adapter, engine, db):
    await _give_resources(db, "abc123")
    await adapter.simulate_message("/lora survey")
    await adapter.simulate_message("/lora charter")
    await adapter.simulate_message("/lora Outpost")
    post = await db.get_post_in_hex("abc123", engine._get_hex_id(*FAR_POSITION))
    now = int(time.time())
    # Collected 3 days ago; warded only for the last 1 day → ~2 days of income.
    await db._execute(
        "UPDATE survey_posts SET last_collected_at = ?, warded_at = ?, ruin_frozen_until = ? WHERE id = ?",
        (now - 3 * 86400, now - 1 * 86400, now + 5 * 86400, post["id"]),
    )
    result = await engine.collect_passive_provisions("abc123")
    dist = engine._distance_from_hex(post["hex_id"])
    import math as m
    expected = int(2.0 * 1 * (1 + m.floor(dist / 5)))
    assert result["total"] == expected


@pytest.mark.asyncio
async def test_passive_provisions_multiple_posts_sum(adapter, engine, db):
    await _give_resources(db, "abc123", provisions=500, marks=50, camp_level=3)
    base_time = time.time()
    with patch("lora_explorer.game.database.time") as mock_time, \
         patch("lora_explorer.game.engine.time") as mock_etime:
        mock_time.time.return_value = base_time
        mock_etime.time.return_value = base_time
        await adapter.simulate_message("/lora survey")
        await adapter.simulate_message("/lora charter")
        await adapter.simulate_message("/lora Fort1")
        adapter.mock_position = (40.15, -105.0)
        mock_time.time.return_value = base_time + 3600
        mock_etime.time.return_value = base_time + 3600
        await adapter.simulate_message("/lora survey")
        await adapter.simulate_message("/lora charter")
        await adapter.simulate_message("/lora Fort2")
    two_days_ago = int(time.time()) - 2 * 86400
    await db._execute(
        "UPDATE survey_posts SET last_collected_at = ?", (two_days_ago,)
    )
    result = await engine.collect_passive_provisions("abc123")
    assert result["total"] > 0
    assert len(result["posts"]) == 2


# ── Rank notification in survey ──

@pytest.mark.asyncio
async def test_survey_auto_promotes(adapter, engine, db):
    await db.get_or_create_player("abc123", 40.0, -105.0)
    await db._execute(
        "UPDATE players SET rank_level = 4, xp = 490 WHERE key = ?",
        ("abc123",),
    )
    response = await adapter.simulate_message("/lora survey")
    assert "RANK UP:" in response
    assert "Scout" in response


@pytest.mark.asyncio
async def test_survey_no_rank_notification_below_threshold(adapter, engine, db):
    await db.get_or_create_player("abc123", 40.0, -105.0)
    # Sit mid-rank with a comfortable gap to the next threshold (rank 5→6 is
    # ~400 XP, well above a single survey's ~150) so this survey won't promote.
    await db._execute(
        "UPDATE players SET rank_level = 5, xp = 500 WHERE key = ?",
        ("abc123",),
    )
    response = await adapter.simulate_message("/lora survey")
    assert "RANK UP" not in response


# --- Momentum ---

@pytest.mark.asyncio
async def test_momentum_first_survey_sets_tier_1(adapter, engine, db):
    response = await adapter.simulate_message("/lora survey")
    player = await db.get_player("abc123")
    assert player["momentum_tier"] == 1


async def _seed_survey(db, key, days_ago):
    """Insert a survey row `days_ago` days in the past (momentum reads the
    surveys table, so a prior-day survey is what establishes the streak)."""
    now = int(time.time())
    with patch("lora_explorer.game.database.time") as mock_time:
        mock_time.time.return_value = now - days_ago * 86400
        await db.record_survey(
            player_key=key, hex_id=f"hex_{days_ago}", lat=40.07, lon=-104.93,
            distance_miles=1.0, snr=None, rssi=None, hops=None,
            xp_earned=10, provisions_earned=5, field_notes_earned=0,
            is_discovery=False,
        )


@pytest.mark.asyncio
async def test_momentum_consecutive_day_increments(adapter, engine, db):
    await db.get_or_create_player("abc123", 40.0, -105.0)
    await _seed_survey(db, "abc123", days_ago=1)
    await db._execute(
        "UPDATE players SET momentum_tier = 2 WHERE key = ?", ("abc123",)
    )

    response = await adapter.simulate_message("/lora survey")
    player = await db.get_player("abc123")
    assert player["momentum_tier"] == 3
    assert "Momentum +15% XP" in response


@pytest.mark.asyncio
async def test_momentum_missed_day_decays_by_one(adapter, engine, db):
    await db.get_or_create_player("abc123", 40.0, -105.0)
    await _seed_survey(db, "abc123", days_ago=2)
    await db._execute(
        "UPDATE players SET momentum_tier = 4 WHERE key = ?", ("abc123",)
    )
    tier = await db.update_momentum_tier("abc123")
    assert tier == 3


@pytest.mark.asyncio
async def test_momentum_caps_at_5(adapter, engine, db):
    await db.get_or_create_player("abc123", 40.0, -105.0)
    await _seed_survey(db, "abc123", days_ago=1)
    await db._execute(
        "UPDATE players SET momentum_tier = 5 WHERE key = ?", ("abc123",)
    )
    tier = await db.update_momentum_tier("abc123")
    assert tier == 5


@pytest.mark.asyncio
async def test_momentum_ignores_stale_last_survey_at(adapter, engine, db):
    # Regression (bug 14): momentum derives the streak from the surveys table,
    # not the mutable last_survey_at field. Player surveyed yesterday, but
    # last_survey_at is stale (a week back) — momentum must hold/increment, not
    # spuriously decay off the stale field.
    await db.get_or_create_player("abc123", 40.0, -105.0)
    await _seed_survey(db, "abc123", days_ago=1)
    now = int(time.time())
    await db._execute(
        "UPDATE players SET momentum_tier = 5, last_survey_at = ? WHERE key = ?",
        (now - 7 * 86400, "abc123"),
    )
    tier = await db.update_momentum_tier("abc123")
    assert tier == 5


@pytest.mark.asyncio
async def test_momentum_xp_boost_applied(adapter, engine, db):
    await db.get_or_create_player("abc123", 40.0, -105.0)
    await _seed_survey(db, "abc123", days_ago=1)
    await db._execute(
        "UPDATE players SET momentum_tier = 4 WHERE key = ?", ("abc123",)
    )

    response = await adapter.simulate_message("/lora survey")
    player = await db.get_player("abc123")
    assert player["momentum_tier"] == 5
    assert "Momentum +25% XP" in response


# --- Contracts ---

@pytest.mark.asyncio
async def test_ensure_weekly_contracts_creates_two(adapter, engine, db):
    await adapter.simulate_message("/lora survey")
    key = (await db.get_first_player())["key"]
    contracts = await engine.ensure_weekly_contracts(key)
    assert len(contracts) == 2
    assert contracts[0]["objective"] != contracts[1]["objective"]
    assert all(c["purchased"] == 0 for c in contracts)


@pytest.mark.asyncio
async def test_ensure_weekly_contracts_idempotent(adapter, engine, db):
    await adapter.simulate_message("/lora survey")
    key = (await db.get_first_player())["key"]
    c1 = await engine.ensure_weekly_contracts(key)
    c2 = await engine.ensure_weekly_contracts(key)
    assert c1[0]["id"] == c2[0]["id"]


@pytest.mark.asyncio
async def test_purchase_contract(adapter, engine, db):
    await adapter.simulate_message("/lora survey")
    key = (await db.get_first_player())["key"]
    await db._execute("UPDATE players SET provisions = 500 WHERE key = ?", (key,))
    contracts = await engine.ensure_weekly_contracts(key)
    result = await engine.purchase_contract(key, contracts[0]["id"])
    assert result["ok"]
    player = await db.get_player(key)
    assert player["provisions"] == 500 - contracts[0]["cost"]


@pytest.mark.asyncio
async def test_purchase_contract_insufficient_funds(adapter, engine, db):
    await adapter.simulate_message("/lora survey")
    key = (await db.get_first_player())["key"]
    await db._execute("UPDATE players SET provisions = 0 WHERE key = ?", (key,))
    contracts = await engine.ensure_weekly_contracts(key)
    result = await engine.purchase_contract(key, contracts[0]["id"])
    assert not result["ok"]


@pytest.mark.asyncio
async def test_contracts_never_reward_provisions(adapter, engine, db):
    """Contracts are provision sinks — the reward is never provisions, and cost
    is always paired to a marks/relic reward (never a losing trade)."""
    import lora_explorer.game.engine as eng
    await adapter.simulate_message("/lora survey")
    key = (await db.get_first_player())["key"]
    for _ in range(40):
        await db._execute("DELETE FROM contracts WHERE player_key = ?", (key,))
        contracts = await engine.ensure_weekly_contracts(key)
        for c in contracts:
            assert c["reward_type"] != "provisions"
            assert c["reward_type"] in ("survey_marks", "relic")
            assert c["cost"] >= eng.CONTRACT_TIERS[0]["cost"][0]


@pytest.mark.asyncio
async def test_item_reward_requires_pvp(adapter, engine, db):
    """Tier-IV munition rewards only appear for PvP-enabled players; with PvP off
    the premium tier always pays a relic."""
    import lora_explorer.game.engine as eng
    await adapter.simulate_message("/lora survey")
    key = (await db.get_first_player())["key"]
    for _ in range(40):
        await db._execute("DELETE FROM contracts WHERE player_key = ?", (key,))
        contracts = await engine.ensure_weekly_contracts(key, pvp_enabled=False)
        for c in contracts:
            assert c["reward_type"] not in eng.CONTRACT_ITEM_REWARD_TYPES


@pytest.mark.asyncio
async def test_item_reward_pending_grant_flow(adapter, engine, db):
    """A completed item-reward contract stays reward_granted=0 and is surfaced as
    a pending grant until explicitly marked (the manager mints it on the Worker)."""
    import lora_explorer.game.engine as eng
    await adapter.simulate_message("/lora survey")
    key = (await db.get_first_player())["key"]
    ws = eng._contract_period_start_utc()
    c = await db.create_contract(key, "survey_sweep", 1, 150, "attack_epic", 1, ws)
    await db._execute("UPDATE contracts SET purchased = 1 WHERE id = ?", (c["id"],))
    result = await db.complete_contract(c["id"], key)
    assert result is not None
    pending = await db.get_pending_contract_item_grants(key, eng.CONTRACT_ITEM_REWARD_TYPES)
    assert [p["id"] for p in pending] == [c["id"]]
    await db.mark_contract_reward_granted(c["id"])
    pending = await db.get_pending_contract_item_grants(key, eng.CONTRACT_ITEM_REWARD_TYPES)
    assert pending == []


# ── Spyglass name reconciliation ──

@pytest.mark.asyncio
async def test_reconcile_updates_renamed_spyglass(adapter, engine, db):
    await db.upsert_known_node("abc123", "Old Name")
    adapter.contacts = {"abc123def456": {"adv_name": "New Name"}}
    updated = await engine.reconcile_known_node_names()
    assert updated == 1
    nodes = await db.get_known_nodes()
    assert nodes[0]["name"] == "New Name"


@pytest.mark.asyncio
async def test_reconcile_leaves_unchanged_names(adapter, engine, db):
    await db.upsert_known_node("abc123", "Same Name")
    adapter.contacts = {"abc123": {"adv_name": "Same Name"}}
    updated = await engine.reconcile_known_node_names()
    assert updated == 0


@pytest.mark.asyncio
async def test_reconcile_ignores_unknown_and_empty(adapter, engine, db):
    await db.upsert_known_node("abc123", "Keep Me")
    # No matching contact, plus an empty adv_name that must not clobber the name.
    adapter.contacts = {"zzz999": {"adv_name": "Someone Else"}}
    assert await engine.reconcile_known_node_names() == 0
    adapter.contacts = {"abc123": {"adv_name": ""}}
    assert await engine.reconcile_known_node_names() == 0
    nodes = await db.get_known_nodes()
    assert nodes[0]["name"] == "Keep Me"


# --- Relic drop rolls (_roll_relic) ---

@pytest.mark.asyncio
async def test_roll_relic_diminish_is_per_type(engine, db):
    """A stack of recent Buried Caches suppresses only Buried Caches — a roll in
    the Wardstone band must still yield a Wardstone (under the old global
    diminishing it would have collapsed to nothing)."""
    await db.get_or_create_player("key1", 40.0, -105.0)
    for i in range(5):
        await db.add_relic("key1", "buried_cache", f"hex{i}")

    with patch("lora_explorer.game.engine.get_daily_dispatch",
               return_value={"id": "bonus_xp"}), \
         patch("lora_explorer.game.engine.random.random", return_value=0.12):
        relic = await engine._roll_relic("key1", "hexN")

    assert relic is not None
    assert relic["type"] == "wardstone"


@pytest.mark.asyncio
async def test_roll_relic_headquarters_boost(engine, db):
    """Base Camp 10 widens the Buried Cache band by +5%, so a roll just past the
    base 16% threshold lands on a Cache at HQ but on a Vigor Tonic below it."""
    await db.get_or_create_player("key1", 40.0, -105.0)

    with patch("lora_explorer.game.engine.get_daily_dispatch",
               return_value={"id": "bonus_xp"}), \
         patch("lora_explorer.game.engine.random.random", return_value=0.165):
        base = await engine._roll_relic("key1", "hexA", base_camp_level=1)
        boosted = await engine._roll_relic("key1", "hexB", base_camp_level=10)

    assert base["type"] == "vigor_tonic"
    assert boosted["type"] == "buried_cache"


# ── Society Dispatch / Analysts' Report ──

@pytest.mark.asyncio
async def test_analysts_report_no_postcard_milestones(db, engine):
    """The old 'X more territory to earn Trailblazer ★' fallback is gone: a player
    parked within reach of a milestone gets no milestone nudge in the dispatch."""
    player = await db.get_or_create_player("nomad", 40.0, -105.0)
    # 3 of 5 discovered hexes = within 50% of the first Trailblazer milestone,
    # which is exactly what used to trigger the (now removed) milestone nudge.
    for i in range(3):
        await db.discover_hex("nomad", f"hex{i}")

    report = await generate_analysts_report(
        db, player, posts=[], contracts=[], ft_complete=True,
        strongbox_claimed=True, mp=None,
    )
    assert "to earn" not in report
    assert "★" not in report
    assert report == ""


@pytest.mark.asyncio
async def test_analysts_report_incoming_raid_leads_and_bypasses_cap(db, engine):
    """An inbound raid always leads the report, even alongside urgent post-ruin
    warnings that would otherwise fill the length cap."""
    player = await db.get_or_create_player("holder", 40.0, -105.0)
    mp = {
        "incoming": [{"post": "Cedar Hollow", "eta_min": 12, "threat": "raze"}],
        "outgoing": None,
        "supply": None,
    }
    report = await generate_analysts_report(
        db, player, posts=[], contracts=[], ft_complete=True,
        strongbox_claimed=True, mp=mp,
    )
    assert "Raid inbound on Cedar Hollow — ETA 12m, projected to raze" in report
    # Leads the report — appears before the "Society Analysts' Report:" body's rest.
    assert report.index("Raid inbound") < len(report)


@pytest.mark.asyncio
async def test_analysts_report_multiple_incoming_raids_summarized(db, engine):
    player = await db.get_or_create_player("holder", 40.0, -105.0)
    mp = {
        "incoming": [
            {"post": "A", "eta_min": 20, "threat": "heavy"},
            {"post": "B", "eta_min": 8, "threat": "hold"},
        ],
        "outgoing": None,
        "supply": None,
    }
    report = await generate_analysts_report(
        db, player, posts=[], contracts=[], ft_complete=True,
        strongbox_claimed=True, mp=mp,
    )
    assert "2 raids inbound — soonest ETA 8m" in report


@pytest.mark.asyncio
async def test_analysts_report_outgoing_raid_and_supply_drop(db, engine):
    """Outgoing raiding party and the latest supply drop both surface as briefing
    nudges — the events a returning player might have missed."""
    player = await db.get_or_create_player("raider", 40.0, -105.0)
    mp = {
        "incoming": [],
        "outgoing": {"target": "Rivalton", "eta_min": 34},
        "supply": "2× Attack (Common), 1× Scout",
    }
    report = await generate_analysts_report(
        db, player, posts=[], contracts=[], ft_complete=True,
        strongbox_claimed=True, mp=mp,
    )
    assert "raiding party strikes Rivalton in ~34m" in report
    assert "Latest supply drop brought 2× Attack (Common), 1× Scout" in report


@pytest.mark.asyncio
async def test_analysts_report_empty_when_nothing_to_brief(db, engine):
    player = await db.get_or_create_player("idle", 40.0, -105.0)
    report = await generate_analysts_report(
        db, player, posts=[], contracts=[], ft_complete=True,
        strongbox_claimed=True, mp=None,
    )
    assert report == ""
