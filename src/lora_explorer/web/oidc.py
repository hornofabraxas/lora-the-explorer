import logging
from urllib.parse import urljoin

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from joserfc import jwt as jose_jwt
from joserfc.jwk import KeySet

log = logging.getLogger(__name__)

_discovery_cache: dict[str, dict] = {}


async def _fetch_discovery(issuer_url: str) -> dict | None:
    if issuer_url in _discovery_cache:
        return _discovery_cache[issuer_url]
    url = issuer_url.rstrip("/") + "/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            meta = resp.json()
            _discovery_cache[issuer_url] = meta
            return meta
    except Exception:
        log.exception("Failed to fetch OIDC discovery from %s", url)
        return None


async def validate_oidc_config(config: dict) -> dict | None:
    issuer_url = config.get("issuer_url", "").strip()
    if not issuer_url:
        return None
    return await _fetch_discovery(issuer_url)


async def create_oidc_client(oidc_config: dict) -> tuple[AsyncOAuth2Client, dict] | None:
    meta = await _fetch_discovery(oidc_config["issuer_url"])
    if not meta:
        return None
    client = AsyncOAuth2Client(
        client_id=oidc_config["client_id"],
        client_secret=oidc_config.get("client_secret", ""),
        code_challenge_method="S256",
    )
    return client, meta


def get_authorization_url(
    client: AsyncOAuth2Client, meta: dict, redirect_uri: str
) -> tuple[str, str, str]:
    import secrets
    from authlib.oauth2.rfc7636 import create_s256_code_challenge

    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(48)
    code_challenge = create_s256_code_challenge(code_verifier)

    authorization_endpoint = meta["authorization_endpoint"]
    url = client.create_authorization_url(
        authorization_endpoint,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=code_challenge,
        code_challenge_method="S256",
        scope="openid",
    )[0]
    return url, state, code_verifier


async def handle_callback(
    oidc_config: dict,
    meta: dict,
    redirect_uri: str,
    code: str,
    code_verifier: str,
) -> str | None:
    token_endpoint = meta["token_endpoint"]
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": oidc_config["client_id"],
                "client_secret": oidc_config.get("client_secret", ""),
                "code_verifier": code_verifier,
            },
        )
        if resp.status_code != 200:
            log.error("Token exchange failed: %s %s", resp.status_code, resp.text)
            return None
        token_data = resp.json()

    id_token = token_data.get("id_token")
    if not id_token:
        log.error("No id_token in token response")
        return None

    jwks_uri = meta.get("jwks_uri")
    if not jwks_uri:
        log.error("No jwks_uri in discovery metadata")
        return None

    async with httpx.AsyncClient(timeout=10) as http:
        jwks_resp = await http.get(jwks_uri)
        jwks_resp.raise_for_status()
        jwks_data = jwks_resp.json()

    key_set = KeySet.import_key_set(jwks_data)
    token = jose_jwt.decode(id_token, key_set)
    claims = token.claims

    # The signature check above only proves the IdP minted *some* token. Validate
    # the standard claims too, or an expired token — or one issued to a different
    # client at the same IdP — would still authenticate. `iss` is the canonical
    # issuer from discovery; `aud` must contain our client_id; `exp` is required.
    expected_iss = meta.get("issuer") or oidc_config.get("issuer_url", "")
    claims_registry = jose_jwt.JWTClaimsRegistry(
        leeway=60,
        iss={"essential": True, "value": expected_iss},
        aud={"essential": True, "value": oidc_config["client_id"]},
        exp={"essential": True},
    )
    try:
        claims_registry.validate(claims)
    except Exception:
        log.error("ID token claim validation failed", exc_info=True)
        return None

    sub = claims.get("sub")
    if not sub:
        log.error("No sub claim in ID token")
        return None
    return sub
