"""Sign-in, On-Behalf-Of and token shape against the identity tenant.

Local only and never in CI: selected with `-m live`. The backend serves the web
build behind the compose `edge` proxy on LIVE_ORIGIN (http://localhost:8000), which
adds the origin header as the production edge does; the Azure CLI is signed in to
both tenants. Each test appends what it observed to OBSERVATIONS_DIR as JSON lines:
claim names, types and verdicts, header names and values, request origins and
counts, never a token or a password.
"""

import asyncio
import base64
import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
import pytest
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import AzureCliCredential
from azure.identity.aio import AzureCliCredential as AsyncAzureCliCredential
from azure.keyvault.secrets import SecretClient
from msgraph.graph_service_client import GraphServiceClient
from personas import PERSONAS, VAULT_URL, secret_name
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Request, Route

from dispute_casework.identity import (
    IdentitySettings,
    issuer,
    jwks_uri,
    mcp_token,
    mcp_token_verifier,
    parse_token,
)

pytestmark = [pytest.mark.live, pytest.mark.block_network(allowed_hosts=[".*"])]

ORIGIN = os.environ.get("LIVE_ORIGIN", "http://localhost:8000")
CLAIM_TYPES = {
    "iss": str,
    "aud": str,
    "exp": int,
    "nbf": int,
    "iat": int,
    "ver": str,
    "tid": str,
    "oid": str,
    "azp": str,
    "scp": str,
    "roles": list,
}
RUN = time.strftime("%Y%m%dT%H%M%S")


def observe(test: str, **observation: Any) -> None:
    path = Path(os.environ["OBSERVATIONS_DIR"]) / f"identity-{RUN}.jsonl"
    with path.open("a") as file:
        file.write(json.dumps({"test": test, **observation}) + "\n")


def claim_shape(claims: dict[str, Any]) -> dict[str, str]:
    """Each expected claim's verdict: present with the stated type, or what it was."""
    return {
        name: (
            "missing"
            if name not in claims
            else "ok"
            if isinstance(claims[name], expected)
            else type(claims[name]).__name__
        )
        for name, expected in CLAIM_TYPES.items()
    }


@pytest.fixture(scope="module")
def identity() -> IdentitySettings:
    return IdentitySettings()


@pytest.fixture(scope="module")
def vault() -> SecretClient:
    credential = AzureCliCredential(tenant_id=os.environ["ARM_TENANT_ID"])
    return SecretClient(VAULT_URL, credential)


def password(vault: SecretClient, persona: str) -> str:
    value = vault.get_secret(secret_name(persona)).value
    assert value
    return value


@dataclass(frozen=True)
class Sent:
    """A request the browser made, with the page that made it, read when it was sent."""

    url: str
    resource_type: str
    page_url: str


@pytest.fixture
def signed_in_page(page: Page) -> Iterator[tuple[Page, list[Sent], list[str]]]:
    """The application page, every request it made and the tokens it sent to /me."""
    tokens: list[str] = []

    def capture_me_token(route: Route, request: Request) -> None:
        authorization = request.headers.get("authorization", "")
        if urlsplit(request.url).path == "/me" and authorization:
            tokens.append(authorization.removeprefix("Bearer "))
        route.continue_()

    requests: list[Sent] = []
    page.context.on(
        "request",
        lambda request: requests.append(
            Sent(request.url, request.resource_type, request.frame.url)
        ),
    )
    page.context.route(f"{ORIGIN}/**", capture_me_token)
    yield page, requests, tokens


def enter_credentials(page: Page, username: str, secret: str) -> list[str]:
    """Microsoft's sign-in page: user name, password, and the stay-signed-in prompt.
    Returns the URLs of any other prompt met (MFA, consent, password change)."""
    page.fill("input[name=loginfmt]", username)
    page.click("input[type=submit]")
    page.fill("input[name=passwd]", secret)
    page.click("input[type=submit]")
    prompts = []
    page.wait_for_load_state()
    if "login.microsoftonline.com" in page.url and not page.is_closed():
        if page.locator("#KmsiCheckboxField, #idBtn_Back").count():
            page.click("#idBtn_Back")
        elif not page.is_closed():
            prompts.append(urlsplit(page.url).path)
    return prompts


