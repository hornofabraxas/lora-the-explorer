import time

import pytest
from unittest.mock import AsyncMock, MagicMock

from lora_explorer.radio.adapter import PositionResult, PositionFailure
from lora_explorer.radio.meshcore_adapter import (
    MeshCoreAdapter,
    D_DEFAULT_MILES,
    D_LEARNED_MIN_MILES,
    D_LEARNED_MAX_MILES,
    PATH_MODEL_MIN_SAMPLES,
    WINDOW_FALLBACK,
    TELEMETRY_TIMEOUT_LAST_PATH,
    TELEMETRY_TIMEOUT_FLOOD,
)

NODE = "abc123def456aa99"


def _adapter():
    # host set so it's "configured", but we never connect — we only exercise the
    # pure automatic-routing helpers, which do no I/O.
    return MeshCoreAdapter(connection_type="wifi", host="127.0.0.1")


def _connected_adapter():
    """Adapter with a mocked companion so request_position runs without I/O."""
    a = _adapter()
    a._mc = MagicMock()
    a._mc.commands.reset_path = AsyncMock()
    a._pause_auto_fetch = AsyncMock()
    a._resume_auto_fetch = AsyncMock()
    a._find_contact_with_refresh = AsyncMock(
        return_value={"adv_name": "R1", "public_key": NODE},
    )
    return a


def _record_attempts(a, results):
    """Patch _try_telemetry to return `results` in order and record each
    (attempt, route, timeout) it was called with."""
    calls = []
    it = iter(results)

    async def fake(node_key, contact, attempt=1, timeout=0, route="flood"):
        calls.append((attempt, route, timeout))
        return next(it)

    a._try_telemetry = fake
    return calls


def _samples_for_speed(a, miles: float, dt_s: int, age_s: int):
    """Seed two GPS fixes `miles` apart over `dt_s` seconds, the latest one
    `age_s` seconds ago — i.e. a node moving at miles/(dt_s) with `age_s` of
    drift since its last contact. ~69 mi per degree of latitude."""
    now = int(time.time())
    lat0, lon0 = 33.45, -112.07
    dlat = miles / 69.0
    a._route_state[NODE[:12]] = now - age_s
    a._pos_history[NODE[:12]] = [
        (lat0, lon0, now - age_s - dt_s),
        (lat0 + dlat, lon0, now - age_s),
    ]


# --- Routing decision --------------------------------------------------------

def test_cold_start_floods():
    # No learned route for this node -> flood to discover one.
    assert _adapter()._should_use_last_path(NODE) is False


def test_no_gps_fallback_recent_route_uses_last_path():
    # A recent success but no GPS history to estimate speed -> fall back to the
    # recency window, which is still fresh -> trust the cached path.
    a = _adapter()
    a._route_state[NODE[:12]] = int(time.time())
    assert a._should_use_last_path(NODE) is True


def test_no_gps_fallback_stale_route_floods():
    a = _adapter()
    a._route_state[NODE[:12]] = int(time.time()) - (WINDOW_FALLBACK + 10)
    assert a._should_use_last_path(NODE) is False


def test_low_drift_within_reach_uses_last_path():
    # Walking pace (~3 mph): even 5 min of drift stays well under D_DEFAULT.
    a = _adapter()
    _samples_for_speed(a, miles=0.25, dt_s=300, age_s=300)
    disp = a._estimate_displacement_miles(NODE)
    assert disp < D_DEFAULT_MILES
    assert a._should_use_last_path(NODE) is True


def test_high_drift_beyond_reach_floods():
    # Highway pace (~60 mph): a ~38s gap already carries the node past D_DEFAULT,
    # so the cached route is assumed stale -> flood (matches the old driving rule).
    a = _adapter()
    _samples_for_speed(a, miles=0.63, dt_s=38, age_s=38)
    disp = a._estimate_displacement_miles(NODE)
    assert disp > D_DEFAULT_MILES
    assert a._should_use_last_path(NODE) is False


def test_decision_context_recorded_for_logging():
    a = _adapter()
    _samples_for_speed(a, miles=0.63, dt_s=38, age_s=38)
    a._should_use_last_path(NODE)
    disp, speed = a._route_decision[NODE]
    assert disp is not None and speed is not None
    assert round(speed) == pytest.approx(60, abs=3)


