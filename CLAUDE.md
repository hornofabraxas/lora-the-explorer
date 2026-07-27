# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

LoRa the Explorer is a location-based exploration RPG played over LoRa mesh radio networks. Players physically explore areas with handheld LoRa devices, sending commands via mesh radio to a game server (typically a Raspberry Pi) that tracks their progress through a web dashboard.

## Commands

```bash
# Run tests (206 tests, ~0.4s)
.venv/bin/python -m pytest

# Run a single test file or test
.venv/bin/python -m pytest tests/test_engine.py
.venv/bin/python -m pytest tests/test_engine.py::test_survey_basic -v

# Run the preview server for UI development (no companion/radio needed)
.venv/bin/python preview_server.py  # serves on port 1493

# Run the full app (requires radio companion or mock)
.venv/bin/python -m lora_explorer

# Docker build
docker build -t lora-the-explorer .
```

## Architecture

### Data Flow

```
LoRa Radio → MeshCoreAdapter → GameEngine → Database (SQLite)
                                    ↓
                              SSE events → Web Dashboard (FastAPI + Jinja2)
```

Players send text commands (`/lora survey`, `/lora charter`, `/lora upkeep`) from their LoRa devices. The `MeshCoreAdapter` receives messages over TCP/serial/BLE from a MeshCore companion device. The `GameEngine` processes commands, validates GPS positions, calculates rewards, and persists state. The web dashboard receives live updates via SSE.

### Key Modules

- **`game/engine.py`** — Core game logic: survey processing, chartering posts, upkeep, XP/provision/field-note calculations, velocity anti-cheat, daily dispatches, contracts, postcards. All game constants and progression tables live here.
- **`game/database.py`** — SQLite via aiosqlite. Schema defined inline as `SCHEMA` string. All DB access goes through the `Database` class.
- **`game/commands.py`** — Parses `/lora <command>` text from radio messages into `ParsedCommand` objects.
- **`radio/adapter.py`** — Abstract `RadioAdapter` interface. `IncomingMessage` dataclass carries sender key, text, GPS coords, signal info.
- **`radio/meshcore_adapter.py`** — Concrete adapter using the `meshcore` library. Handles TCP/serial/BLE connections, GPS telemetry requests, repeater scanning.
- **`web/routes.py`** — FastAPI routes for the dashboard. Serves HTML pages, handles OIDC login, SSE event stream, API endpoints for post upgrades, resupply, contracts, merchant.
- **`web/auth.py`** — Password auth + session cookies via itsdangerous. Rate-limited login.
- **`web/oidc.py`** — OIDC SSO support (PocketID/Authentik/Authelia) via authlib.
- **`community/client.py`** — HTTP client for optional community server linking.

### Game Concepts

- **Hex grid**: H3 resolution 8 (~0.31 sq mi per hex). Players survey hexes to earn XP, provisions, and field notes.
- **Survey Posts**: Chartered at physical locations ≥3mi from home. Have levels (1-5), require periodic upkeep (7-day ruin timer), and provide survey multipliers.
- **Base Camp**: Levels 1-10, unlocked by spending provisions + field notes. Higher levels unlock more post slots and XP multipliers.
- **Velocity check**: `MAX_VELOCITY_MPH = 150` — rejects surveys implying impossible travel speed between consecutive GPS fixes.

### Testing Patterns

Tests use a `MockRadioAdapter` that captures sent messages and allows simulating incoming radio messages. Tests create an in-memory SQLite database (`:memory:`). The standard pattern:

1. Create `MockRadioAdapter` + `Database` + `GameEngine`
2. Set home location, simulate a survey message with GPS coords
3. Assert on the response string and database state

### Web UI

Jinja2 templates in `web/templates/`, static CSS in `web/static/`. The dashboard is a PWA-style single-server app. When changing CSS, bump the `style.css?v=N` cache version in `base.html`.

### Preview Server

`preview_server.py` runs a standalone FastAPI server on port 1493 with mock game data — no radio companion or database needed. Use this for UI development.
