"""Entra access tokens: verification and the principal they carry."""

from typing import Literal
from uuid import UUID

import jwt
from azure.identity.aio import OnBehalfOfCredential
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict

Role = Literal["preparer", "approver", "restricted_viewer"]

REQUIRED_CLAIMS = ["exp", "iat", "nbf", "iss", "aud"]
REQUIRED_SCOPE = "access_as_user"
# Microsoft.IdentityModel's TokenValidationParameters.DefaultClockSkew.
CLOCK_SKEW_SECONDS = 300


class IdentitySettings(BaseSettings):
    """IDENTITY_TENANT_ID, API_CLIENT_ID and MCP_CLIENT_ID, from the environment."""

    model_config = SettingsConfigDict(frozen=True)

    identity_tenant_id: str
    api_client_id: str
    mcp_client_id: str


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)

    oid: UUID
    roles: frozenset[Role]


def issuer(tenant_id: str) -> str:
    return f"https://login.microsoftonline.com/{tenant_id}/v2.0"


def jwks_uri(tenant_id: str) -> str:
    return f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"


def parse_token(
    token: str, jwks: jwt.PyJWKClient, *, issuer: str, audience: str
) -> Principal:
    """Raises jwt.PyJWTError for an invalid token, ValidationError for a bad claim."""
    claims = jwt.decode(
        token,
        jwks.get_signing_key_from_jwt(token).key,
        algorithms=["RS256"],
        audience=audience,
        issuer=issuer,
        options={"require": REQUIRED_CLAIMS},
        leeway=CLOCK_SKEW_SECONDS,
    )
    scopes = claims.get("scp", "")
    if not isinstance(scopes, str) or REQUIRED_SCOPE not in scopes.split():
        raise jwt.InvalidTokenError(f"scp does not grant {REQUIRED_SCOPE}")
    return Principal.model_validate(
        {"oid": claims.get("oid"), "roles": claims.get("roles", [])}
    )


def mcp_token_verifier(identity: IdentitySettings) -> JWTVerifier:
    """Explicit issuer, keys and audience; AzureJWTVerifier also accepts api:// ones."""
    return JWTVerifier(
        jwks_uri=jwks_uri(identity.identity_tenant_id),
        issuer=issuer(identity.identity_tenant_id),
        audience=identity.mcp_client_id,
        algorithm="RS256",
        required_scopes=[REQUIRED_SCOPE],
    )


async def mcp_token(
    user_assertion: str, identity: IdentitySettings, certificate: bytes
) -> str:
    """Exchanges the caller's API token for an MCP token, On-Behalf-Of the same user.

    One credential per incoming token: its cache holds one user's tokens only.
    """
    async with OnBehalfOfCredential(
        identity.identity_tenant_id,
        identity.api_client_id,
        client_certificate=certificate,
        user_assertion=user_assertion,
    ) as credential:
        token = await credential.get_token(
            f"api://{identity.mcp_client_id}/{REQUIRED_SCOPE}"
        )
    return token.token
