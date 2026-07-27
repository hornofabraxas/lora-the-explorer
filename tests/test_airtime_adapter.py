"""Adapter-level tests for the airtime governor wiring: direct-first replies and
flood suppression under budget."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from meshcore import EventType
from lora_explorer.radio.adapter import PositionResult, PositionFailure
from lora_explorer.radio.meshcore_adapter import MeshCoreAdapter

NODE = "abc123def456aa99"


def _ok_event():
    return SimpleNamespace(type=EventType.CONTACT_MSG_RECV, payload={})


def _err_event():
    return SimpleNamespace(type=EventType.ERROR, payload="boom")


def _adapter():
    a = MeshCoreAdapter(connection_type="wifi", host="127.0.0.1")
    a._mc = MagicMock()
    a._mc.commands.reset_path = AsyncMock()
    a._pause_auto_fetch = AsyncMock()
    a._resume_auto_fetch = AsyncMock()
    a._find_contact_with_refresh = AsyncMock(
        return_value={"adv_name": "R1", "public_key": NODE},
    )
    # Pretend we already have a fresh, under-budget airtime sample so the flood
    # gate never tries to query the (mocked) companion.
    a._sample_airtime_now = AsyncMock()
    return a


def _seed_under_budget(a):
    a._governor.observe(uptime_secs=1000, tx_air_secs=0, rx_air_secs=0)
    # advance the governor's own default (monotonic) clock via a second sample
    a._governor.observe(uptime_secs=1600, tx_air_secs=0, rx_air_secs=0)


@pytest.mark.asyncio
async def test_send_message_direct_first_no_flood():
    a = _adapter()
    _seed_under_budget(a)
    a._mc.commands.send_msg_with_retry = AsyncMock(return_value=_ok_event())

    ok = await a.send_message(NODE, "hello")

    assert ok is True
    # Exactly one send (direct), no reset_path/flood.
    assert a._mc.commands.send_msg_with_retry.await_count == 1
    _, kwargs = a._mc.commands.send_msg_with_retry.await_args
    assert kwargs["max_flood_attempts"] == 0
    a._mc.commands.reset_path.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_flood_fallback_when_direct_fails():
    a = _adapter()
    _seed_under_budget(a)
    # First (direct) send fails, second (flood) succeeds.
    a._mc.commands.send_msg_with_retry = AsyncMock(
        side_effect=[None, _ok_event()]
    )

    ok = await a.send_message(NODE, "hello")

    assert ok is True
    assert a._mc.commands.send_msg_with_retry.await_count == 2
    a._mc.commands.reset_path.assert_awaited_once()
    # Second call is the flood escalation.
    _, kwargs = a._mc.commands.send_msg_with_retry.await_args
    assert kwargs["max_flood_attempts"] == 2
    assert kwargs["flood_after"] == 0


@pytest.mark.asyncio
async def test_send_message_flood_suppressed_over_budget():
    a = _adapter()
    # Over budget: 10% TX duty across the window.
    a._governor.observe(uptime_secs=1000, tx_air_secs=0, rx_air_secs=0)
    a._governor.observe(uptime_secs=1100, tx_air_secs=10, rx_air_secs=10)
    a._mc.commands.send_msg_with_retry = AsyncMock(side_effect=[None, _ok_event()])

    ok = await a.send_message(NODE, "hello")

    # Direct attempt ran and failed; flood was suppressed → overall failure, and
    # only the one direct send went out.
    assert ok is False
    assert a._mc.commands.send_msg_with_retry.await_count == 1
    a._mc.commands.reset_path.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_position_flood_suppressed_over_budget():
    a = _adapter()
    # Over budget.
    a._governor.observe(uptime_secs=1000, tx_air_secs=0, rx_air_secs=0)
    a._governor.observe(uptime_secs=1100, tx_air_secs=10, rx_air_secs=10)

    calls = []

    async def fake_try(node_key, contact, attempt=1, timeout=0, route="flood"):
        calls.append(route)
        return None

    a._try_telemetry = fake_try
    # No cached route → plan is flood-only, and every flood is suppressed.
    res = await a.request_position(NODE)

    assert res.failure == PositionFailure.TIMEOUT
    assert calls == []  # no flood ever went to the radio
