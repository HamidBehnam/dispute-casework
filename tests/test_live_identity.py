"""Sign-in, On-Behalf-Of and token shape against the identity tenant.

Local only and never in CI: selected with `-m live`. The backend serves the web
build behind the compose `edge` proxy on LIVE_ORIGIN (http://localhost:8000), which
adds the origin header as the production edge does; the Azure CLI is signed in to
both tenants. Each test appends what it observed to OBSERVATIONS_DIR as one JSON
line, in a `finally`, whatever the outcome: claim names, types and verdicts, header
names and values, request origins and counts, booleans and AADSTS codes, never an
identifier, a token or a password.
"""

import asyncio
import base64
import json
import os
import re
import time
from collections.abc import Coroutine, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
from playwright.sync_api import Frame, Page, Request, Route

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
ASSIGNED = [persona for persona, role in PERSONAS.items() if role]
UNASSIGNED = [persona for persona, role in PERSONAS.items() if role is None]
SIGN_IN_SECONDS = 90
POLL_MILLISECONDS = 500
PASSWORD_POST_SECONDS = 5
CLOSE_GRACE_SECONDS = 2
AADSTS = re.compile(r"AADSTS\d+")
GUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
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
STARTED: dict[str, Any] = {}


def run_coroutine[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Sync Playwright keeps an event loop running on the test thread."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


@pytest.fixture(autouse=True)
def timed() -> None:
    STARTED.update(utc=datetime.now(UTC).isoformat(timespec="milliseconds"))
    STARTED.update(monotonic=time.monotonic())


def observe(test: str, **observation: Any) -> None:
    """One line per test, with the test's start time and its duration so far."""
    path = Path(os.environ["OBSERVATIONS_DIR"]) / f"identity-{RUN}.jsonl"
    timing = {
        "start_utc": STARTED["utc"],
        "duration_ms": round((time.monotonic() - STARTED["monotonic"]) * 1000),
    }
    with path.open("a") as file:
        file.write(json.dumps({"test": test, **timing, **observation}) + "\n")


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


@dataclass
class SignIn:
    """How one sign-in ended, read from the application page and Microsoft's page.

    result: "me" (the page showed /me), "app-error" (the page showed MSAL's error),
    "microsoft-error" (Microsoft's page showed an AADSTS code), "api-token-sent"
    (a token reached /me and the caller stopped there), "prompt" (a Microsoft page
    other than the credentials and stay-signed-in pages was still showing at the
    deadline), "popup-closed" (the popup closed before it reached the bridge page
    and MSAL wrote no error within CLOSE_GRACE_SECONDS) or "timeout". pages is the
    login page's document sequence, host and path only; password_attempts counts
    password submits made, password_confirmed whether one sent its POST.
    error_type names an exception raised during the sign-in or after it.
    """

    result: str = "timeout"
    prompts: list[str] = field(default_factory=list)
    microsoft_codes: list[str] = field(default_factory=list)
    app_error: dict[str, str] | None = None
    me: dict[str, Any] | None = None
    pages: list[str] = field(default_factory=list)
    password_attempts: int = 0
    password_confirmed: bool = False
    error_type: str | None = None


def on_microsoft(page: Page) -> bool:
    return not page.is_closed() and not page.url.startswith(ORIGIN)


def where(url: str) -> str:
    parts = urlsplit(url)
    return GUID.sub("<guid>", f"{parts.netloc}{parts.path}")


def visible(page: Page, selector: str) -> bool:
    return page.locator(selector).first.is_visible()


def sign_in(
    app: Page,
    login: Page,
    username: str,
    secret: str,
    tokens: list[str],
    outcome: SignIn,
    *,
    stop_on_token: bool = False,
) -> None:
    """Drive Microsoft's pages on `login` until the application page settles.

    Every page is examined on every poll until the deadline, so a prompt on any
    host, before or after the stay-signed-in page, is recorded as itself."""
    typed_username = typed_password = False
    bridge = where(f"{ORIGIN}/redirect")
    closed_at: float | None = None

    def record_page(frame: Frame) -> None:
        if frame == login.main_frame:
            outcome.pages.append(where(frame.url))

    outcome.pages.append(where(login.url))
    login.on("framenavigated", record_page)
    deadline = time.monotonic() + SIGN_IN_SECONDS
    while time.monotonic() < deadline:
        try:
            if stop_on_token and tokens:
                outcome.result = "api-token-sent"
                return
            closed = login is not app and login.is_closed()
            me, error = app.evaluate(
                "() => [document.getElementById('me')?.textContent ?? '',"
                " document.getElementById('error')?.textContent ?? '']"
            )
            if me:
                outcome.me, outcome.result = json.loads(me), "me"
                return
            if error:
                outcome.app_error, outcome.result = json.loads(error), "app-error"
                return
            if closed and bridge not in outcome.pages:
                # MSAL closes the popup before it writes its own error, so a close is
                # read as the user's only if no error follows within the grace.
                closed_at = closed_at or time.monotonic()
                if time.monotonic() - closed_at >= CLOSE_GRACE_SECONDS:
                    outcome.result = "popup-closed"
                    return
            if on_microsoft(login):
                codes = AADSTS.findall(login.inner_text("body"))
                if codes:
                    outcome.microsoft_codes = sorted(set(codes))
                    outcome.result = "microsoft-error"
                    return
                if not typed_username and visible(login, "input[name=loginfmt]"):
                    login.fill("input[name=loginfmt]", username)
                    login.click("input[type=submit]")
                    typed_username = True
                elif not typed_password and visible(login, "input[name=passwd]"):
                    outcome.password_attempts += 1
                    login.fill("input[name=passwd]", secret)
                    # Entra shows the password field before its view accepts the
                    # submit; only the form's POST shows the password was sent, so a
                    # click without one times out here and the next poll submits again.
                    with login.expect_request(
                        lambda request: (
                            request.method == "POST" and request.is_navigation_request()
                        ),
                        timeout=PASSWORD_POST_SECONDS * 1000,
                    ):
                        login.click("input[type=submit]")
                    typed_password = outcome.password_confirmed = True
                elif login.locator("#KmsiCheckboxField").count():
                    login.click("#idBtn_Back")
                elif (
                    typed_password
                    and not visible(login, "input[name=loginfmt]")
                    and not visible(login, "input[name=passwd]")
                ):
                    shown = login.locator("[role=heading]:visible").first
                    text = shown.inner_text().strip() if shown.count() else ""
                    if "@" in text:
                        text = "<names the account>"
                    if text and text not in outcome.prompts:
                        outcome.prompts.append(text)
        except PlaywrightError:
            pass  # a page between two navigations; read it on the next poll
        app.wait_for_timeout(POLL_MILLISECONDS)
    if outcome.prompts and on_microsoft(login):
        outcome.result = "prompt"
    elif login is not app and login.is_closed():
        outcome.result = "popup-closed"


def script_counts(requests: list[Sent]) -> dict[str, int]:
    """E2 is gated on the application's own pages; Microsoft's are recorded."""
    scripts = [r for r in requests if r.resource_type == "script"]
    from_app_pages = [r for r in scripts if r.page_url.startswith(ORIGIN)]
    return {
        "script_requests": len(scripts),
        "off_origin_script_requests": sum(
            not r.url.startswith(ORIGIN) for r in scripts
        ),
        "script_requests_from_app_pages": len(from_app_pages),
        "off_origin_script_requests_from_app_pages": sum(
            not r.url.startswith(ORIGIN) for r in from_app_pages
        ),
    }


def sign_in_record(outcome: SignIn) -> dict[str, Any]:
    return {
        "result": outcome.result,
        "prompts": outcome.prompts,
        "microsoft_codes": outcome.microsoft_codes,
        "app_error": outcome.app_error,
        "pages": outcome.pages,
        "password_attempts": outcome.password_attempts,
        "password_confirmed": outcome.password_confirmed,
        "error_type": outcome.error_type,
    }


def popup_sign_in(
    page: Page,
    persona: str,
    vault: SecretClient,
    tokens: list[str],
    outcome: SignIn,
    *,
    stop_on_token: bool = False,
) -> None:
    page.goto(ORIGIN)
    with page.expect_popup() as popup:
        page.click("#sign-in-popup")
    sign_in(
        page,
        popup.value,
        f"{persona}@{os.environ['IDENTITY_DOMAIN']}",
        password(vault, persona),
        tokens,
        outcome,
        stop_on_token=stop_on_token,
    )


@pytest.mark.parametrize("attempt", [1, 2, 3])
@pytest.mark.parametrize("persona", ASSIGNED)
def test_a_persona_signs_in_with_a_popup_and_reaches_me(
    signed_in_page: tuple[Page, list[Sent], list[str]],
    identity: IdentitySettings,
    vault: SecretClient,
    persona: str,
    attempt: int,
) -> None:
    """E1, E2 (popup), E5 and E6 for the API audience."""
    page, requests, tokens = signed_in_page
    outcome = SignIn()
    record: dict[str, Any] = {"persona": persona, "attempt": attempt}
    try:
        popup_sign_in(page, persona, vault, tokens, outcome)
        record["api_token_sent_to_me"] = bool(tokens)
        if outcome.me is not None and tokens:
            principal = parse_token(
                tokens[-1],
                jwt.PyJWKClient(jwks_uri(identity.identity_tenant_id)),
                issuer=issuer(identity.identity_tenant_id),
                audience=identity.api_client_id,
            )
            claims = jwt.decode(tokens[-1], options={"verify_signature": False})
            record |= {
                "parse_token_accepts": True,
                "me_roles": outcome.me["roles"],
                "me_oid_matches": outcome.me["oid"]
                == claims["oid"]
                == str(principal.oid),
                "claim_shape": claim_shape(claims),
                "ver": claims.get("ver"),
                "aud_is_api": claims.get("aud") == identity.api_client_id,
                "iss_is_v2": claims.get("iss") == issuer(identity.identity_tenant_id),
                "tid_is_identity_tenant": (
                    claims.get("tid") == identity.identity_tenant_id
                ),
                "scp": claims.get("scp"),
                "roles": claims.get("roles"),
            }
    except Exception as error:
        outcome.error_type = type(error).__name__
        raise
    finally:
        observe("popup", **record, **sign_in_record(outcome), **script_counts(requests))
    assert outcome.result == "me" and not outcome.prompts
    assert record["me_roles"] == [PERSONAS[persona]]
    assert script_counts(requests)["off_origin_script_requests_from_app_pages"] == 0


@pytest.mark.parametrize("persona", ASSIGNED)
def test_a_persona_signs_in_with_a_redirect(
    signed_in_page: tuple[Page, list[Sent], list[str]],
    vault: SecretClient,
    persona: str,
) -> None:
    """E2, the redirect flow through the bridge."""
    page, requests, tokens = signed_in_page
    outcome = SignIn()
    try:
        page.goto(ORIGIN)
        page.click("#sign-in-redirect")
        sign_in(
            page,
            page,
            f"{persona}@{os.environ['IDENTITY_DOMAIN']}",
            password(vault, persona),
            tokens,
            outcome,
        )
    except Exception as error:
        outcome.error_type = type(error).__name__
        raise
    finally:
        observe(
            "redirect",
            persona=persona,
            me_roles=outcome.me["roles"] if outcome.me else None,
            **sign_in_record(outcome),
            **script_counts(requests),
        )
    assert outcome.result == "me" and not outcome.prompts
    assert outcome.me and outcome.me["roles"] == [PERSONAS[persona]]
    assert script_counts(requests)["off_origin_script_requests_from_app_pages"] == 0


@pytest.mark.parametrize("persona", UNASSIGNED)
def test_an_unassigned_member_is_refused(
    signed_in_page: tuple[Page, list[Sent], list[str]],
    vault: SecretClient,
    persona: str,
) -> None:
    """E1: a member with no app role is refused a token for the API.

    The AADSTS code is read wherever it appears: MSAL's errorNo in the page's
    #error, or the text of Microsoft's page. The test ends as soon as a token
    reaches /me."""
    page, _, tokens = signed_in_page
    outcome = SignIn()
    try:
        popup_sign_in(page, persona, vault, tokens, outcome, stop_on_token=True)
    except Exception as error:
        outcome.error_type = type(error).__name__
        raise
    finally:
        app_number = (outcome.app_error or {}).get("errorNo")
        codes = [f"AADSTS{app_number}"] if app_number else outcome.microsoft_codes
        observe(
            "unassigned",
            **sign_in_record(outcome),
            aadsts_codes=codes,
            aadsts_point="app" if app_number else "microsoft" if codes else None,
            api_tokens_sent_to_me=len(tokens),
        )
    assert "AADSTS50105" in codes and not tokens


def test_the_redirect_bridge_headers() -> None:
    """E2: /redirect is never cached and carries no opener policy."""
    record: dict[str, Any] = {}
    try:
        response = httpx.get(f"{ORIGIN}/redirect")
        record |= {
            "status": response.status_code,
            "cache_control": response.headers.get("cache-control"),
            "cross_origin_opener_policy": response.headers.get(
                "cross-origin-opener-policy"
            ),
        }
    except Exception as error:
        record["error_type"] = type(error).__name__
        raise
    finally:
        observe("redirect-headers", **record)
    assert record["cache_control"] == "no-store"
    assert record["cross_origin_opener_policy"] is None


@pytest.mark.parametrize("persona", ASSIGNED)
def test_the_backend_exchanges_a_persona_token_for_an_mcp_token(
    signed_in_page: tuple[Page, list[Sent], list[str]],
    identity: IdentitySettings,
    vault: SecretClient,
    persona: str,
) -> None:
    """E3, E4 (c) and E6 for the MCP audience."""
    page, _, tokens = signed_in_page
    outcome = SignIn()
    record: dict[str, Any] = {"persona": persona}
    try:
        popup_sign_in(page, persona, vault, tokens, outcome)
        record["me_roles"] = outcome.me["roles"] if outcome.me else None
        assert tokens, "no API token reached /me"
        certificate = base64.b64decode(vault.get_secret("obo-certificate").value or "")
        token = run_coroutine(mcp_token(tokens[-1], identity, certificate))
        principal = parse_token(
            token,
            jwt.PyJWKClient(jwks_uri(identity.identity_tenant_id)),
            issuer=issuer(identity.identity_tenant_id),
            audience=identity.mcp_client_id,
        )
        record["parse_token_accepts"] = True
        record["roles_match"] = principal.roles == {PERSONAS[persona]}
        record["fastmcp_accepts"] = (
            run_coroutine(mcp_token_verifier(identity).verify_token(token)) is not None
        )
        claims = jwt.decode(token, options={"verify_signature": False})
        record |= {
            "claim_shape": claim_shape(claims),
            "ver": claims.get("ver"),
            "aud_is_mcp": claims.get("aud") == identity.mcp_client_id,
            "azp_is_api": claims.get("azp") == identity.api_client_id,
            "tid_is_identity_tenant": claims.get("tid") == identity.identity_tenant_id,
            "scp": claims.get("scp"),
            "roles": claims.get("roles"),
        }
    except Exception as error:
        outcome.error_type = type(error).__name__
        raise
    finally:
        observe("obo", **record, **sign_in_record(outcome))
    assert record["roles_match"] and record["fastmcp_accepts"]


def test_an_assertion_for_another_audience_is_refused(
    identity: IdentitySettings, vault: SecretClient
) -> None:
    """E3: a token whose audience is not the API is not exchanged."""
    record: dict[str, Any] = {"refused": False, "error_codes": []}
    try:
        certificate = base64.b64decode(vault.get_secret("obo-certificate").value or "")
        graph_token = (
            AzureCliCredential(tenant_id=identity.identity_tenant_id)
            .get_token("https://graph.microsoft.com/.default")
            .token
        )
        try:
            run_coroutine(mcp_token(graph_token, identity, certificate))
        except ClientAuthenticationError as refusal:
            record["refused"] = True
            record["error_codes"] = sorted(set(AADSTS.findall(str(refusal))))
    except Exception as error:
        record["error_type"] = type(error).__name__
        raise
    finally:
        observe("obo-refused", **record)
    assert record["refused"]


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

    record: dict[str, Any] = {}
    try:
        record["certificates"], record["passwords"] = run_coroutine(read())
    except Exception as error:
        record["error_type"] = type(error).__name__
        raise
    finally:
        observe("credentials", **record)
    assert (record["certificates"], record["passwords"]) == (1, 0)
