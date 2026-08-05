import asyncio
import json
import logging
import os
import time
from meshcore import MeshCore, EventType
from .adapter import RadioAdapter, IncomingMessage, MessageHandler, PositionResult, PositionFailure
from .airtime import AirtimeGovernor, SAMPLE_MAX_AGE_S

log = logging.getLogger(__name__)

# When a contact lookup MISSES, re-query the companion's contact list. A node we
# need to reach — above all the sender we just received a command from — may have
# been learned by the firmware since our last sync (CONTACT_MSG_RECV only fires
# for senders the firmware already knows). get_contacts is a local companion
# query with no mesh airtime, so re-sync eagerly; this short debounce only stops
# a genuinely-absent key from re-querying on every retry in a tight window.
CONTACT_MISS_REFRESH_INTERVAL = 5
RECONNECT_BASE_DELAY = 2
RECONNECT_MAX_DELAY = 60
# Per-attempt telemetry timeouts. A cached ("last path") route should answer in
# roughly one round-trip, so it gets a shorter deadline before we escalate to a
# flood; a flood discovery is given the full budget. Real-world data: last-path
# replies land in <9s, and flood replies have been observed as late as ~33s, so
# 15s is a comfortable last-path ceiling and 35s covers the slowest real flood.
TELEMETRY_TIMEOUT_LAST_PATH = 15
TELEMETRY_TIMEOUT_FLOOD = 35

# Automatic routing: a cached ("last path") firmware route breaks when the
# spyglass leaves the coverage cell of its entry repeater — a DISTANCE effect,
# not a time one (30s in a car ≈ 10min on foot ≈ the same drift). So we decide
# last-path vs flood from how far the node has likely moved since the path was
# learned, estimated from its own recent GPS fixes. Speed is only the converter.
#   displacement ≈ speed_est × (time since last contact)
# If that stays under D_DEFAULT_MILES the cached path is probably still good.
# 0.5 mi generalizes the old field-tuned behavior: at highway speed a ~38s hex
# cadence already exceeds it (→ flood, matching the old WINDOW_DRIVING=0), while
# a walker drifts well under it for many minutes (→ last-path, like WINDOW_WALKING).
D_DEFAULT_MILES = 0.5
# No-GPS fallback: when we can't estimate the node's speed (too few fixes / GPS
# off) we can't convert time→distance, so fall back to a plain time window plus
# the existing stale-route drop. Generous + self-healing (a wrong guess costs one
# last-path attempt, then floods).
WINDOW_FALLBACK = 300

# Learned per-install path-reach (Phase 4). Every last-path attempt is one labeled
# sample — its estimated displacement plus whether the cached route delivered — so
# over time we can fit this player's own reach distance D rather than assuming 0.5
# mi. One characteristic terrain per player, so a single global model (not per
# node). Cold-start / too little data falls back to D_DEFAULT_MILES; the fit is
# recency-weighted (terrain/repeaters change) and clamped to a sane band.
PATH_MODEL_MIN_SAMPLES = 12
PATH_MODEL_MAX_SAMPLES = 500      # rolling window kept on disk + in memory
PATH_MODEL_HALFLIFE_S = 7 * 86400  # a 1-week-old sample counts half as much
D_LEARNED_MIN_MILES = 0.15
D_LEARNED_MAX_MILES = 1.0

TELEMETRY_LOG_MAX_ENTRIES = 200


