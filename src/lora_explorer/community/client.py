import logging

import httpx

log = logging.getLogger(__name__)

TIMEOUT = 5.0


class CommunityClient:
    def __init__(self, base_url: str):
        self._base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self._base_url, timeout=TIMEOUT)

    async def close(self) -> None:
        await self._http.aclose()

    async def link(self, code: str, api_key: str) -> dict:
        try:
            resp = await self._http.post(
                "/api/link", json={"code": code, "api_key": api_key},
            )
            resp.raise_for_status()
            return {"ok": True, **resp.json()}
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            log.warning("Community link failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def post_stats(self, api_key: str, stats: dict) -> bool:
        try:
            resp = await self._http.post(
                "/api/stats",
                json=stats,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
            return True
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            log.debug("Community stats post failed: %s", exc)
            return False

    async def post_achievement(self, api_key: str, achievement: dict) -> bool:
        try:
            resp = await self._http.post(
                "/api/achievement",
                json=achievement,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
            return True
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            log.debug("Community achievement post failed: %s", exc)
            return False
