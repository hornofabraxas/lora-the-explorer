import hashlib
import hmac
import logging
import time

import httpx

log = logging.getLogger(__name__)

TIMEOUT = 10.0


class WorkerClient:
    def __init__(self, base_url: str, player_id: str | None = None, secret: str | None = None,
                 invite_code: str | None = None):
        self._base_url = base_url.strip().rstrip("/")
        self._http = httpx.AsyncClient(base_url=self._base_url, timeout=TIMEOUT)
        self._player_id = player_id
        self._secret = secret
        # Optional invite code for a Worker that gates registration (REGISTER_SECRET).
        self._invite_code = invite_code

    async def close(self) -> None:
        await self._http.aclose()

    def set_credentials(self, player_id: str, secret: str) -> None:
        self._player_id = player_id
        self._secret = secret

    def _sign(self, body: str) -> dict[str, str]:
        timestamp = str(int(time.time()))
        message = self._player_id + timestamp + body
        signature = hmac.new(
            self._secret.encode(), message.encode(), hashlib.sha256,
        ).hexdigest()
        return {
            "X-Player-ID": self._player_id,
            "X-Timestamp": timestamp,
            "X-Signature": signature,
        }

    async def register(self, display_name: str,
                       invite_code: str | None = None) -> dict:
        try:
            payload = {"display_name": display_name}
            # A code typed into the UI wins; otherwise fall back to the env default
            # (WORKER_INVITE_CODE) for headless installs.
            code = invite_code or self._invite_code
            if code:
                payload["invite_code"] = code
            resp = await self._http.post("/api/register", json=payload)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            # Surface the Worker's message (e.g. the invite-code prompt) rather than
            # a bare HTTP status, so the UI can show the player why it failed.
            try:
                error_msg = exc.response.json().get("error", str(exc))
            except Exception:
                error_msg = str(exc)
            log.warning("Worker registration failed: %s", error_msg)
            return {"ok": False, "error": error_msg}
        except httpx.RequestError as exc:
            log.warning("Worker registration failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def push_bundle(self, bundle: dict) -> dict:
        try:
            import json
            body = json.dumps(bundle)
            headers = self._sign(body)
            headers["Content-Type"] = "application/json"
            resp = await self._http.post("/api/bundle", content=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.json()
                error_msg = detail.get("error", str(exc))
            except Exception:
                error_msg = str(exc)
            log.warning("Worker bundle push failed: %s", error_msg)
            return {"ok": False, "error": error_msg}
        except httpx.RequestError as exc:
            log.warning("Worker bundle push failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def scout(self, target_player_id: str, probe_item_id: str) -> dict:
        try:
            import json
            body = json.dumps({"target_player_id": target_player_id, "probe_item_id": probe_item_id})
            headers = self._sign(body)
            headers["Content-Type"] = "application/json"
            resp = await self._http.post("/api/scout", content=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            log.warning("Worker scout failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def install_item(self, post_token: str, item_id: str) -> dict:
        try:
            import json
            body = json.dumps({"post_token": post_token, "item_id": item_id})
            headers = self._sign(body)
            headers["Content-Type"] = "application/json"
            resp = await self._http.post("/api/defend/install", content=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            log.warning("Worker install_item failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def buy_item(self, item_type: str, purchase_id: str) -> dict:
        try:
            import json
            body = json.dumps({"item_type": item_type, "purchase_id": purchase_id})
            headers = self._sign(body)
            headers["Content-Type"] = "application/json"
            resp = await self._http.post("/api/shop/buy", content=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            try:
                error_msg = exc.response.json().get("error", str(exc))
            except Exception:
                error_msg = str(exc)
            log.warning("Worker buy_item failed: %s", error_msg)
            return {"ok": False, "error": error_msg}
        except httpx.RequestError as exc:
            log.warning("Worker buy_item failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def salvage_items(self, item_ids: list[str]) -> dict:
        try:
            import json
            body = json.dumps({"item_ids": item_ids})
            headers = self._sign(body)
            headers["Content-Type"] = "application/json"
            resp = await self._http.post("/api/shop/salvage", content=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            try:
                error_msg = exc.response.json().get("error", str(exc))
            except Exception:
                error_msg = str(exc)
            log.warning("Worker salvage_items failed: %s", error_msg)
            return {"ok": False, "error": error_msg}
        except httpx.RequestError as exc:
            log.warning("Worker salvage_items failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def restore_hp(self, post_token: str, provisions_spent: int) -> dict:
        try:
            import json
            body = json.dumps({"post_token": post_token, "provisions_spent": provisions_spent})
            headers = self._sign(body)
            headers["Content-Type"] = "application/json"
            resp = await self._http.post("/api/defend/restore", content=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            log.warning("Worker restore_hp failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def get_defense(self) -> dict:
        try:
            headers = self._sign("")
            resp = await self._http.get(
                f"/api/player/{self._player_id}/defense", headers=headers,
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            log.warning("Worker get_defense failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def get_my_raid(self) -> dict:
        """Fetch the attacker's most recent raid (in-flight status or landed result)."""
        try:
            headers = self._sign("")
            resp = await self._http.get("/api/raid/mine", headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            log.warning("Worker get_my_raid failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def get_status(self) -> dict:
        """Combined defense + own-raid poll in a single request. Returns
        ``{ok, defense: {ok, posts}, raid: {ok, active_raid_id, raid}}`` — the two
        halves keep the exact shapes of get_defense()/get_my_raid() so the same
        handlers consume them. Replaces two per-cycle requests with one."""
        try:
            headers = self._sign("")
            resp = await self._http.get("/api/status", headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            log.warning("Worker get_status failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def dispatch_raid(self, target_player_id: str, target_post_token: str,
                            item_ids: list[str]) -> dict:
        """Dispatch an atomic multi-item raid (travels, resolves at arrival)."""
        try:
            import json
            body = json.dumps({
                "target_player_id": target_player_id,
                "target_post_token": target_post_token,
                "item_ids": item_ids,
            })
            headers = self._sign(body)
            headers["Content-Type"] = "application/json"
            resp = await self._http.post("/api/raid/dispatch", content=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            try:
                error_msg = exc.response.json().get("error", str(exc))
            except Exception:
                error_msg = str(exc)
            log.warning("Worker dispatch_raid failed: %s", error_msg)
            return {"ok": False, "error": error_msg}
        except httpx.RequestError as exc:
            log.warning("Worker dispatch_raid failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def deploy_boost(self, post_token: str, item_ids: list[str]) -> dict:
        """Deploy defense items as temporary flat-HP boosts on a post."""
        try:
            import json
            body = json.dumps({"post_token": post_token, "item_ids": item_ids})
            headers = self._sign(body)
            headers["Content-Type"] = "application/json"
            resp = await self._http.post("/api/defend/boost", content=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            try:
                error_msg = exc.response.json().get("error", str(exc))
            except Exception:
                error_msg = str(exc)
            log.warning("Worker deploy_boost failed: %s", error_msg)
            return {"ok": False, "error": error_msg}
        except httpx.RequestError as exc:
            log.warning("Worker deploy_boost failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def get_raid_cooldowns(self) -> dict:
        """Fetch this player's live per-target raid cooldowns: {ok, expires_at}
        mapping target post_token -> epoch seconds the cooldown clears."""
        try:
            headers = self._sign("")
            resp = await self._http.get("/api/raid/cooldowns", headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            log.warning("Worker get_raid_cooldowns failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def get_leaderboard(self) -> dict:
        """Signed fetch — the leaderboard is registered-players-only now."""
        try:
            headers = self._sign("")
            resp = await self._http.get("/api/leaderboard", headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            log.warning("Worker leaderboard fetch failed: %s", exc)
            return {"ok": False, "error": str(exc)}
