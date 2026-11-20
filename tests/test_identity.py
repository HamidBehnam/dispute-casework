import asyncio
import datetime
import uuid
from collections.abc import Callable
from types import TracebackType
from typing import Any

import jwt
import pytest
from azure.core.credentials import AccessToken
from azure.identity.aio import OnBehalfOfCredential
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import NoEncryption, pkcs12
from cryptography.x509.oid import NameOID
from pydantic import ValidationError

from dispute_casework import identity as identity_module
from dispute_casework.identity import (
    IdentitySettings,
    Principal,
    issuer,
    jwks_uri,
    mcp_token,
    mcp_token_verifier,
    parse_token,
)


def test_a_principal_carries_only_the_three_roles() -> None:
    oid = uuid.uuid4()
    principal = Principal.model_validate({"oid": str(oid), "roles": ["approver"]})
    assert principal == Principal(oid=oid, roles=frozenset({"approver"}))
    with pytest.raises(ValidationError):
        Principal.model_validate({"oid": str(oid), "roles": ["administrator"]})
    with pytest.raises(ValidationError):
        Principal.model_validate({"oid": "not-a-guid", "roles": []})


@pytest.mark.block_network(allowed_hosts=["127.0.0.1"])
def test_parse_token_returns_the_principal_of_a_valid_token(
    identity: IdentitySettings,
    jwks_url: str,
    claims: Callable[..., dict[str, Any]],
    sign: Callable[[dict[str, Any]], str],
) -> None:
    jwks = jwt.PyJWKClient(jwks_url)
    issued = claims(identity.api_client_id, roles=["approver"])
    principal = parse_token(
        sign(issued),
        jwks,
        issuer=issuer(identity.identity_tenant_id),
        audience=identity.api_client_id,
    )
    assert principal == Principal(
        oid=uuid.UUID(issued["oid"]), roles=frozenset({"approver"})
    )
    no_role = parse_token(
        sign(claims(identity.api_client_id, roles=None)),
        jwks,
        issuer=issuer(identity.identity_tenant_id),
        audience=identity.api_client_id,
    )
    assert no_role.roles == frozenset()


@pytest.mark.block_network(allowed_hosts=["127.0.0.1"])
def test_parse_token_refuses_an_unknown_role(
    identity: IdentitySettings,
    jwks_url: str,
    claims: Callable[..., dict[str, Any]],
    sign: Callable[[dict[str, Any]], str],
) -> None:
    with pytest.raises(ValidationError):
        parse_token(
            sign(claims(identity.api_client_id, roles=["administrator"])),
            jwt.PyJWKClient(jwks_url),
            issuer=issuer(identity.identity_tenant_id),
            audience=identity.api_client_id,
        )


@pytest.mark.block_network(allowed_hosts=["127.0.0.1"])
@pytest.mark.parametrize("scp", [None, "", "User.Read", "access_as_users"], ids=str)
def test_parse_token_refuses_a_token_without_the_delegated_scope(
    identity: IdentitySettings,
    jwks_url: str,
    claims: Callable[..., dict[str, Any]],
    sign: Callable[[dict[str, Any]], str],
    scp: str | None,
) -> None:
    with pytest.raises(jwt.InvalidTokenError, match="scp"):
        parse_token(
            sign(claims(identity.api_client_id, scp=scp)),
            jwt.PyJWKClient(jwks_url),
            issuer=issuer(identity.identity_tenant_id),
            audience=identity.api_client_id,
        )


@pytest.mark.block_network(allowed_hosts=["127.0.0.1"])
def test_parse_token_accepts_the_scope_among_others(
    identity: IdentitySettings,
    jwks_url: str,
    claims: Callable[..., dict[str, Any]],
    sign: Callable[[dict[str, Any]], str],
) -> None:
    parse_token(
        sign(claims(identity.api_client_id, scp="User.Read access_as_user")),
        jwt.PyJWKClient(jwks_url),
        issuer=issuer(identity.identity_tenant_id),
        audience=identity.api_client_id,
    )


def test_the_mcp_verifier_names_issuer_keys_audience_and_scope(
    identity: IdentitySettings,
) -> None:
    verifier = mcp_token_verifier(identity)
    assert verifier.issuer == issuer(identity.identity_tenant_id)
    assert verifier.jwks_uri == jwks_uri(identity.identity_tenant_id)
    assert verifier.audience == identity.mcp_client_id
    assert verifier.algorithm == "RS256"
    assert verifier.required_scopes == ["access_as_user"]


def test_mcp_token_exchanges_the_callers_token_with_one_credential_per_call(
    monkeypatch: pytest.MonkeyPatch, identity: IdentitySettings
) -> None:
    credentials: list[dict[str, Any]] = []

    class RecordingCredential:
        def __init__(self, tenant_id: str, client_id: str, **kwargs: Any) -> None:
            self.record: dict[str, Any] = {
                "tenant_id": tenant_id,
                "client_id": client_id,
                **kwargs,
                "scopes": [],
                "closed": False,
            }
            credentials.append(self.record)

        async def __aenter__(self) -> "RecordingCredential":
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            self.record["closed"] = True

        async def get_token(self, *scopes: str) -> AccessToken:
            self.record["scopes"].extend(scopes)
            return AccessToken(f"mcp-token-for-{self.record['user_assertion']}", 0)

    monkeypatch.setattr(identity_module, "OnBehalfOfCredential", RecordingCredential)

    async def exchange_two() -> list[str]:
        return [
            await mcp_token("first-user", identity, b"certificate"),
            await mcp_token("second-user", identity, b"certificate"),
        ]

    assert asyncio.run(exchange_two()) == [
        "mcp-token-for-first-user",
        "mcp-token-for-second-user",
    ]
    assert [c["user_assertion"] for c in credentials] == ["first-user", "second-user"]
    for record in credentials:
        assert record["tenant_id"] == identity.identity_tenant_id
        assert record["client_id"] == identity.api_client_id
        assert record["client_certificate"] == b"certificate"
        assert record["scopes"] == [f"api://{identity.mcp_client_id}/access_as_user"]
        assert record["closed"]


def test_the_credential_accepts_the_vaults_pkcs12_without_a_password(
    identity: IdentitySettings,
) -> None:
    """Key Vault stores the certificate as application/x-pkcs12 with no password."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "dcw-api")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pfx = pkcs12.serialize_key_and_certificates(
        None, key, certificate, None, NoEncryption()
    )

    async def construct() -> None:
        async with OnBehalfOfCredential(
            identity.identity_tenant_id,
            identity.api_client_id,
            client_certificate=pfx,
            user_assertion="token",
        ):
            pass

    asyncio.run(construct())
