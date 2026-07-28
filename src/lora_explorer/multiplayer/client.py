import hashlib
import hmac
import json
import logging
import time

import httpx

from .. import __version__

log = logging.getLogger(__name__)

TIMEOUT = 10.0


class WorkerClient:
    def __init__(self, base_url: str, player_id: str | None = None, secret: str | None = None,
                 invite_code: str | None = None):
        self._base_url = base_url.strip().rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self._base_url, timeout=TIMEOUT,
            # Version gate (Worker middleware/version.ts) — advisory unless the
            # operator raises MIN_CLIENT_VERSION. Not part of the HMAC signature:
            # it's a compatibility signal, not an anti-cheat one, and a modified
            # client can already lie about anything, so signing it buys nothing.
            headers={"X-Client-Version": __version__},
        )
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

    def _error_result(self, exc: httpx.HTTPStatusError | httpx.RequestError) -> dict:
        """Uniform failure shape for every method below. For an HTTP error,
        surfaces the Worker's own `error` message (falling back to the bare
        exception text if the body isn't JSON) and flags update_required/
        min_version on a 426, so the manager can distinguish 'you're locked out
        of multiplayer until you update' from any other failure."""
        result: dict = {"ok": False}
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            result["status_code"] = status
            try:
                detail = exc.response.json()
            except Exception:
                detail = {}
            result["error"] = detail.get("error", str(exc))
            if status == 426:
                result["update_required"] = True
                if detail.get("min_version"):
                    result["min_version"] = detail["min_version"]
        else:
            result["error"] = str(exc)
        return result

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
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            result = self._error_result(exc)
            log.warning("Worker registration failed: %s", result["error"])
            return result

    async def push_bundle(self, bundle: dict) -> dict:
        try:
            body = json.dumps(bundle)
            headers = self._sign(body)
            headers["Content-Type"] = "application/json"
            resp = await self._http.post("/api/bundle", content=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            result = self._error_result(exc)
            log.warning("Worker bundle push failed: %s", result["error"])
            return result

    async def scout(self, target_player_id: str, probe_item_id: str) -> dict:
        try:
            body = json.dumps({"target_player_id": target_player_id, "probe_item_id": probe_item_id})
            headers = self._sign(body)
            headers["Content-Type"] = "application/json"
            resp = await self._http.post("/api/scout", content=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            result = self._error_result(exc)
            log.warning("Worker scout failed: %s", result["error"])
            return result

    async def install_item(self, post_token: str, item_id: str) -> dict:
        try:
            body = json.dumps({"post_token": post_token, "item_id": item_id})
            headers = self._sign(body)
            headers["Content-Type"] = "application/json"
            resp = await self._http.post("/api/defend/install", content=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            result = self._error_result(exc)
            log.warning("Worker install_item failed: %s", result["error"])
            return result

    async def buy_item(self, item_type: str, purchase_id: str) -> dict:
        try:
            body = json.dumps({"item_type": item_type, "purchase_id": purchase_id})
            headers = self._sign(body)
            headers["Content-Type"] = "application/json"
            resp = await self._http.post("/api/shop/buy", content=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            result = self._error_result(exc)
            log.warning("Worker buy_item failed: %s", result["error"])
            return result

    async def salvage_items(self, item_ids: list[str]) -> dict:
        try:
            body = json.dumps({"item_ids": item_ids})
            headers = self._sign(body)
            headers["Content-Type"] = "application/json"
            resp = await self._http.post("/api/shop/salvage", content=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            result = self._error_result(exc)
            log.warning("Worker salvage_items failed: %s", result["error"])
            return result

    async def restore_hp(self, post_token: str, provisions_spent: int) -> dict:
        try:
            body = json.dumps({"post_token": post_token, "provisions_spent": provisions_spent})
            headers = self._sign(body)
            headers["Content-Type"] = "application/json"
            resp = await self._http.post("/api/defend/restore", content=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            result = self._error_result(exc)
            log.warning("Worker restore_hp failed: %s", result["error"])
            return result

    async def get_defense(self) -> dict:
        try:
            headers = self._sign("")
            resp = await self._http.get(
                f"/api/player/{self._player_id}/defense", headers=headers,
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            result = self._error_result(exc)
            log.warning("Worker get_defense failed: %s", result["error"])
            return result

    async def get_my_raid(self) -> dict:
        """Fetch the attacker's most recent raid (in-flight status or landed result)."""
        try:
            headers = self._sign("")
            resp = await self._http.get("/api/raid/mine", headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            result = self._error_result(exc)
            log.warning("Worker get_my_raid failed: %s", result["error"])
            return result

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
            result = self._error_result(exc)
            log.warning("Worker get_status failed: %s", result["error"])
            return result

    async def dispatch_raid(self, target_player_id: str, target_post_token: str,
                            item_ids: list[str]) -> dict:
        """Dispatch an atomic multi-item raid (travels, resolves at arrival)."""
        try:
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
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            result = self._error_result(exc)
            log.warning("Worker dispatch_raid failed: %s", result["error"])
            return result

    async def deploy_boost(self, post_token: str, item_ids: list[str]) -> dict:
        """Deploy defense items as temporary flat-HP boosts on a post."""
        try:
            body = json.dumps({"post_token": post_token, "item_ids": item_ids})
            headers = self._sign(body)
            headers["Content-Type"] = "application/json"
            resp = await self._http.post("/api/defend/boost", content=body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            result = self._error_result(exc)
            log.warning("Worker deploy_boost failed: %s", result["error"])
            return result

    async def get_raid_cooldowns(self) -> dict:
        """Fetch this player's live per-target raid cooldowns: {ok, expires_at}
        mapping target post_token -> epoch seconds the cooldown clears."""
        try:
            headers = self._sign("")
            resp = await self._http.get("/api/raid/cooldowns", headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            result = self._error_result(exc)
            log.warning("Worker get_raid_cooldowns failed: %s", result["error"])
            return result

    async def get_leaderboard(self) -> dict:
        """Signed fetch — the leaderboard is registered-players-only now."""
        try:
            headers = self._sign("")
            resp = await self._http.get("/api/leaderboard", headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            result = self._error_result(exc)
            log.warning("Worker leaderboard fetch failed: %s", result["error"])
            return result
