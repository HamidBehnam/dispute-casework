"""Creates the demo personas and assigns each its role on the API and the MCP server.

`plan` lists what `apply` would change and changes nothing. `apply` creates a missing
persona with a generated password, stored in Key Vault before the user is created so
that no account exists without its password on record, and adds every missing role
assignment. An existing persona keeps its password. A run that stops part-way is
completed by running `apply` again.
"""

import argparse
import secrets
import string

import httpx
from azure.identity import AzureCliCredential
from pydantic_settings import BaseSettings

GRAPH = "https://graph.microsoft.com/v1.0"
VAULT = "https://kv-dcw-eus2.vault.azure.net"
VAULT_API_VERSION = "2025-07-01"

PERSONAS = {
    "preparer1": "preparer",
    "preparer2": "preparer",
    "approver1": "approver",
    "approver2": "approver",
    "viewer1": "restricted_viewer",
    "viewer2": "restricted_viewer",
}


class PersonaSettings(BaseSettings):
    identity_tenant_id: str
    identity_domain: str
    api_client_id: str
    mcp_client_id: str


def generate_password() -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(32))
        if (
            any(c.islower() for c in password)
            and any(c.isupper() for c in password)
            and any(c.isdigit() for c in password)
        ):
            return password


def resource(
    graph: httpx.Client, client_id: str
) -> tuple[str, dict[str, str], set[tuple[str, str]]]:
    """The service principal's id, its role ids by value, its (user, role) pairs."""
    principal = (
        graph.get(
            f"/servicePrincipals(appId='{client_id}')",
            params={"$select": "id,appRoles"},
        )
        .raise_for_status()
        .json()
    )
    roles = {role["value"]: role["id"] for role in principal["appRoles"]}
    assigned = (
        graph.get(
            f"/servicePrincipals/{principal['id']}/appRoleAssignedTo",
            params={"$select": "principalId,appRoleId", "$top": "999"},
        )
        .raise_for_status()
        .json()["value"]
    )
    return (
        principal["id"],
        roles,
        {(a["principalId"], a["appRoleId"]) for a in assigned},
    )


def run(
    graph: httpx.Client, vault: httpx.Client, settings: PersonaSettings, apply: bool
) -> list[str]:
    resources = {
        "api": resource(graph, settings.api_client_id),
        "mcp": resource(graph, settings.mcp_client_id),
    }
    changes = []
    for name, role in PERSONAS.items():
        response = graph.get(
            f"/users/{name}@{settings.identity_domain}", params={"$select": "id"}
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            changes.append(f"{name}: create")
            user_id = None
            if apply:
                password = generate_password()
                vault.put(
                    f"/secrets/persona-{name}-password",
                    params={"api-version": VAULT_API_VERSION},
                    json={"value": password, "contentType": "password"},
                ).raise_for_status()
                user_id = (
                    graph.post(
                        "/users",
                        json={
                            "accountEnabled": True,
                            "displayName": name,
                            "mailNickname": name,
                            "userPrincipalName": f"{name}@{settings.identity_domain}",
                            "passwordProfile": {
                                "forceChangePasswordNextSignIn": False,
                                "password": password,
                            },
                        },
                    )
                    .raise_for_status()
                    .json()["id"]
                )
        else:
            user_id = response.raise_for_status().json()["id"]

        for audience, (principal_id, roles, assigned) in resources.items():
            if (user_id, roles[role]) in assigned:
                continue
            changes.append(f"{name}: assign {role} on {audience}")
            if apply:
                graph.post(
                    f"/servicePrincipals/{principal_id}/appRoleAssignedTo",
                    json={
                        "principalId": user_id,
                        "resourceId": principal_id,
                        "appRoleId": roles[role],
                    },
                ).raise_for_status()
    return changes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["plan", "apply"])
    args = parser.parse_args()
    settings = PersonaSettings()
    graph_token = AzureCliCredential(tenant_id=settings.identity_tenant_id).get_token(
        "https://graph.microsoft.com/.default"
    )
    vault_token = AzureCliCredential().get_token("https://vault.azure.net/.default")
    with (
        httpx.Client(
            base_url=GRAPH,
            headers={"Authorization": f"Bearer {graph_token.token}"},
            timeout=30,
        ) as graph,
        httpx.Client(
            base_url=VAULT,
            headers={"Authorization": f"Bearer {vault_token.token}"},
            timeout=30,
        ) as vault,
    ):
        changes = run(graph, vault, settings, apply=args.mode == "apply")
    print("\n".join(changes) or "no changes")


if __name__ == "__main__":
    main()