def _fmt_uptime(secs) -> str:
    """Human-readable uptime, e.g. '3d 4h', '2h 15m', '8m'."""
    if not secs or secs < 0:
        return "—"
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two lat/lon points."""
    import math
    r = 3958.7613  # mean Earth radius, miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def derive_mesh_health(status: dict) -> list[dict]:
    """Turn raw companion counters (all cumulative since boot) into a small,
    player-facing set of mesh-health metrics.

    Returns a list of metric dicts ready for the template to render as
    tap-to-expand rows:
        {"key", "label", "value", "level" (ok|warn|bad|none), "detail"}

    Every input is optional — a metric is simply omitted when the firmware
    didn't supply the fields it needs. No radio traffic is involved; all
    numbers come from local companion queries.
    """
    metrics: list[dict] = []

    def g(k):
        return status.get(k)

    uptime = g("uptime_secs")
    tx = g("tx_air_secs")
    rx = g("rx_air_secs")

    # 1. Airtime used — share of time this node spent transmitting since boot.
    if uptime and tx is not None:
        tx_duty = tx / uptime * 100
        if tx_duty >= 25:
            level = "bad"
        elif tx_duty >= 10:
            level = "warn"
        else:
            level = "ok"
        metrics.append({
            "key": "airtime", "label": "Airtime used", "value": f"{tx_duty:.1f}%",
            "level": level,
            "detail": (
                f"Your node was transmitting for {tx}s of the {_fmt_uptime(uptime)} "
                f"it has been running. Lower means more headroom — messages leave "
                f"sooner and you leave more of the channel free for everyone else."
            ),
        })

    # 2. Channel busy — share of time the radio was TX or RX.
    if uptime and tx is not None and rx is not None:
        busy = (tx + rx) / uptime * 100
        if busy >= 60:
            level = "bad"
        elif busy >= 30:
            level = "warn"
        else:
            level = "ok"
        metrics.append({
            "key": "channel_busy", "label": "Channel busy", "value": f"{busy:.1f}%",
            "level": level,
            "detail": (
                f"The radio was transmitting or receiving for {tx + rx}s of the "
                f"{_fmt_uptime(uptime)} since boot ({tx}s TX + {rx}s RX). A high "
                f"figure means a crowded channel — expect slower, less reliable delivery."
            ),
        })

    # 3. Send queue — outbound messages waiting right now.
    queue = g("queue_len")
    if queue is not None:
        if queue == 0:
            value, level = "Idle", "ok"
        else:
            value = str(queue)
            level = "bad" if queue >= 5 else "warn"
        metrics.append({
            "key": "queue", "label": "Send queue", "value": value, "level": level,
            "detail": (
                "Messages waiting in the companion's outbound queue at this moment. "
                "Zero is healthy; a number that stays above zero means the radio "
                "can't transmit as fast as it's being asked to (duty-cycle throttling "
                "or congestion)."
            ),
        })

    # 4. RX error rate — corrupted receptions (newer firmware only).
    recv = g("recv")
    recv_errors = g("recv_errors")
    if recv_errors is not None and recv is not None:
        total = recv + recv_errors
        if total > 0:
            err_pct = recv_errors / total * 100
            if err_pct >= 20:
                level = "bad"
            elif err_pct >= 5:
                level = "warn"
            else:
                level = "ok"
            metrics.append({
                "key": "rx_errors", "label": "RX errors", "value": f"{err_pct:.1f}%",
                "level": level,
                "detail": (
                    f"{recv_errors} of {total} packets the radio started to receive "
                    f"failed their checksum. A rising rate points to interference or "
                    f"links right at the edge of range."
                ),
            })

    # 5. Last signal margin — informational only. This is the last packet the
    # base camp heard from *any* node (often an unrelated distant flood), so a
    # weak reading isn't a problem to flag — hence the neutral dot.
    rssi = g("last_rssi")
    noise = g("noise_floor")
    if rssi is not None and noise is not None:
        margin = rssi - noise
        metrics.append({
            "key": "last_signal", "label": "Last signal", "value": f"{margin:g} dB over noise",
            "level": "none",
            "detail": (
                f"The most recent packet the base camp received ({rssi} dBm) sat "
                f"{margin:g} dB above the local noise floor ({noise} dBm). This is "
                f"whatever the radio last heard — possibly a distant node or a flood "
                f"passing through — so it's a snapshot of the RF environment, not a "
                f"verdict on your links. A steadily rising noise floor is the thing "
                f"worth watching."
            ),
        })

    return metrics


class MeshCoreAdapter(RadioAdapter):
    def __init__(
        self,
        connection_type: str = "wifi",
        host: str = "",
        port: int = 4000,
        serial_port: str = "/dev/ttyUSB0",
        baud_rate: int = 115200,
        ble_address: str = "",
        ble_pin: str = "",
    ):
        self._connection_type = connection_type
        self._host = host
        self._port = port
        self._serial_port = serial_port
        self._baud_rate = baud_rate
        self._ble_address = ble_address
        self._ble_pin = ble_pin
        self._data_dir: str = ""
        self._mc: MeshCore | None = None
        self._handler: MessageHandler | None = None
        self._contacts: dict = {}
        self._telemetry_pending: dict[str, asyncio.Event] = {}
        self._last_position: dict[str, tuple[float, float]] = {}
        self._last_contact_refresh: float = 0
        self._reconnect_task: asyncio.Task | None = None
        self._shutting_down = False
        self._travel_mode: str = "walking"
        self._route_state: dict[str, int] = {}
        # Up to the two most recent successful (lat, lon, ts) fixes per node
        # (keyed by node_key[:12]) — used to estimate the node's speed and, from
        # that, how far it has likely drifted since its cached route was learned.
        self._pos_history: dict[str, list[tuple[float, float, int]]] = {}
        # Per-request routing decision context (displacement_mi, speed_mph),
        # stashed so _log_telemetry_timing can record it. Cleared per request.
        self._route_decision: dict[str, tuple[float | None, float | None]] = {}
        # Learned path-reach model: rolling (ts, displacement_mi, delivered) samples
        # from real last-path attempts. `_learned_d_cache` memoizes the current fit
        # (None = recompute on next read).
        self._path_samples: list[tuple[int, float, bool]] = []
        self._learned_d_cache: float | None = None
        # Base-camp-wide airtime protection on top of the per-player survey cap.
        # Fed cumulative companion counters (get_companion_status + on-demand
        # samples before a flood); gates flooding in request_position/send_message
        # and the per-player interval in the engine.
        self._governor = AirtimeGovernor()
        self._configured = bool(
            (connection_type == "wifi" and host)
            or (connection_type == "usb" and serial_port)
            or (connection_type == "ble" and ble_address)
        )

    @property
    def configured(self) -> bool:
        return self._configured

    @property
    def _telemetry_log_path(self) -> str:
        if not self._data_dir:
            return ""
        return os.path.join(self._data_dir, "telemetry_timing.jsonl")

    @property
    def _path_model_path(self) -> str:
        if not self._data_dir:
            return ""
        return os.path.join(self._data_dir, "path_model.jsonl")

    def set_data_dir(self, path: str) -> None:
        self._data_dir = path
        self._load_route_state()
        self._load_path_model()

    @property
    def _route_state_path(self) -> str:
        if not self._data_dir:
            return ""
        return os.path.join(self._data_dir, "route_state.json")

    def _load_route_state(self) -> None:
        path = self._route_state_path
        if not path:
            return
        try:
            with open(path, "r") as f:
                data = json.load(f)
            self._route_state = data.get("last_contact", {})
            raw_hist = data.get("pos_history", {})
            self._pos_history = {
                k: [tuple(s) for s in v][-2:]
                for k, v in raw_hist.items()
                if isinstance(v, list)
            }
            mode = data.get("mode", "walking")
            self._travel_mode = mode if mode in ("walking", "driving") else "walking"
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save_route_state(self) -> None:
        path = self._route_state_path
        if not path:
            return
        try:
            with open(path, "w") as f:
                json.dump({
                    "mode": self._travel_mode,
                    "last_contact": self._route_state,
                    "pos_history": self._pos_history,
                }, f)
        except Exception:
            log.debug("Could not write route state", exc_info=True)

    def get_travel_mode(self) -> str:
        return self._travel_mode

    def set_travel_mode(self, mode: str) -> None:
        if mode not in ("walking", "driving"):
            return
        self._travel_mode = mode
        self._save_route_state()

    def _in_fresh_window(self, node_key: str) -> bool:
        """Fallback freshness check (used only when we can't estimate speed):
        True if the last successful telemetry is recent enough to still trust the
        firmware's cached path."""
        ts = self.get_last_contact_ts(node_key)
        if ts is None:
            return False
        return (int(time.time()) - ts) < WINDOW_FALLBACK

    def _history_for(self, node_key: str) -> list[tuple[float, float, int]]:
        for k, samples in self._pos_history.items():
            if k.startswith(node_key) or node_key.startswith(k):
                return samples
        return []

    def _estimate_speed_mph(self, node_key: str) -> float | None:
        """Node speed from its two most recent GPS fixes, or None if we don't
        have two usable samples."""
        samples = self._history_for(node_key)
        if len(samples) < 2:
            return None
        (lat1, lon1, ts1), (lat2, lon2, ts2) = samples[-2], samples[-1]
        dt_hours = (ts2 - ts1) / 3600.0
        if dt_hours <= 0:
            return None
        return _haversine_miles(lat1, lon1, lat2, lon2) / dt_hours

    def _estimate_displacement_miles(self, node_key: str) -> float | None:
        """How far the node has likely drifted since its cached route was learned:
        the measured drift up to the last fix plus a speed-based extrapolation for
        the time elapsed since. None when speed can't be estimated (caller then
        falls back to the time window)."""
        samples = self._history_for(node_key)
        if not samples:
            return None
        speed_mph = self._estimate_speed_mph(node_key)
        if speed_mph is None:
            return None
        # The route re-arms on every success, so its origin is the latest fix;
        # the only unknown is drift since then, hence speed x elapsed.
        last_ts = samples[-1][2]
        elapsed_hours = max(0.0, (int(time.time()) - last_ts) / 3600.0)
        return speed_mph * elapsed_hours

    def _should_use_last_path(self, node_key: str) -> bool:
        """Decide last-path vs flood for this request and stash the decision
        context for logging. Flood when there's no learned route; otherwise
        last-path only while the node's estimated drift stays under this player's
        learned path-reach distance (D_DEFAULT until enough data)."""
        ts = self.get_last_contact_ts(node_key)
        if ts is None:
            self._route_decision[node_key] = (None, None)
            return False  # no cached route to trust — flood and learn one
        disp = self._estimate_displacement_miles(node_key)
        speed = self._estimate_speed_mph(node_key)
        self._route_decision[node_key] = (disp, speed)
        if disp is None:
            # Can't convert time→distance (too few fixes / GPS off): fall back to
            # a plain recency window.
            return self._in_fresh_window(node_key)
        return disp < self._learned_D()

    # --- Learned path-reach model (Phase 4) ---------------------------------

    def _load_path_model(self) -> None:
        path = self._path_model_path
        if not path:
            return
        samples: list[tuple[int, float, bool]] = []
        try:
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        samples.append((int(d["ts"]), float(d["disp"]), bool(d["ok"])))
                    except (ValueError, KeyError, json.JSONDecodeError):
                        continue
        except FileNotFoundError:
            return
        self._path_samples = samples[-PATH_MODEL_MAX_SAMPLES:]
        self._learned_d_cache = None

    def _record_path_sample(self, node_key: str, success: bool) -> None:
        """Log one labeled last-path outcome: the displacement we estimated for it
        and whether the cached route actually delivered. Feeds _learned_D()."""
        disp, _speed = self._route_decision.get(node_key, (None, None))
        if disp is None:
            return  # no displacement estimate → nothing to learn from
        entry = (int(time.time()), round(float(disp), 3), bool(success))
        self._path_samples.append(entry)
        del self._path_samples[:-PATH_MODEL_MAX_SAMPLES]
        self._learned_d_cache = None
        path = self._path_model_path
        if not path:
            return
        try:
            with open(path, "a") as f:
                f.write(json.dumps({"ts": entry[0], "disp": entry[1], "ok": entry[2]}) + "\n")
            self._trim_path_model(path)
        except Exception:
            log.debug("Could not write path model sample", exc_info=True)

    def _trim_path_model(self, path: str) -> None:
        try:
            with open(path, "r") as f:
                lines = f.readlines()
            if len(lines) > PATH_MODEL_MAX_SAMPLES:
                with open(path, "w") as f:
                    f.writelines(lines[-PATH_MODEL_MAX_SAMPLES:])
        except Exception:
            log.debug("Could not trim path model", exc_info=True)

    def _learned_D(self) -> float:
        """This player's fitted path-reach distance: the drift threshold D that
        maximizes the historical payoff of the "use last-path when disp < D"
        policy — each past attempt it would have tried counts +weight if the
        cached route delivered, −weight if it was wasted (recency-weighted). This
        extends D only out to where last-path actually tends to succeed and pulls
        it in where it doesn't, degrading gracefully: all-fail → D_LEARNED_MIN,
        all-succeed → out to the furthest observed drift. Falls back to D_DEFAULT
        until there's enough data; clamped to a sane band; memoized until a new
        sample invalidates it."""
        if self._learned_d_cache is not None:
            return self._learned_d_cache
        samples = self._path_samples
        if len(samples) < PATH_MODEL_MIN_SAMPLES:
            self._learned_d_cache = D_DEFAULT_MILES
            return D_DEFAULT_MILES
        now = time.time()
        weighted = [
            (disp, ok, 0.5 ** ((now - ts) / PATH_MODEL_HALFLIFE_S))
            for ts, disp, ok in samples
        ]
        # Candidate D just above each observed drift; the empty policy (try
        # nothing) scores 0, so an all-failure history keeps D at the floor.
        best_val, best_D = 0.0, D_LEARNED_MIN_MILES
        for t in sorted({d + 1e-6 for d, _, _ in weighted}):
            val = sum(w if ok else -w for d, ok, w in weighted if d < t)
            if val > best_val:
                best_val, best_D = val, t
        d = max(D_LEARNED_MIN_MILES, min(D_LEARNED_MAX_MILES, best_D))
        self._learned_d_cache = d
        return d

    def get_routing_model_stats(self) -> dict:
        """Public snapshot of the learned path-reach model for diagnostics: the
        drift threshold D currently in force, how many labeled samples back it,
        and whether it's still on the cold-start default."""
        n = len(self._path_samples)
        using_default = n < PATH_MODEL_MIN_SAMPLES
        return {
            "learned_d_mi": round(self._learned_D(), 3),
            "samples": n,
            "min_samples": PATH_MODEL_MIN_SAMPLES,
            "using_default": using_default,
            "default_d_mi": D_DEFAULT_MILES,
        }

    def get_last_contact_ts(self, node_key: str) -> int | None:
        for k, ts in self._route_state.items():
            if k.startswith(node_key) or node_key.startswith(k):
                return ts
        return None

    def _record_successful_contact(self, node_key: str) -> None:
        now = int(time.time())
        key = node_key[:12]
        self._route_state[key] = now
        # Capture the node's GPS fix from this exchange (if any) so we can track
        # its speed. Keep only the two most recent samples.
        pos = self._last_position.get(node_key)
        if pos is not None:
            hist = self._pos_history.setdefault(key, [])
            hist.append((pos[0], pos[1], now))
            del hist[:-2]
        self._save_route_state()

    async def connect(self) -> None:
        if not self._configured:
            log.info("Companion not configured — skipping connection")
            return
        self._shutting_down = False
        self._mc = await self._create_connection()
        log.info("Connected to companion via %s", self._connection_desc())

        await self._initialize_session()
        log.info("Listening for messages")

    async def reconfigure(
        self,
        connection_type: str,
        host: str = "",
        port: int = 4000,
        serial_port: str = "/dev/ttyUSB0",
        ble_address: str = "",
        ble_pin: str = "",
    ) -> None:
        if self._mc:
            await self.disconnect()

        self._connection_type = connection_type
        self._host = host
        self._port = port
        self._serial_port = serial_port
        self._ble_address = ble_address
        self._ble_pin = ble_pin
        self._configured = bool(
            (connection_type == "wifi" and host)
            or (connection_type == "usb" and serial_port)
            or (connection_type == "ble" and ble_address)
        )
        self._shutting_down = False
        self._contacts = {}

        await self.connect()

    async def disconnect(self) -> None:
        self._shutting_down = True
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
        if self._mc:
            try:
                await self._mc.stop_auto_message_fetching()
            except Exception:
                pass
            try:
                await self._mc.disconnect()
            except Exception:
                pass
            self._mc = None
            log.info("Disconnected")

    @staticmethod
    def _send_ok(result) -> bool:
        """A send_msg_with_retry result counts as delivered when it's non-None
        and not an ERROR event."""
        return result is not None and result.type != EventType.ERROR

    async def send_message(self, recipient_key: str, text: str) -> bool:
        if not self._mc:
            return False
        await self._pause_auto_fetch()
        try:
            contact = await self._find_contact_with_refresh(recipient_key)
            if not contact:
                log.warning("Contact not found: %s", recipient_key)
                return False
            # A reply almost always follows a telemetry exchange that just proved
            # a fresh path (the firmware learns routes from telemetry responses),
            # so try the cheap direct route first — a text packet is our most
            # expensive packet on air. flood_after past max_attempts + zero flood
            # attempts keeps this direct-only: if there's no known path it returns
            # immediately and we fall through to the gated flood below.
            result = await self._mc.commands.send_msg_with_retry(
                contact, text, max_attempts=2, max_flood_attempts=0,
                flood_after=99, min_timeout=15,
            )
            if self._send_ok(result):
                log.info("Send confirmed via direct path for %s", recipient_key)
                return True

            # Direct failed (no path, or the cached one went stale). Escalate to a
            # flood only if the base-camp-wide airtime budget allows it.
            if not await self._reserve_flood():
                log.warning(
                    "Direct send to %s failed and flood suppressed by airtime "
                    "budget (TX %.2f%%)", recipient_key, self._governor.tx_duty_pct() or 0.0,
                )
                return False
            await self._mc.commands.reset_path(contact)
            log.info("Direct send to %s failed, retrying via flood", recipient_key)
            result = await self._mc.commands.send_msg_with_retry(
                contact, text, max_attempts=2, max_flood_attempts=2,
                flood_after=0, min_timeout=15,
            )
            if self._send_ok(result):
                log.info("Send confirmed via flood for %s", recipient_key)
                return True
            log.warning("Send failed: no ACK after retries for %s", recipient_key)
            return False
        except Exception:
            log.exception("Error sending message to %s", recipient_key)
            return False
        finally:
            await self._resume_auto_fetch()

    async def get_repeaters(self) -> list[dict]:
        if not self._mc:
            return []
        await self._pause_auto_fetch()
        try:
            result = await self._mc.commands.get_contacts(timeout=10)
            if result is None or result.type == EventType.ERROR:
                log.warning("Failed to fetch contacts: %s", result)
                return []
            contacts = list((result.payload or {}).values())
            repeaters = []
            for c in contacts:
                if c.get("type") != 2:
                    continue
                lat = c.get("adv_lat", 0)
                lon = c.get("adv_lon", 0)
                if lat == 0 and lon == 0:
                    continue
                repeaters.append({
                    "public_key": c["public_key"],
                    "name": c.get("adv_name", "Unknown"),
                    "lat": lat,
                    "lon": lon,
                    "path_len": c.get("out_path_len", -1),
                })
            log.info("Found %d repeaters with GPS out of %d contacts", len(repeaters), len(contacts))
            return repeaters
        except Exception:
            log.exception("Error fetching repeaters")
            return []
        finally:
            await self._resume_auto_fetch()

    async def set_message_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def request_position(
        self, node_key: str, progress_callback=None,
    ) -> PositionResult:
        pending = self._telemetry_pending.get(node_key)
        if pending:
            log.info("Telemetry already in progress for %s, waiting", node_key)
            await pending.wait()
            pos = self._last_position.get(node_key)
            if pos:
                return PositionResult(position=pos)
            return PositionResult(failure=PositionFailure.TIMEOUT)

        if not self._mc:
            log.warning("No MeshCore connection for telemetry request")
            return PositionResult(failure=PositionFailure.ERROR)
        contact = await self._find_contact_with_refresh(node_key)
        if not contact:
            log.warning("Contact not found for telemetry: %s", node_key)
            return PositionResult(failure=PositionFailure.ERROR)
        done_event = asyncio.Event()
        self._telemetry_pending[node_key] = done_event
        await self._pause_auto_fetch()
        try:
            adv_name = contact.get("adv_name", "?")

            # Build the per-request attempt plan from automatic routing (not a
            # stored setting). Each entry is (route, timeout, reset_first). When
            # the node's estimated drift since its last contact is small the
            # firmware likely still holds a valid cached route, so we give that
            # path TWO cached tries before escalating — a single transient
            # telemetry dropout shouldn't cost us the route. reset_path (which
            # wipes the firmware's cached out_path) is deferred to the flood
            # escalation, so a still-good path survives a lone miss.
            use_last_path = self._should_use_last_path(node_key)
            if use_last_path:
                plan = [
                    ("last_path", TELEMETRY_TIMEOUT_LAST_PATH, False),
                    ("last_path", TELEMETRY_TIMEOUT_LAST_PATH, False),
                    ("flood", TELEMETRY_TIMEOUT_FLOOD, True),
                ]
            else:
                plan = [
                    ("flood", TELEMETRY_TIMEOUT_FLOOD, True),
                    ("flood", TELEMETRY_TIMEOUT_FLOOD, True),
                ]

            for attempt, (route, timeout, reset_first) in enumerate(plan, start=1):
                # Base-camp-wide airtime gate: floods (and only floods) are spaced
                # out and suppressed when we're over our rolling TX budget. A
                # suppressed flood ends the plan — last-path attempts already ran.
                if route == "flood":
                    if not await self._reserve_flood():
                        log.info(
                            "Flood telemetry to %s suppressed by airtime budget "
                            "(TX %.2f%%)", node_key, self._governor.tx_duty_pct() or 0.0,
                        )
                        break
                log.info(
                    "Requesting telemetry from %s (%s) — attempt %d/%d (%s, %s, %ds)",
                    adv_name, node_key, attempt, len(plan), self._travel_mode, route, timeout,
                )
                if progress_callback:
                    await progress_callback(f"attempt_{attempt}", route, timeout)
                if reset_first:
                    await self._mc.commands.reset_path(contact)
                result = await self._try_telemetry(
                    node_key, contact, attempt=attempt, timeout=timeout, route=route,
                )
                if result:
                    self._record_successful_contact(node_key)
                    # Label this last-path decision for the learned model: the route
                    # delivered (position or GPS-off reply, i.e. not a companion
                    # error) — a win only if the delivering attempt was the cached
                    # path, a loss if we'd already escalated to flood. Errors carry
                    # no routing signal, so they're skipped.
                    if use_last_path and result.failure != PositionFailure.ERROR:
                        self._record_path_sample(node_key, success=(route == "last_path"))
                    return result

            log.warning("All %d telemetry attempts failed for %s", len(plan), node_key)
            self._last_position.pop(node_key, None)
            if use_last_path:
                # Cached route proven stale even after two tries — a last-path loss.
                self._record_path_sample(node_key, success=False)
                # Drop the route so the next request floods instead of wasting more
                # last-path attempts.
                self._route_state.pop(node_key[:12], None)
                self._save_route_state()
            return PositionResult(failure=PositionFailure.TIMEOUT)
        finally:
            await self._resume_auto_fetch()
            self._telemetry_pending.pop(node_key, None)
            self._route_decision.pop(node_key, None)
            done_event.set()

    async def _try_telemetry(
        self, node_key: str, contact: dict, attempt: int = 1,
        timeout: float = TELEMETRY_TIMEOUT_FLOOD, route: str = "flood",
    ) -> PositionResult | None:
        t0 = time.monotonic()
        try:
            result = await self._mc.commands.req_telemetry_sync(
                contact, timeout=0, min_timeout=timeout,
            )
        except Exception:
            log.exception("Telemetry request error for %s", node_key)
            self._log_telemetry_timing(node_key, attempt, time.monotonic() - t0, False, "error", route)
            return PositionResult(failure=PositionFailure.ERROR)
        elapsed = time.monotonic() - t0
        if result is None:
            self._log_telemetry_timing(node_key, attempt, elapsed, False, "timeout", route)
            return None
        log.info("Telemetry for %s: %s", node_key, result)
        for entry in result:
            if entry.get("type") == "gps":
                val = entry.get("value", {})
                lat = val.get("latitude")
                lon = val.get("longitude")
                if lat is not None and lon is not None:
                    self._last_position[node_key] = (lat, lon)
                    self._log_telemetry_timing(node_key, attempt, elapsed, True, "", route)
                    return PositionResult(position=(lat, lon))
        log.warning("Telemetry missing GPS for %s: %s", node_key, result)
        self._last_position.pop(node_key, None)
        self._log_telemetry_timing(node_key, attempt, elapsed, False, "no_gps", route)
        return PositionResult(failure=PositionFailure.NO_GPS)

    def _log_telemetry_timing(self, node_key: str, attempt: int, elapsed: float, success: bool, reason: str = "", route: str = "") -> None:
        entry = {
            "ts": int(time.time()),
            "node": node_key[:8],
            "attempt": attempt,
            "elapsed_s": round(elapsed, 1),
            "success": success,
        }
        if route:
            entry["route"] = route
            entry["mode"] = self._travel_mode
            # Automatic-routing inputs, so D_DEFAULT can be tuned from real data.
            disp, speed = self._route_decision.get(node_key, (None, None))
            if disp is not None:
                entry["displacement_mi"] = round(disp, 3)
            if speed is not None:
                entry["speed_mph"] = round(speed, 1)
        if reason:
            entry["reason"] = reason
        log.info("Telemetry timing: %s", entry)
        try:
            log_path = self._telemetry_log_path
            if not log_path:
                return
            with open(log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
            self._trim_telemetry_log(log_path)
        except Exception:
            log.debug("Could not write telemetry timing log", exc_info=True)

    def _trim_telemetry_log(self, path: str) -> None:
        try:
            with open(path, "r") as f:
                lines = f.readlines()
            if len(lines) > TELEMETRY_LOG_MAX_ENTRIES:
                with open(path, "w") as f:
                    f.writelines(lines[-TELEMETRY_LOG_MAX_ENTRIES:])
        except Exception:
            pass

    async def _create_connection(self) -> MeshCore:
        if self._connection_type == "wifi":
            mc = await MeshCore.create_tcp(self._host, self._port)
        elif self._connection_type == "usb":
            mc = await MeshCore.create_serial(self._serial_port, self._baud_rate)
        elif self._connection_type == "ble":
            mc = await MeshCore.create_ble(
                address=self._ble_address,
                pin=self._ble_pin or None,
            )
        else:
            raise ValueError(f"Unknown connection type: {self._connection_type}")
        if mc is None:
            raise ConnectionError("Failed to connect to companion node")
        return mc

    async def _initialize_session(self) -> None:
        """Run appstart, load contacts, subscribe to messages, start fetching."""
        result = await self._mc.commands.send_appstart()
        if result.type == EventType.ERROR:
            raise ConnectionError(f"Failed to initialize device: {result.payload}")
        log.info("Device initialized: %s", result.payload.get("name", "unknown"))

        await self._refresh_contacts()
        self._mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_message)
        self._mc.subscribe(EventType.DISCONNECTED, self._on_disconnect)
        await self._mc.start_auto_message_fetching()

    async def _on_disconnect(self, event) -> None:
        if self._shutting_down:
            return
        reason = event.payload.get("reason", "unknown") if event.payload else "unknown"
        log.warning("Connection lost: %s", reason)

        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        delay = RECONNECT_BASE_DELAY
        attempt = 0
        while not self._shutting_down:
            attempt += 1
            log.info("Reconnect attempt %d (waiting %.0fs)", attempt, delay)
            await asyncio.sleep(delay)

            if self._shutting_down:
                return

            try:
                if self._mc:
                    try:
                        await self._mc.stop_auto_message_fetching()
                    except Exception:
                        pass
                    try:
                        await self._mc.disconnect()
                    except Exception:
                        pass

                self._mc = await self._create_connection()
                await self._initialize_session()

                log.info("Reconnected successfully after %d attempts", attempt)
                return
            except Exception as e:
                log.warning("Reconnect attempt %d failed: %s", attempt, e)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)

    def _connection_desc(self) -> str:
        if self._connection_type == "wifi":
            return f"TCP {self._host}:{self._port}"
        elif self._connection_type == "ble":
            return f"BLE {self._ble_address}"
        return f"serial {self._serial_port}"

    async def _on_message(self, event) -> None:
        if not self._handler:
            return
        payload = event.payload
        snr = payload.get("SNR")
        rssi = payload.get("RSSI")

        msg = IncomingMessage(
            sender_key=payload.get("pubkey_prefix", ""),
            text=payload.get("text", ""),
            snr=snr,
            rssi=rssi,
            hops=payload.get("path_len"),
            # MeshCore names this "sender_timestamp" (the sender's compose time),
            # which is stable across flood retransmits — the firmware hashes
            # sender_timestamp + text for message identity. Used for dedup.
            timestamp=payload.get("sender_timestamp"),
        )

        log.info(
            "Received from %s: %s (SNR=%.1f dB, hops=%s)",
            msg.sender_key,
            msg.text[:50],
            msg.snr if msg.snr is not None else 0,
            msg.hops,
        )

        try:
            response = await self._handler(msg)
        except Exception:
            log.exception("Error processing command from %s", msg.sender_key)
            response = "SERVER ERROR\nTry again shortly."

        if response:
            log.info("Sending reply to %s (%d chars)", msg.sender_key, len(response))
            try:
                ok = await self.send_message(msg.sender_key, response)
                log.info("Reply sent to %s: %s", msg.sender_key, "OK" if ok else "FAILED")
            except Exception:
                log.exception("Error sending reply to %s", msg.sender_key)

    def congested(self) -> bool:
        """True when the channel is busy enough that the engine should back the
        per-player survey cadence off. Delegates to the airtime governor."""
        return self._governor.congested()

    async def _export_contact_uri_locked(self) -> str | None:
        """Fetch the node's own shareable contact card as a `meshcore://…` URI.
        EXPORT_CONTACT is a local companion query (it returns the node's advert
        card — key, timestamp, flags, name — not a mesh broadcast, unlike
        SHARE_CONTACT), so it costs no radio airtime. Must be called with
        auto-fetch already paused — it does not manage the pause itself."""
        if not self._mc:
            return None
        try:
            result = await self._mc.commands.export_contact()
        except Exception:
            log.debug("Contact URI export failed", exc_info=True)
            return None
        if not result or result.type == EventType.ERROR:
            return None
        return (result.payload or {}).get("uri")

    async def get_contact_uri(self) -> str | None:
        if not self._mc:
            return None
        await self._pause_auto_fetch()
        try:
            return await self._export_contact_uri_locked()
        finally:
            await self._resume_auto_fetch()

    async def _sample_airtime_now(self) -> None:
        """Feed the governor one fresh cumulative-counter snapshot from local
        companion queries (no radio airtime). Must be called with auto-fetch
        already paused — it does not manage the pause itself."""
        if not self._mc:
            return
        try:
            radio = await self._mc.commands.get_stats_radio()
            core = await self._mc.commands.get_stats_core()
        except Exception:
            log.debug("Airtime sample query failed", exc_info=True)
            return
        if not radio or radio.type == EventType.ERROR:
            return
        if not core or core.type == EventType.ERROR:
            return
        self._governor.observe(
            uptime_secs=core.payload.get("uptime_secs"),
            tx_air_secs=radio.payload.get("tx_air_secs"),
            rx_air_secs=radio.payload.get("rx_air_secs"),
        )

    async def _reserve_flood(self) -> bool:
        """Base-camp-wide gate for an outbound flood. Refreshes the airtime
        sample if stale, then returns False when we're over our rolling TX budget
        (caller must skip the flood) or True after enforcing the global minimum
        spacing between floods."""
        if self._governor.last_sample_age() is None or (
            self._governor.last_sample_age() or 0
        ) > SAMPLE_MAX_AGE_S:
            await self._sample_airtime_now()
        return await self._governor.reserve_flood()

    async def _pause_auto_fetch(self) -> None:
        if self._mc:
            try:
                await self._mc.stop_auto_message_fetching()
            except Exception:
                pass

    async def _resume_auto_fetch(self) -> None:
        if self._mc:
            try:
                await self._mc.start_auto_message_fetching()
            except Exception:
                log.exception("Failed to resume auto message fetching")

    async def _refresh_contacts(self) -> None:
        if not self._mc:
            return
        result = await self._mc.commands.get_contacts()
        if result.type != EventType.ERROR:
            self._contacts = result.payload or {}
            self._last_contact_refresh = time.time()
            log.info("Loaded %d contacts", len(self._contacts))

    async def _find_contact_with_refresh(self, key_prefix: str) -> dict | None:
        contact = self._find_contact(key_prefix)
        if contact:
            return contact
        # Cache miss. The contact we need to address — typically the sender we're
        # about to reply to — may have been added to the firmware after our last
        # sync, so re-query (cheap, local, no mesh airtime). The short debounce
        # keeps a truly-absent key from re-querying on every retry, but is small
        # enough that a first-command-after-startup reply still resolves instead
        # of going silent for the old 5-minute window.
        if time.time() - self._last_contact_refresh > CONTACT_MISS_REFRESH_INTERVAL:
            await self._refresh_contacts()
            return self._find_contact(key_prefix)
        return None

    def get_contacts(self) -> dict:
        return dict(self._contacts)

    async def get_companion_status(self) -> dict:
        if not self._mc:
            return {"connected": False, "configured": self._configured}
        status: dict = {"connected": True, "connection": self._connection_desc()}
        si = self._mc.self_info
        if si:
            status["node_name"] = si.get("name", "")
            if si.get("public_key"):
                status["public_key"] = si["public_key"]
        await self._pause_auto_fetch()
        try:
            # All of the following are local companion queries — no radio
            # airtime, no mesh round-trip — so they're cheap to poll here.
            # The node's own contact card, as the `meshcore://…` URI the app's
            # add-contact scanner expects (the QR must encode this, not the bare
            # public key). Local export, so no airtime.
            status["contact_uri"] = await self._export_contact_uri_locked()
            try:
                radio = await self._mc.commands.get_stats_radio()
                if radio and radio.type != EventType.ERROR:
                    p = radio.payload
                    status["noise_floor"] = p.get("noise_floor")
                    status["last_rssi"] = p.get("last_rssi")
                    status["last_snr"] = p.get("last_snr")
                    status["tx_air_secs"] = p.get("tx_air_secs")
                    status["rx_air_secs"] = p.get("rx_air_secs")
            except Exception:
                log.exception("Failed to get radio stats")
            try:
                core = await self._mc.commands.get_stats_core()
                if core and core.type != EventType.ERROR:
                    p = core.payload
                    status["uptime_secs"] = p.get("uptime_secs")
                    status["errors"] = p.get("errors")
                    status["queue_len"] = p.get("queue_len")
                    # Core stats also carry battery; prefer it and skip get_bat.
                    if p.get("battery_mv") is not None:
                        status["battery_mv"] = p.get("battery_mv")
            except Exception:
                log.exception("Failed to get core stats")
            try:
                packets = await self._mc.commands.get_stats_packets()
                if packets and packets.type != EventType.ERROR:
                    p = packets.payload
                    status["recv"] = p.get("recv")
                    status["recv_errors"] = p.get("recv_errors")
            except Exception:
                log.exception("Failed to get packet stats")
            if "battery_mv" not in status:
                try:
                    bat = await self._mc.commands.get_bat()
                    if bat and bat.type != EventType.ERROR:
                        status["battery_mv"] = bat.payload.get("battery_mv")
                except Exception:
                    log.exception("Failed to get battery")
        finally:
            await self._resume_auto_fetch()
        # Feed the airtime governor from the same counters the UI poll already
        # gathered — free rolling samples whenever the dashboard is open.
        self._governor.observe(
            uptime_secs=status.get("uptime_secs"),
            tx_air_secs=status.get("tx_air_secs"),
            rx_air_secs=status.get("rx_air_secs"),
        )
        status["uptime_display"] = _fmt_uptime(status.get("uptime_secs"))
        status["health_metrics"] = derive_mesh_health(status)
        return status

    async def reboot_companion(self) -> bool:
        if not self._mc:
            return False
        try:
            await self._mc.commands.reboot()
            return True
        except Exception:
            log.exception("Failed to reboot companion")
            return False

    def get_connection_config(self) -> dict:
        return {
            "connection_type": self._connection_type,
            "companion_host": self._host,
            "companion_port": self._port,
            "serial_port": self._serial_port,
            "ble_address": self._ble_address,
            "ble_pin": self._ble_pin,
            "configured": self._configured,
        }

    @staticmethod
    async def scan_ble(timeout: float = 5.0) -> list[dict]:
        try:
            from bleak import BleakScanner
            devices = await BleakScanner.discover(timeout=timeout)
            return [
                {"address": d.address, "name": d.name or "Unknown"}
                for d in devices
                if d.name and "meshcore" in d.name.lower()
                or d.name and "mesh" in d.name.lower()
                or d.name and "lora" in d.name.lower()
                or not d.name
            ]
        except Exception:
            log.exception("BLE scan failed")
            return []

    def _find_contact(self, key_prefix: str) -> dict | None:
        if key_prefix in self._contacts:
            return self._contacts[key_prefix]
        for key, contact in self._contacts.items():
            if key.startswith(key_prefix) or key_prefix.startswith(key):
                return contact
        return None
