"""SSE event transport: monotonic ids, broadcast fan-out, reconnect catch-up."""
import pytest
import pytest_asyncio

from lora_explorer.game.database import Database
from lora_explorer.game.engine import GameEngine
from tests.test_engine import MockRadioAdapter


@pytest.fixture
def adapter():
    return MockRadioAdapter()


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(db_path=str(tmp_path / "sse.db"))
    yield database
    await database.close()


@pytest_asyncio.fixture
async def engine(adapter, db):
    e = GameEngine(adapter=adapter, home_lat=40.0, home_lon=-105.0, db=db)
    adapter.engine = e
    await e.start()
    yield e
    await e.stop()


@pytest.mark.asyncio
async def test_events_have_monotonic_ids(engine):
    engine._publish_event("survey", {"n": 1})
    engine._publish_event("survey", {"n": 2})
    ids = [e["id"] for e in engine._event_history]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    assert ids[-1] - ids[-2] == 1


@pytest.mark.asyncio
async def test_broadcast_to_all_subscribers(engine):
    q1 = engine.subscribe()
    q2 = engine.subscribe()
    engine._publish_event("survey", {"n": 1})
    e1 = q1.get_nowait()
    e2 = q2.get_nowait()
    # Same event delivered to BOTH — not stolen by whichever gets there first.
    assert e1["id"] == e2["id"]
    assert e1["type"] == "survey"


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery(engine):
    q = engine.subscribe()
    engine.unsubscribe(q)
    engine._publish_event("survey", {"n": 1})
    assert q.empty()


@pytest.mark.asyncio
async def test_full_subscriber_drops_oldest_stays_live(engine):
    q = engine.subscribe()
    for i in range(150):  # queue maxsize is 100
        engine._publish_event("survey", {"n": i})
    # Never blocks/raises; keeps the newest events, drops the oldest.
    assert q.qsize() == 100
    newest = None
    while not q.empty():
        newest = q.get_nowait()
    assert newest["data"]["n"] == 149


@pytest.mark.asyncio
async def test_live_id_matches_persisted_id(engine):
    import asyncio
    engine._publish_event("survey", {"n": 1})
    live_id = engine._event_history[-1]["id"]
    await asyncio.sleep(0.05)  # let the fire-and-forget DB insert land
    rows = await engine.get_events_since(live_id - 1)
    assert any(r["id"] == live_id for r in rows)


@pytest.mark.asyncio
async def test_id_sequence_continues_across_restart(adapter, tmp_path):
    import asyncio
    path = str(tmp_path / "restart.db")

    db1 = Database(db_path=path)
    e1 = GameEngine(adapter=adapter, home_lat=40.0, home_lon=-105.0, db=db1)
    adapter.engine = e1
    await e1.start()
    e1._publish_event("survey", {"n": 1})
    await asyncio.sleep(0.05)  # let the DB insert land before we close
    last_id = e1._event_history[-1]["id"]
    await e1.stop()
    await db1.close()

    # Fresh process/db on the same file: ids must not restart or collide.
    db2 = Database(db_path=path)
    e2 = GameEngine(adapter=adapter, home_lat=40.0, home_lon=-105.0, db=db2)
    adapter.engine = e2
    await e2.start()
    e2._publish_event("survey", {"n": 2})
    assert e2._event_history[-1]["id"] > last_id
    await e2.stop()
    await db2.close()