def api_claims(token: str, identity: IdentitySettings) -> dict[str, Any]:
    principal = parse_token(
        token,
        jwt.PyJWKClient(jwks_uri(identity.identity_tenant_id)),
        issuer=issuer(identity.identity_tenant_id),
        audience=identity.api_client_id,
    )
    claims: dict[str, Any] = jwt.decode(token, options={"verify_signature": False})
    assert str(principal.oid) == claims["oid"]
    return claims


@pytest.mark.parametrize("attempt", [1, 2, 3])
@pytest.mark.parametrize("persona", list(PERSONAS))
def test_a_persona_signs_in_with_a_popup_and_reaches_me(
    signed_in_page: tuple[Page, list[Sent], list[str]],
    identity: IdentitySettings,
    vault: SecretClient,
    persona: str,
    attempt: int,
) -> None:
    """E1, E2 (popup), E5 and E6 for the API audience."""
    page, requests, tokens = signed_in_page
    page.goto(ORIGIN)
    with page.expect_popup() as popup:
        page.click("#sign-in-popup")
    prompts = enter_credentials(
        popup.value,
        f"{persona}@{os.environ['IDENTITY_DOMAIN']}",
        password(vault, persona),
    )
    page.wait_for_function("document.getElementById('me').textContent.length > 0")
    me = json.loads(page.inner_text("#me"))
    claims = api_claims(tokens[-1], identity)
    scripts = [r for r in requests if r.resource_type == "script"]
    off_origin = [r for r in scripts if not r.url.startswith(ORIGIN)]
    from_app_pages = [r for r in scripts if r.page_url.startswith(ORIGIN)]
    observe(
        "popup",
        persona=persona,
        attempt=attempt,
        me_roles=me["roles"],
        me_oid_matches=me["oid"] == claims["oid"],
        prompts=prompts,
        silent_token=bool(tokens),
        claim_shape=claim_shape(claims),
        ver=claims.get("ver"),
        aud_is_api=claims.get("aud") == identity.api_client_id,
        iss_is_v2=claims.get("iss") == issuer(identity.identity_tenant_id),
        scp=claims.get("scp"),
        roles=claims.get("roles"),
        script_requests=len(scripts),
        off_origin_script_requests=len(off_origin),
        script_requests_from_app_pages=len(from_app_pages),
        off_origin_script_requests_from_app_pages=sum(
            not r.url.startswith(ORIGIN) for r in from_app_pages
        ),
    )
    assert me["roles"] == [PERSONAS[persona]] and not prompts and not off_origin


@pytest.mark.parametrize("persona", list(PERSONAS))
def test_a_persona_signs_in_with_a_redirect(
    signed_in_page: tuple[Page, list[Sent], list[str]],
    vault: SecretClient,
    persona: str,
) -> None:
    """E2, the redirect flow through the bridge."""
    page, requests, _ = signed_in_page
    page.goto(ORIGIN)
    page.click("#sign-in-redirect")
    prompts = enter_credentials(
        page, f"{persona}@{os.environ['IDENTITY_DOMAIN']}", password(vault, persona)
    )
    page.wait_for_function("document.getElementById('me').textContent.length > 0")
    me = json.loads(page.inner_text("#me"))
    scripts = [r for r in requests if r.resource_type == "script"]
    off_origin = [r for r in scripts if not r.url.startswith(ORIGIN)]
    from_app_pages = [r for r in scripts if r.page_url.startswith(ORIGIN)]
    observe(
        "redirect",
        persona=persona,
        me_roles=me["roles"],
        prompts=prompts,
        script_requests=len(scripts),
        off_origin_script_requests=len(off_origin),
        script_requests_from_app_pages=len(from_app_pages),
        off_origin_script_requests_from_app_pages=sum(
            not r.url.startswith(ORIGIN) for r in from_app_pages
        ),
    )
    assert me["roles"] == [PERSONAS[persona]] and not prompts and not off_origin


ADMINISTRATOR_SIGN_IN_SECONDS = 600


