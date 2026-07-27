# Combat Revision & Storage Architecture (2026-07-11)

**Status:** Design proposal — NOT yet implemented. Resolves taskwarrior `lora` #16 (attack gating) and #15 (outpost-screen consolidation).

**Relationship to [`multiplayer-design.md`](multiplayer-design.md):** This supersedes the combat sections of that doc where they conflict. It keeps that doc's good bones — travel time, passive-leaning defense, resolve-at-resolution snapshot, multi-attacker diminishing returns, Probe scouting, coarse-cell travel bands, location privacy — and revises the pieces that were either stale (influence-committed attacks; the live system moved to item-based) or clunky in playtest reasoning (the survey-to-open-a-window gate).

---

## 1. The problem we were solving (#16)

The live system gates attacks by requiring a player to physically **survey through their own outpost** to open a 30-minute attack window (an Ingress-style "go there to act" mechanic). At our scale — ≤3 outposts per player, all ≥3 miles from home — that's a big physical trek to unlock a small action, and it stacks on top of two gates we *already* have (attack items + marks cost). It felt like a toll on top of a toll.

**Core reframe:** physical activity should govern **supply, not per-attack permission.** You survey to earn attack items (ammo); you spend that ammo whenever you like. The survey-window gate is dropped.

## 2. The combat loop (revised)

**Recon → Compose → Dispatch → Travel → Resolve-at-arrival.**

