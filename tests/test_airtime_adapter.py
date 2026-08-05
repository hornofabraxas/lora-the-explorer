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


def _contact_event(contacts):
    return SimpleNamespace(type=EventType.CONTACT_MSG_RECV, payload=contacts)


@pytest.mark.asyncio
async def test_find_contact_refreshes_on_miss():
    """A contact absent from the cache — e.g. the sender we just received a
    command from, learned by the firmware after our last sync — is resolved by
    re-querying the companion, so the reply/telemetry path isn't silently lost."""
    a = MeshCoreAdapter(connection_type="wifi", host="127.0.0.1")
    a._mc = MagicMock()
    a._contacts = {}  # cache miss
    a._last_contact_refresh = 0  # past the miss debounce
    a._mc.commands.get_contacts = AsyncMock(
        return_value=_contact_event({NODE: {"adv_name": "R1", "public_key": NODE}})
    )

    contact = await a._find_contact_with_refresh(NODE)

    assert contact is not None
    assert contact["public_key"] == NODE
    a._mc.commands.get_contacts.assert_awaited_once()


@pytest.mark.asyncio
async def test_find_contact_no_refresh_within_debounce():
    """A genuinely-absent contact re-queried moments ago isn't re-fetched again
    inside the debounce window (keeps repeated sends from hammering the link)."""
    import time as _t
    a = MeshCoreAdapter(connection_type="wifi", host="127.0.0.1")
    a._mc = MagicMock()
    a._contacts = {}
    a._last_contact_refresh = _t.time()  # just refreshed
    a._mc.commands.get_contacts = AsyncMock(return_value=_contact_event({}))

    contact = await a._find_contact_with_refresh(NODE)

    assert contact is None
    a._mc.commands.get_contacts.assert_not_awaited()


@pytest.mark.asyncio
async def test_find_contact_hit_skips_refresh():
    """A cache hit never queries the companion."""
    a = MeshCoreAdapter(connection_type="wifi", host="127.0.0.1")
    a._mc = MagicMock()
    a._contacts = {NODE: {"adv_name": "R1", "public_key": NODE}}
    a._mc.commands.get_contacts = AsyncMock()

    contact = await a._find_contact_with_refresh(NODE)

    assert contact is not None
    a._mc.commands.get_contacts.assert_not_awaited()


def test_build_contact_uri_matches_app_scanner_format():
    """The add-contact URI must be the app's structured URL form, not the
    meshcore lib's raw-hex export blob (which the QR scanner rejects)."""
    from lora_explorer.radio.meshcore_adapter import _build_contact_uri

    pk = "9cd8fcf22a47333b591d96a2b848b73f457b1bb1a3ea2453a885f9e5787765b1"
    assert _build_contact_uri(pk, "Example Contact") == (
        "meshcore://contact/add?name=Example+Contact"
        f"&public_key={pk}&type=1"
    )
    # Empty name still yields a valid URI; no public key yields nothing.
    assert _build_contact_uri(pk, "").endswith(f"public_key={pk}&type=1")
    assert _build_contact_uri("", "Base Camp") is None