def test_the_unassigned_administrator_is_refused(
    signed_in_page: tuple[Page, list[Sent], list[str]],
) -> None:
    """E1: the administrator holds no app role, so Entra refuses the API token.

    Run headed (`--headed`); the administrator signs in by hand in the popup, with
    MFA. Only the error code is recorded."""
    page, _, tokens = signed_in_page
    console: list[str] = []
    page.on("console", lambda message: console.append(message.text))
    page.goto(ORIGIN)
    with page.expect_popup() as popup:
        page.click("#sign-in-popup")
    deadline = time.monotonic() + ADMINISTRATOR_SIGN_IN_SECONDS
    refused = False
    while not refused and time.monotonic() < deadline:
        try:
            shown = "" if popup.value.is_closed() else popup.value.content()
        except PlaywrightError:  # the popup is between two pages; read it next second
            shown = ""
        refused = any("AADSTS50105" in text for text in [shown, *console])
        page.wait_for_timeout(1000)
    observe("administrator", refused_with_aadsts50105=refused, api_tokens=len(tokens))
    assert refused and not tokens


def test_the_redirect_bridge_headers() -> None:
    """E2: /redirect is never cached and carries no opener policy."""
    response = httpx.get(f"{ORIGIN}/redirect")
    observe(
        "redirect-headers",
        status=response.status_code,
        cache_control=response.headers.get("cache-control"),
        cross_origin_opener_policy=response.headers.get("cross-origin-opener-policy"),
    )
    assert response.headers["cache-control"] == "no-store"
    assert "cross-origin-opener-policy" not in response.headers


@pytest.mark.parametrize("persona", list(PERSONAS))
def test_the_backend_exchanges_a_persona_token_for_an_mcp_token(
    signed_in_page: tuple[Page, list[Sent], list[str]],
    identity: IdentitySettings,
    vault: SecretClient,
    persona: str,
) -> None:
    """E3, E4 (c) and E6 for the MCP audience."""
    page, _, tokens = signed_in_page
    page.goto(ORIGIN)
    with page.expect_popup() as popup:
        page.click("#sign-in-popup")
    enter_credentials(
        popup.value,
        f"{persona}@{os.environ['IDENTITY_DOMAIN']}",
        password(vault, persona),
    )
    page.wait_for_function("document.getElementById('me').textContent.length > 0")
    certificate = base64.b64decode(vault.get_secret("obo-certificate").value or "")

    token = asyncio.run(mcp_token(tokens[-1], identity, certificate))
    principal = parse_token(
        token,
        jwt.PyJWKClient(jwks_uri(identity.identity_tenant_id)),
        issuer=issuer(identity.identity_tenant_id),
        audience=identity.mcp_client_id,
    )
    fastmcp_accepts = (
        asyncio.run(mcp_token_verifier(identity).verify_token(token)) is not None
    )
    claims = jwt.decode(token, options={"verify_signature": False})
    observe(
        "obo",
        persona=persona,
        parse_token_accepts=True,
        fastmcp_accepts=fastmcp_accepts,
        claim_shape=claim_shape(claims),
        ver=claims.get("ver"),
        aud_is_mcp=claims.get("aud") == identity.mcp_client_id,
        azp_is_api=claims.get("azp") == identity.api_client_id,
        scp=claims.get("scp"),
        roles=claims.get("roles"),
    )
    assert principal.roles == {PERSONAS[persona]} and fastmcp_accepts


def test_an_assertion_for_another_audience_is_refused(
    identity: IdentitySettings, vault: SecretClient
) -> None:
    """E3: a token whose audience is not the API is not exchanged."""
    certificate = base64.b64decode(vault.get_secret("obo-certificate").value or "")
    graph_token = (
        AzureCliCredential(tenant_id=identity.identity_tenant_id)
        .get_token("https://graph.microsoft.com/.default")
        .token
    )
    with pytest.raises(ClientAuthenticationError) as refusal:
        asyncio.run(mcp_token(graph_token, identity, certificate))
    codes = [word for word in str(refusal.value).split() if word.startswith("AADSTS")]
    observe("obo-refused", refused=True, error_codes=codes)


def test_the_api_app_holds_one_certificate_and_no_password(
    identity: IdentitySettings,
) -> None:
    """E3: the API registration's credentials, read from Graph."""

    async def read() -> tuple[int, int]:
        async with AsyncAzureCliCredential(
            tenant_id=identity.identity_tenant_id
        ) as credential:
            graph = GraphServiceClient(credential)
            application = await graph.applications_with_app_id(
                identity.api_client_id
            ).get()
            assert application
            return (
                len(application.key_credentials or []),
                len(application.password_credentials or []),
            )

    certificates, passwords = asyncio.run(read())
    observe("credentials", certificates=certificates, passwords=passwords)
    assert (certificates, passwords) == (1, 0)
