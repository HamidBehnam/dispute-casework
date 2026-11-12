import json

import httpx

from dispute_casework.personas import (
    PERSONAS,
    PersonaSettings,
    generate_password,
    run,
)

SETTINGS = PersonaSettings(
    identity_tenant_id="tenant",
    identity_domain="example.test",
    api_client_id="api-app",
    mcp_client_id="mcp-app",
)
ROLES = [
    {"value": role, "id": f"{role}-id"}
    for role in ("preparer", "approver", "restricted_viewer")
]


class Tenant:
    """Stands in for the Graph and Key Vault endpoints and records every write."""

    def __init__(self) -> None:
        self.users: dict[str, str] = {}
        self.assignments: dict[str, set[tuple[str, str]]] = {
            "api-sp": set(),
            "mcp-sp": set(),
        }
        self.writes: list[str] = []

    def graph(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1.0")
        if request.method == "GET" and path.startswith("/servicePrincipals(appId="):
            sp = "api-sp" if "api-app" in path else "mcp-sp"
            return httpx.Response(200, json={"id": sp, "appRoles": ROLES})
        if request.method == "GET" and path.endswith("/appRoleAssignedTo"):
            sp = path.split("/")[2]
            assigned = [
                {"principalId": p, "appRoleId": r} for p, r in self.assignments[sp]
            ]
            return httpx.Response(200, json={"value": assigned})
        if request.method == "GET" and path.startswith("/users/"):
            upn = path.removeprefix("/users/")
            if upn not in self.users:
                return httpx.Response(404)
            return httpx.Response(200, json={"id": self.users[upn]})
        if request.method == "POST" and path == "/users":
            body = json.loads(request.content)
            upn = body["userPrincipalName"]
            self.users[upn] = f"id-{upn}"
            self.writes.append(f"user {upn.split('@')[0]}")
            return httpx.Response(201, json={"id": self.users[upn]})
        if request.method == "POST" and path.endswith("/appRoleAssignedTo"):
            body = json.loads(request.content)
            self.assignments[body["resourceId"]].add(
                (body["principalId"], body["appRoleId"])
            )
            self.writes.append(f"assignment {body['resourceId']}")
            return httpx.Response(201, json={})
        raise AssertionError(f"unexpected Graph call {request.method} {path}")

    def vault(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        self.writes.append(f"secret {request.url.path.split('/')[2]}")
        return httpx.Response(200, json={})

    def run(self, apply: bool) -> list[str]:
        with (
            httpx.Client(
                base_url="https://graph.test/v1.0",
                transport=httpx.MockTransport(self.graph),
            ) as graph,
            httpx.Client(
                base_url="https://vault.test", transport=httpx.MockTransport(self.vault)
            ) as vault,
        ):
            return run(graph, vault, SETTINGS, apply)


def test_plan_changes_nothing() -> None:
    tenant = Tenant()

    changes = tenant.run(apply=False)

    assert len(changes) == 3 * len(PERSONAS)
    assert tenant.writes == []


def test_apply_stores_each_password_before_creating_the_user() -> None:
    tenant = Tenant()

    tenant.run(apply=True)

    for name in PERSONAS:
        assert tenant.writes.index(
            f"secret persona-{name}-password"
        ) < tenant.writes.index(f"user {name}")
    assert (
        len(tenant.assignments["api-sp"])
        == len(tenant.assignments["mcp-sp"])
        == len(PERSONAS)
    )


def test_apply_again_changes_nothing() -> None:
    tenant = Tenant()
    tenant.run(apply=True)
    tenant.writes.clear()

    assert tenant.run(apply=True) == []
    assert tenant.writes == []


def test_apply_adds_only_the_missing_assignment() -> None:
    tenant = Tenant()
    tenant.run(apply=True)
    tenant.assignments["mcp-sp"].discard(
        ("id-viewer2@example.test", "restricted_viewer-id")
    )
    tenant.writes.clear()

    assert tenant.run(apply=True) == ["viewer2: assign restricted_viewer on mcp"]
    assert tenant.writes == ["assignment mcp-sp"]


def test_generated_password_meets_the_tenant_complexity_rule() -> None:
    for _ in range(100):
        password = generate_password()
        assert len(password) == 32
        assert any(c.islower() for c in password)
        assert any(c.isupper() for c in password)
        assert any(c.isdigit() for c in password)