1. **Recon (Probe).** Spend a Probe to reveal a target's *exact HP and permanent defense.* Un-probed targets show only coarse intel ("heavily fortified"). This turns the attack from a blind bid into a solvable tactical puzzle.
2. **Compose an atomic raid party.** Assign **multiple** attack items to one raid (not the live system's single-item attack). A **live damage preview** projects the outcome against *currently visible* defense as you add items:
   > Raid party: 3× uncommon, 1× epic → Projected: **412 / 400 HP → RAZED** (11 to spare)

   The preview is the load-bearing fun-fix: it converts "combat via eBay" (blind over/underkill dread) into a loadout-optimization puzzle. Overkill and underkill both become *visible choices*, not gambles.
3. **Dispatch — items committed here, atomically.** The whole raid is **one bundle / one Worker (DO) resolution regardless of item count.** This is what preserves the "hoard everything and throw it all to wipe an outpost" fantasy while keeping writes bounded.
4. **Travel time** (see §3) — the raid is in flight for hours.
5. **Resolve at arrival, against defense-at-arrival** (see §4). Attacker gets an after-action report; defender gets one clean notification.

**Resolution is deterministic** (no RNG in the damage roll — fits the seeded-PRNG ethos). Tension lives in item scarcity, opportunity cost, information (Probe), timing, and the defender's *hidden* reactive reserve — not dice.

## 3. Travel time is the counterplay spine (not flavor)

Travel time is promoted from optional flavor to the mechanism that makes defender agency possible **and** spaces writes. Scaled by real distance between the attacker's and target's coarse H3 cells (privacy preserved — no exact GPS to the Worker).

- **Min ~1 hour, max ~12 hours** (tunable). The floor guarantees every raid gives at least a token reaction window past the 60s defense poll; the cap keeps raids same-day.
- **Near = cheaper, shorter warning, easier surprise-kill. Far = pricier, longer warning, more prep time.** A genuine risk/reward axis for the attacker, and it mimics real raids. (Distance may also apply a small "expedition attrition" damage falloff — optional effectiveness knob, add only if #1/#2 don't create enough texture.)
- The defender learns of an inbound raid via the existing **60s defense poll** — hours-long ETA ≫ 60s, so latency is a non-issue.

## 4. Defense model (revised — the real fix for counterplay-at-the-ceiling)

**Problem:** a maxed outpost (max level + best permanent defense installed, full HP) has *nothing to upgrade to* when the horn sounds. Upgrade-based reinforcement gives zero agency to exactly the most-invested player. Emergency HP repair also does nothing at full HP.

**Insight:** the offense/defense asymmetry *is* the bug. Offense is a **consumable hoard** (no ceiling); defense was a **permanent slot** (hard ceiling). Fix: give defense a consumable reactive layer too.

**Two layers:**

- **Permanent installed item = damage reduction** — your standing wall quality (keeps the live `power − floor(defense×0.5)` model). This is your *floor.*
- **Temporary boosts = flat HP** — the reactive, ceiling-less layer. Same defense items you already drop, deployed as timed buffs:
  - **Flat HP, not damage reduction.** Flat HP with diminishing returns can *never* create an impenetrable wall — enough total firepower always drains it. Damage-reduction stacking trends toward invincibility, which is why we avoid it here.
  - **Diminishing returns** per stacked boost (first +HP big, each subsequent smaller). Caps any single outpost's survivability so a committed/coordinated attacker can always break through with enough.
  - **Don't regen; soaked first.** Boost HP absorbs damage and doesn't come back; base HP is what regens. Combat stays a clean race of *total incoming damage vs. total HP.*
  - **Duration:** start with "lasts as long as the longest travel time (~12h)" so a boost raised the instant *any* warning appears is guaranteed live at impact — no precision-timing required. If defense feels oppressive in playtest, switch to **consume-on-hit** (persists up to 12h *or* until it absorbs one raid), which forces a fresh boost per raid and drains the hoard faster (more offense-favorable).
  - **Hidden from the attacker.** The Probe reveals the permanent wall, never the reserve or *whether* the defender will spend it. So the damage preview can never promise a guaranteed raze against an active, stocked defender — this is what kills "the attacker precalculated my obliteration."

- **Besieged state:** an attacked outpost pauses passive HP regen for ~12–24h. Underkill becomes *phase one of a siege* (soften today, finish tomorrow), not wasted firepower. Combined with the per-target cooldown, the natural rhythm is multi-hour, not multi-day.

**Resolution timing rule (critical):** damage resolves **at arrival, against whatever is active then** — permanent wall + live boosts + current HP. This is the single rule that makes the reaction window matter.

**Power curve this produces:** investment + attentiveness + reserves = *survivable, never invincible.* Good defender beats lazy attacker; committed/coordinated attacker still beats an unprepared or AFK one (an AFK defender spends no reserves → visible wall only → precalculated raze works, which correctly makes engagement the price).

## 5. Threat readout (defender's decision support)

The defender's version of the attacker's Probe — symmetric fog. On an inbound raid, show a **coarse, banded, outcome-framed** assessment so the burn-boosts-or-give-up call is informed, not a blind gamble:

- Framed as *stakes*, not raw size: "A war band approaches — **projected to raze** your outpost" / "heavy damage" / "you'll hold."
- **No exact numbers, never the composition** (preserves the attacker's fog).
- **No live post-boost recalculator** — show pre-boost stakes only; the defender judges how much to commit under diminishing returns. Symmetric with the attacker (who sees the pre-reaction preview but not whether the defender reacts). This symmetry is what stops *either* side from becoming a solved checkbox.

## 6. Throttling — no artificial daily cap

The daily-attack-cap idea is dropped (feels clunky). Replaced by **diegetic** limits that also bound writes:

- **One raiding party in flight at a time** ("your war-band is deployed; you can't launch another until it returns"). This makes travel time *become* the throttle (max raids/day ≈ 24h ÷ travel time), and it forces **coordination for pile-ons** — one player can't stack simultaneous raids on a target; they need partners. That's the "coordination is real effort" property we want.
- **Per-target 24h cooldown** (keep) — stops re-hitting the same outpost.
- **Item + marks cost** (keep) — economic throttle.
- **Multi-attacker diminishing returns** (keep from the base doc) — pile-ons are inefficient.

## 7. #15 — HP/defense on the Outpost screen

**Decision: keep them on the Outpost screen; add a read-only roll-up on the multiplayer screen.**

HP and defense are intrinsic properties of a specific outpost (like a unit's health belongs on the unit) — the outpost screen is their natural, most-intuitive home. The gap isn't "wrong place," it's "no single at-a-glance PvP status view." So: keep contextual detail on each Outpost screen (where you act), and **mirror** a read-only per-outpost summary (HP bar, installed defense, under-attack/inbound flag) on the multiplayer screen. Multiplayer screen = "am I under attack / roster / leaderboard / attack others"; Outpost screen = "manage this specific holding."

---

## 8. Storage architecture: migrate write-heavy state KV → Durable Objects

The revised combat model (near-real-time raids, couch-attackers, active defense) is write-heavy, and **KV is the wrong primitive for it** — KV is built for read-heavy, write-rare data. Cloudflare KV free tier caps at **1,000 writes/day** (~8 active players). SQLite-backed Durable Objects, **available on the Workers Free plan**, cap at **100,000 rows-written/day** — a 100× lift for $0.

### Free-tier limits (verified 2026-07-11)

| Limit (Workers Free) | KV | SQLite-backed Durable Object |
|---|---|---|
| Writes/day | **1,000** | **100,000 rows written** |
| Reads/day | 100,000 | 5,000,000 rows read |
| Lists/day | 1,000 | n/a (use SQL `SELECT`) |
| Requests/day | — | 100,000 |
| Compute | — | 13,000 GB-s/day |
| Storage | 1 GB | 5 GB/account, 10 GB/object — **free plan not billed for storage** |
| Object count | — | **Unlimited instances**; 100 namespaces/classes (free) |

Sources: [DO pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/), [DO limits](https://developers.cloudflare.com/durable-objects/platform/limits/), [DO on Free plan](https://developers.cloudflare.com/changelog/post/2025-04-07-durable-objects-free-tier/).

### Critical caveat: quotas are account-wide, not per-object

Unlimited objects does **not** mean unlimited budget. The daily quotas (100K writes, 5M reads, 100K requests, 13K GB-s, 5 GB) are a **single shared pool across every DO.** Sharding into many objects buys **concurrency and isolation** (each DO is single-threaded; separate objects don't block each other, each stays under its own 10 GB / 1,000-req/s ceiling) — *not* more quota.

### All-DO vs. split KV+DO: go all-DO

For raw scalability they **tie**, so simplicity wins. Every operation is a **Worker request** (Workers Free ~100K/day) regardless of backend — that Worker ceiling is the true bottleneck, and DO's storage pools (100K writes, 5M reads) are far larger than it, so they never bind first. Splitting reads back to KV would spend from two pools but wouldn't raise the Worker ceiling. **Verdict: all mutable game state → DO; static reference data → code/local cache; retire KV for game state.**

The one caveat: keep **DO fan-out low** — design so a player's poll touches *one* DO (their own Player DO, which caches their outposts' status), not N, so DO requests stay ~1:1 with Worker requests.

### Decision rule

KV's only scarce limits are **writes (1K)** and **lists (1K)**. So: frequent writes → DO; needs enumeration → DO (SQL, no list cap); needs transactions/timers → DO (alarms); read-mostly static → code/KV.

### Per-mechanic mapping

| Mechanic | Today | Move to | Why |
|---|---|---|---|
| Outpost HP / defense / in-flight raids / boosts | KV | **Per-outpost DO** | Write-heavy; **alarms** resolve raids at arrival; transactional (kills snapshot races) |
| Daily survey-cap counter | KV META (48h TTL) | **Per-player DO** | Up to **50 writes/player/day** — the biggest hidden KV write drain. Expire via alarm instead of TTL |
| Item inventory (Worker-authoritative) | KV | **Per-player DO** | Rewritten every bundle push |
| Merchant weekly-purchase counters | KV | **Per-player DO** | Occasional writes; lives with the player |
| Signed action bundles / public ledger | KV | **Per-player DO (append)** | Append-heavy; federation reads served through the Worker |
| Multiplayer settings / webhook URL | KV | Per-player DO (or leave) | Rare writes — fine either way |
| Leaderboard / player-index / renown standings | KV index key | **One "registry" DO** | SQL-query standings — eliminates the KV-list problem and index-maintenance writes |
| Item catalog / drop tables / shop config | code/KV | **Keep in code / local cache** | Static, read-only — never make the network request at all |

### Recommended topology — 3 DO classes

1. **`Outpost` DO** (one per outpost) — combat state + raid-resolution **alarms** (each outpost self-schedules resolution at arrival; replaces the global cron scan).
2. **`Player` DO** (one per player) — inventory, survey-cap counter, purchases, settings, ledger append.
3. **`Registry` DO** (one) — leaderboard/standings via SQL. *(Caveat: concentrates leaderboard reads on one hot object — fine at this scale, well under 1,000 req/s.)*

3 of 100 namespaces used. KV ends up doing almost nothing.

### New ceiling after migration

Writes stop binding (~100K/day ≈ hundreds of active players). The binding constraint becomes **~100K requests/day**, dominated by the **60s defense poll** (~1,440/player/day) → roughly **50+ concurrent active players** before you'd lengthen/adapt the poll interval or move to paid ($5/mo → 1M+). A ~10× lift over KV's ~8, on the free plan, for $0.

### The real scalability levers (none are KV-vs-DO)

1. Keep static data **off the network entirely** (code / local SQLite cache) — a Worker request you never make.
2. **Minimize DO fan-out** per request (one-DO-per-interaction).
3. **Reduce request frequency** — adapt/lengthen the defense poll. This buys more than any storage choice; it's the first move when approaching the ceiling, then paid — *not* resurrecting KV.

### Migration is a scoped refactor, not a rewrite

Extract the combat + per-player mutable state from the KV-backed Worker into DO classes with migrations and alarms. Do the **state-coalescing** (one record/row per outpost/player) as part of the DO schema so writes stay minimal regardless. This is its own task (see #NEW).

---

## 9. Open tuning knobs (playtest)

- Travel-time min/max (1h / 12h starting point) and distance→ETA/cost/attrition curve.
- Boost duration model: fixed-12h vs. consume-on-hit; diminishing-returns curve; per-outpost stack cap.
- Besieged-state regen-pause duration (~12–24h).
- Per-target cooldown vs. boost duration consistency (the "attack into the buff vs. wait it out" dynamic is a coordination/second-attacker game unless cooldown < boost).
- Threat-readout band thresholds.
- Defense poll interval (the scaling dial).