def test_speed_needs_two_samples():
    a = _adapter()
    a._route_state[NODE[:12]] = int(time.time())
    a._pos_history[NODE[:12]] = [(33.45, -112.07, int(time.time()))]
    assert a._estimate_speed_mph(NODE) is None
    assert a._estimate_displacement_miles(NODE) is None


def test_successful_contact_records_position_sample():
    a = _adapter()
    a._last_position[NODE] = (33.45, -112.07)
    a._record_successful_contact(NODE)
    assert a._pos_history[NODE[:12]][-1][:2] == (33.45, -112.07)
    # Only the two most recent samples are kept.
    for _ in range(3):
        a._record_successful_contact(NODE)
    assert len(a._pos_history[NODE[:12]]) == 2


def test_timeout_split_ordering():
    assert TELEMETRY_TIMEOUT_LAST_PATH < TELEMETRY_TIMEOUT_FLOOD


# --- Attempt plans (request_position) ---------------------------------------

@pytest.mark.asyncio
async def test_in_window_two_last_path_then_flood():
    # A fresh route (no-GPS fallback) -> two cached tries, then a flood escalation.
    a = _connected_adapter()
    a._route_state[NODE[:12]] = int(time.time())
    calls = _record_attempts(a, [None, None, None])
    res = await a.request_position(NODE)

    assert [c[1] for c in calls] == ["last_path", "last_path", "flood"]
    assert [c[2] for c in calls] == [
        TELEMETRY_TIMEOUT_LAST_PATH,
        TELEMETRY_TIMEOUT_LAST_PATH,
        TELEMETRY_TIMEOUT_FLOOD,
    ]
    # reset_path (which wipes the firmware's cached out_path) fires only for the
    # flood escalation — never during a cached-route try.
    assert a._mc.commands.reset_path.await_count == 1
    assert res.failure == PositionFailure.TIMEOUT
    # Route proven stale after all tries fail -> dropped so the next call floods.
    assert NODE[:12] not in a._route_state


@pytest.mark.asyncio
async def test_high_drift_floods_even_with_a_learned_route():
    # A cached route on record, but the node's estimated drift exceeds D_DEFAULT,
    # so the request skips cached tries and floods immediately.
    a = _connected_adapter()
    _samples_for_speed(a, miles=0.63, dt_s=38, age_s=38)
    calls = _record_attempts(a, [None, None])
    res = await a.request_position(NODE)

    assert [c[1] for c in calls] == ["flood", "flood"]
    assert res.failure == PositionFailure.TIMEOUT


@pytest.mark.asyncio
async def test_transient_dropout_keeps_route_and_avoids_reset():
    # First cached try drops a packet; the SECOND cached try succeeds. The good
    # route must survive — no reset_path, and the learned route stays armed.
    a = _connected_adapter()
    a._route_state[NODE[:12]] = int(time.time())
    calls = _record_attempts(a, [None, PositionResult(position=(1.0, 2.0))])
    res = await a.request_position(NODE)

    assert [c[1] for c in calls] == ["last_path", "last_path"]
    assert a._mc.commands.reset_path.await_count == 0
    assert res.ok
    assert NODE[:12] in a._route_state


@pytest.mark.asyncio
async def test_cold_start_floods_twice_with_reset():
    # No prior success -> flood, two flood tries, each resetting path.
    a = _connected_adapter()
    calls = _record_attempts(a, [None, None])
    res = await a.request_position(NODE)

    assert [c[1] for c in calls] == ["flood", "flood"]
    assert a._mc.commands.reset_path.await_count == 2
    assert res.failure == PositionFailure.TIMEOUT


# --- Learned path-reach model (Phase 4) -------------------------------------

def _seed_path_samples(a, pairs):
    now = int(time.time())
    a._path_samples = [(now, disp, ok) for disp, ok in pairs]
    a._learned_d_cache = None


def test_learned_d_cold_start_is_default():
    a = _adapter()
    assert a._learned_D() == D_DEFAULT_MILES


def test_learned_d_too_few_samples_stays_default():
    a = _adapter()
    _seed_path_samples(a, [(0.2, True)] * (PATH_MODEL_MIN_SAMPLES - 1))
    assert a._learned_D() == D_DEFAULT_MILES


