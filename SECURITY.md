# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report privately via either:

- **GitHub** → the repository's *Security* tab → *Report a vulnerability* (preferred)
- **[Discord](https://discord.gg/EHXemsA2SS)** → direct message the maintainer

Please include what the issue is, how to reproduce it, and what an attacker gains.

### What to expect

This is a hobby project maintained by one person — there is no bounty and no SLA. Realistically:
acknowledgement within a few days, and a fix for anything serious as fast as is practical.
Credit in the release notes if you'd like it.

## What is in scope

Especially interested in anything that:

- **Leaks precise location.** Any path by which a player's real coordinates, real H3 hex IDs, or
  survey history escape their own install — this is the project's most important invariant.
- Bypasses dashboard authentication (password or OIDC), or allows session forgery.
- Forges or replays signed requests to the multiplayer service, or lets one player act as another.
- Allows unauthenticated access to admin endpoints on the multiplayer service.
- Achieves remote code execution, SQL injection, or path traversal in the game server.

## What is out of scope

- **Gameplay cheating by the owner of an install.** The client is open source and player-hosted,
  and GPS never leaves the device. Spoofing your own GPS or inflating your own local XP is a known,
  accepted, architecturally unsolvable limitation — it is bounded by server-side caps and manual
  review, not prevented. Please don't report it as a vulnerability.
- Issues requiring physical access to a player's own hardware or database.
- **LoRa mesh traffic being observable.** Mesh routing is public by design. Assume anything sent
  over the air is visible to anyone in radio range.
- Denial of service through radio spam or bandwidth exhaustion on someone's own mesh.
- Vulnerabilities in Cloudflare, MeshCore firmware, or third-party dependencies — report those
  upstream (do tell us if the project's usage makes them materially worse).

## Notes for people self-hosting

- **You are responsible for your own instance.** The dashboard holds your full location history.
- **Do not expose the dashboard directly to the internet.** Put it behind a VPN, a reverse proxy
  with TLS, or an authenticating proxy. Password auth plus rate limiting is a lock on the door, not
  a public-internet-grade defence.
- **Set a strong password, or use OIDC SSO** (PocketID / Authentik / Authelia are supported).
- **Backups are sensitive.** They contain every coordinate you've ever surveyed. Store and share
  them accordingly.
- Keep the container updated — security fixes ship in the image on `main`.

## Supported versions

Only the latest `main` / `:latest` image is supported. There are no backported fixes.
