import time

import pytest

from lora_explorer.web import oidc as oidc_module


class _FakeResp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    """Stubs the two httpx calls handle_callback makes: POST token endpoint,
    GET jwks_uri. Routed by URL substring."""

    def __init__(self, token_json, jwks_json, *a, **k):
        self._token_json = token_json
        self._jwks_json = jwks_json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, data=None):
        return _FakeResp(200, self._token_json)

    async def get(self, url):
        return _FakeResp(200, self._jwks_json)


ISS = "https://id.example.com"
CLIENT_ID = "my-client"


def _signed_id_token(claims):
    from joserfc import jwt
    from joserfc.jwk import OctKey
    key = OctKey.generate_key(256)
    token = jwt.encode({"alg": "HS256", "kid": "k1"}, claims, key)
    jwks = key.as_dict(private=False)
    jwks["kid"] = "k1"
    return token, {"keys": [jwks]}


async def _run_callback(monkeypatch, claims):
    id_token, jwks = _signed_id_token(claims)

    def _client_factory(*a, **k):
        return _FakeAsyncClient({"id_token": id_token}, jwks)

    monkeypatch.setattr(oidc_module.httpx, "AsyncClient", _client_factory)
    meta = {"issuer": ISS, "token_endpoint": f"{ISS}/token", "jwks_uri": f"{ISS}/jwks"}
    return await oidc_module.handle_callback(
        {"client_id": CLIENT_ID, "client_secret": "s", "issuer_url": ISS},
        meta, redirect_uri="https://app/cb", code="c", code_verifier="v",
    )


@pytest.mark.asyncio
async def test_handle_callback_valid_token(monkeypatch):
    now = int(time.time())
    sub = await _run_callback(monkeypatch, {
        "iss": ISS, "aud": CLIENT_ID, "sub": "user-1", "exp": now + 300,
    })
    assert sub == "user-1"


@pytest.mark.asyncio
async def test_handle_callback_rejects_expired(monkeypatch):
    now = int(time.time())
    sub = await _run_callback(monkeypatch, {
        "iss": ISS, "aud": CLIENT_ID, "sub": "user-1", "exp": now - 300,
    })
    assert sub is None


@pytest.mark.asyncio
async def test_handle_callback_rejects_wrong_audience(monkeypatch):
    now = int(time.time())
    sub = await _run_callback(monkeypatch, {
        "iss": ISS, "aud": "someone-else", "sub": "user-1", "exp": now + 300,
    })
    assert sub is None


@pytest.mark.asyncio
async def test_handle_callback_rejects_wrong_issuer(monkeypatch):
    now = int(time.time())
    sub = await _run_callback(monkeypatch, {
        "iss": "https://evil.example.com", "aud": CLIENT_ID, "sub": "user-1", "exp": now + 300,
    })
    assert sub is None