def test_learned_d_extends_to_observed_success():
    # Last-path succeeds out to ~0.4 mi -> D lands there, not the 0.5 default.
    a = _adapter()
    _seed_path_samples(a, [(0.4, True)] * 15)
    assert a._learned_D() == pytest.approx(0.4, abs=0.02)


def test_learned_d_all_failures_pulls_to_floor():
    a = _adapter()
    _seed_path_samples(a, [(0.4, False)] * 15)
    assert a._learned_D() == D_LEARNED_MIN_MILES


def test_learned_d_finds_success_failure_boundary():
    # Succeeds at 0.2, fails at 0.5 -> only try last-path out to the success band.
    a = _adapter()
    _seed_path_samples(a, [(0.2, True)] * 8 + [(0.5, False)] * 8)
    d = a._learned_D()
    assert 0.2 <= d < 0.5


def test_learned_d_clamped_to_max():
    a = _adapter()
    _seed_path_samples(a, [(5.0, True)] * 15)
    assert a._learned_D() == D_LEARNED_MAX_MILES


def test_learned_d_recency_weighting_forgets_old_failures():
    # Old failures at 0.3, but recent successes at 0.3 -> the fresh data wins.
    a = _adapter()
    now = int(time.time())
    old = now - 60 * 86400  # ~60 days: many half-lives ago
    a._path_samples = (
        [(old, 0.3, False)] * 20 + [(now, 0.3, True)] * 20
    )
    a._learned_d_cache = None
    assert a._learned_D() > 0.3


def test_learned_d_used_by_routing_decision():
    # A drift of 0.45 mi: last-path under the 0.5 default, but flood once the
    # learned model has tightened D below it.
    a = _adapter()
    _samples_for_speed(a, miles=0.45, dt_s=3600, age_s=3600)  # ~0.45 mph, 0.45mi drift
    assert a._estimate_displacement_miles(NODE) == pytest.approx(0.45, abs=0.02)
    assert a._should_use_last_path(NODE) is True  # default D=0.5
    _seed_path_samples(a, [(0.2, True)] * 8 + [(0.4, False)] * 8)  # learned D ~0.2
    assert a._should_use_last_path(NODE) is False


def test_record_path_sample_skips_when_no_displacement():
    a = _adapter()
    a._route_decision[NODE] = (None, None)
    a._record_path_sample(NODE, success=True)
    assert a._path_samples == []


def test_record_path_sample_appends_and_invalidates_cache():
    a = _adapter()
    a._learned_d_cache = 0.99  # stale
    a._route_decision[NODE] = (0.3, 5.0)
    a._record_path_sample(NODE, success=True)
    assert a._path_samples[-1][1:] == (0.3, True)
    assert a._learned_d_cache is None


def test_path_model_persists_and_reloads(tmp_path):
    a = _adapter()
    a.set_data_dir(str(tmp_path))
    a._route_decision[NODE] = (0.3, 5.0)
    a._record_path_sample(NODE, success=True)
    a._record_path_sample(NODE, success=False)

    b = _adapter()
    b.set_data_dir(str(tmp_path))
    assert [s[1:] for s in b._path_samples] == [(0.3, True), (0.3, False)]


@pytest.mark.asyncio
async def test_last_path_win_records_success_sample():
    a = _connected_adapter()
    _samples_for_speed(a, miles=0.1, dt_s=300, age_s=60)  # small drift -> last-path
    a._path_samples = []
    _record_attempts(a, [PositionResult(position=(1.0, 2.0))])
    await a.request_position(NODE)
    assert a._path_samples and a._path_samples[-1][2] is True


@pytest.mark.asyncio
async def test_flood_escalation_records_failure_sample():
    a = _connected_adapter()
    _samples_for_speed(a, miles=0.1, dt_s=300, age_s=60)  # last-path attempted
    a._path_samples = []
    # Both cached tries miss; the flood escalation delivers -> last-path lost.
    _record_attempts(a, [None, None, PositionResult(position=(1.0, 2.0))])
    await a.request_position(NODE)
    assert a._path_samples and a._path_samples[-1][2] is False
