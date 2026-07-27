# Car Mode / Auto-Survey (2026-07-18)

LoRa the Explorer is played mostly from a moving vehicle, where driving is the
legitimate way to extend mesh coverage. This feature removes the friction (and
distraction) of touching the phone while driving, and retires the manual
Walking/Driving travel toggle in favor of automatic routing.

## Automatic telemetry routing

A cached firmware ("last path") route breaks when the node leaves the coverage
cell of its entry repeater — a **distance** effect, not a time one. Rather than
ask the player whether they're walking or driving, the adapter estimates how far
the node has drifted since its route was learned and decides accordingly:

- Each successful telemetry exchange records the node's GPS fix and timestamp
  (`_pos_history`, up to the two most recent per node, persisted in
  `route_state.json`).
- `_estimate_displacement_miles` = node speed (from the last two fixes) × time
  since the last contact.
- `_should_use_last_path`: flood if there's no learned route; otherwise use
  last-path only while the estimated drift stays under the player's path-reach
  distance `D` (see below), else flood. With too few fixes to estimate speed
  (e.g. GPS off), it falls back to a plain recency window (`WINDOW_FALLBACK`).

This generalizes the old hand-tuned behavior: at highway speed the ~38s hex
cadence already exceeds 0.5 mi (so it floods, as the old `WINDOW_DRIVING=0` did),
while a walker drifts under it for minutes (so it keeps using last-path). A wrong
guess costs one last-path attempt, then floods and re-learns. `telemetry_timing.jsonl`
logs `displacement_mi` and `speed_mph` per attempt.

### Learned per-install path-reach (`_learned_D`)

`D` starts at `D_DEFAULT_MILES` (0.5 mi) but is fit per install from the player's
own history. Every last-path attempt is one labeled sample — its estimated
displacement and whether the cached route delivered — appended to
`path_model.jsonl` (rolling window, recency-weighted with a 1-week half-life).
`_learned_D` picks the `D` that maximizes the payoff of the "use last-path when
`disp < D`" policy: each past attempt it would have tried counts +weight if the
route delivered, −weight if it was wasted. So `D` extends out only to where
last-path actually tends to succeed and pulls in where it doesn't — all-failure
history → floor (0.15 mi), all-success → out to the furthest observed drift
(capped at 1.0 mi). Cold-start and thin data fall back to the 0.5 mi default,
which also seeds the exploration that generates the first samples. There is no
player-facing path-reach stat.

Routing constants and logic live in `radio/meshcore_adapter.py`. `GET /api/travel-mode`
remains as read-only routing diagnostics; there is no settable travel mode.

## Uniform survey rate cap

`SURVEY_MIN_INTERVAL_S` (35s, in `game/engine.py`) is the minimum spacing between
successful surveys — applied to every survey, manual tap or auto. It sits at the
flood round-trip time, so a walker (hexes minutes apart) never feels it, but a
fast mover can't out-tap the hands-free auto-surveyor. This removes any incentive
to touch the phone while driving without measuring speed or guessing driver vs
passenger. A too-soon manual survey gets a gentle "transmitter cooling" message;
a too-soon **auto** survey is a silent no-op (no telemetry, no feed spam).

## Hands-free auto-survey

The Radio page has an **Auto-Survey** toggle (off by default, gated on a secure
context since it needs a live GPS watch). When on, entering a "Survey Ready" hex
fires a survey automatically — the safest behavior (don't touch the phone) is also
the rewarded one. A client-side debounce mirrors the server rate cap, a per-hex
guard prevents double-firing, an ambient chime + session counter confirm each
auto survey, and a one-time disclaimer sets the safety expectation. Surveys always
pay full rewards regardless of speed, and position always comes from node telemetry
(never the phone), so this is spoof-resistant. See `web/templates/spyglass.html`.

### Idle stand-down

A forgotten-on switch keeps the high-accuracy GPS watch (and wake lock) alive and
drains the battery, so Auto-Survey disarms itself after `AUTO_SURVEY_IDLE_MS`
(**30 min**) with no *logged* survey. The countdown resets only on real progress —
a confirmed `autosurvey_logged` event — and pointedly **not** on every GPS fix, so a
stationary phone still times out while anyone actually moving stays armed. 30 min is
a deliberate upper bound: at H3 res 8 (~0.5 mi across the flats) even slow hiking
(1.5–3.5 mph) crosses a fresh hex every ~9–20 min, so only sub-1-mph "movement" —
i.e. genuinely stopped — trips it, and it never cuts off a real hiker. On stand-down
the switch clears (persisted `lora-autosurvey=false`), the pilot lamp goes dark, and
a one-off note posts to the radio feed. Arming/clearing is centralized in
`updateAutoSurveyStatus()`, which starts the timer only when none is running so the
per-fix calls can't reset it; `bumpAutoSurveyCount()` restarts it on each logged
survey. This is why the feature keeps the name **Auto-Survey** rather than "Car
Mode" — it is full-reward and equally valid on foot, so a car-specific name would
mis-steer players.
