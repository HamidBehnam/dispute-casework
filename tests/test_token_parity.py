"""The same token fixtures through parse_token (PyJWT) and FastMCP's JWTVerifier.

FastMCP 4.2.2's JWTVerifier never reads iat and checks exp and nbf only when they
are present (fastmcp/server/auth/providers/jwt.py, load_access_token), so it
accepts a token without one of them, or with iat in the future, which parse_token
refuses. Its tolerance is 0 s on exp and 30 s on nbf, against parse_token's 300 s,
so a token inside that band is accepted by parse_token only. The expected verdicts
below record that; a change in either verifier fails the test.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import ValidationError

from dispute_casework.identity import (
    REQUIRED_SCOPE,
    IdentitySettings,
    issuer,
    parse_token,
)

ACCEPT, REJECT = "accept", "reject"

# fixture: (parse_token, FastMCP)
EXPECTED = {
    "valid": (ACCEPT, ACCEPT),
    "no-roles": (ACCEPT, ACCEPT),
    "no-scp": (REJECT, REJECT),
    "wrong-scp": (REJECT, REJECT),
    "expired": (REJECT, REJECT),
    "exp-inside-the-leeway": (ACCEPT, REJECT),
    "nbf-in-the-future": (REJECT, REJECT),
    "nbf-inside-the-leeway": (ACCEPT, REJECT),
    "no-exp": (REJECT, ACCEPT),
    "no-nbf": (REJECT, ACCEPT),
    "no-iat": (REJECT, ACCEPT),
    "iat-in-the-future": (REJECT, ACCEPT),
    "wrong-aud": (REJECT, REJECT),
    "wrong-iss": (REJECT, REJECT),
    "v1-issuer": (REJECT, REJECT),
    "unknown-kid": (REJECT, REJECT),
    "alg-none": (REJECT, REJECT),
    "hs256-with-the-public-key": (REJECT, REJECT),
    "tampered-payload": (REJECT, REJECT),
}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unsigned(header: dict[str, Any], claims: dict[str, Any]) -> str:
    return f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims).encode())}"


def mint(
    fixture: str,
    audience: str,
    identity: IdentitySettings,
    claims: Callable[..., dict[str, Any]],
    sign: Callable[[dict[str, Any]], str],
    signing_key: rsa.RSAPrivateKey,
) -> str:
    now = int(time.time())
    overrides: dict[str, dict[str, Any]] = {
        "no-roles": {"roles": None},
        "no-scp": {"scp": None},
        "wrong-scp": {"scp": "access_as_users"},
        "expired": {"iat": now - 4200, "nbf": now - 4200, "exp": now - 600},
        "exp-inside-the-leeway": {
            "iat": now - 3660,
            "nbf": now - 3660,
            "exp": now - 60,
        },
        "nbf-in-the-future": {"nbf": now + 3600},
        "nbf-inside-the-leeway": {"nbf": now + 120},
        "no-exp": {"exp": None},
        "no-nbf": {"nbf": None},
        "no-iat": {"iat": None},
        "iat-in-the-future": {"iat": now + 3600},
        "wrong-aud": {"aud": str(uuid.uuid4())},
        "wrong-iss": {"iss": issuer(str(uuid.uuid4()))},
        "v1-issuer": {"iss": f"https://sts.windows.net/{identity.identity_tenant_id}/"},
    }
    payload = claims(audience, **overrides.get(fixture, {}))
    if fixture == "unknown-kid":
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        return jwt.encode(payload, other, algorithm="RS256", headers={"kid": "other"})
    if fixture == "alg-none":
        return unsigned({"alg": "none", "typ": "JWT", "kid": "test-key"}, payload) + "."
    if fixture == "hs256-with-the-public-key":
        pem = signing_key.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        )
        signing_input = unsigned(
            {"alg": "HS256", "typ": "JWT", "kid": "test-key"}, payload
        )
        mac = hmac.new(pem, signing_input.encode(), hashlib.sha256).digest()
        return f"{signing_input}.{b64(mac)}"
    token = sign(payload)
    if fixture == "tampered-payload":
        header, _, signature = token.split(".")
        forged = b64(json.dumps(payload | {"roles": ["approver"]}).encode())
        return f"{header}.{forged}.{signature}"
    return token


@pytest.mark.block_network(allowed_hosts=["127.0.0.1"])
@pytest.mark.parametrize("audience", ["api", "mcp"])
@pytest.mark.parametrize("fixture", list(EXPECTED))
def test_parse_token_and_fastmcp_verdicts_match_the_expected_table(
    fixture: str,
    audience: str,
    identity: IdentitySettings,
    jwks_url: str,
    claims: Callable[..., dict[str, Any]],
    sign: Callable[[dict[str, Any]], str],
    signing_key: rsa.RSAPrivateKey,
) -> None:
    client_id = {"api": identity.api_client_id, "mcp": identity.mcp_client_id}[audience]
    token = mint(fixture, client_id, identity, claims, sign, signing_key)

    try:
        parse_token(
            token,
            jwt.PyJWKClient(jwks_url),
            issuer=issuer(identity.identity_tenant_id),
            audience=client_id,
        )
        pyjwt_verdict = ACCEPT
    except (jwt.PyJWTError, ValidationError):
        pyjwt_verdict = REJECT

    verifier = JWTVerifier(
        jwks_uri=jwks_url,
        issuer=issuer(identity.identity_tenant_id),
        audience=client_id,
        algorithm="RS256",
        required_scopes=[REQUIRED_SCOPE],
    )
    accepted = asyncio.run(verifier.verify_token(token)) is not None
    fastmcp_verdict = ACCEPT if accepted else REJECT

    print(
        f"E4 {fixture} {audience} parse_token={pyjwt_verdict} fastmcp={fastmcp_verdict}"
    )
    assert (pyjwt_verdict, fastmcp_verdict) == EXPECTED[fixture]
