"""The six personas of the identity tenant and their app-role assignments.

    uv run python infra/identity/personas.py plan
    uv run python infra/identity/personas.py apply

Run after this root's apply, with API_CLIENT_ID and MCP_CLIENT_ID from its outputs.
A persona's password is stored in Key Vault before its user is created, so a run
that stops between the two resumes with the stored password; a second apply
changes nothing.
"""

import argparse
import asyncio
import secrets
import string
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from azure.core.exceptions import ResourceNotFoundError
from azure.identity.aio import AzureCliCredential
from azure.keyvault.secrets.aio import SecretClient
from kiota_abstractions.api_error import APIError
from msgraph.generated.models.app_role_assignment import AppRoleAssignment
from msgraph.generated.models.password_profile import PasswordProfile
from msgraph.generated.models.user import User
from msgraph.graph_service_client import GraphServiceClient
from pydantic_settings import BaseSettings

VAULT_URL = "https://kv-dcw-eus2.vault.azure.net"
GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]
PASSWORD_LENGTH = 32

PERSONAS = {
    "preparer-1": "preparer",
    "preparer-2": "preparer",
    "approver-1": "approver",
    "approver-2": "approver",
    "viewer-1": "restricted_viewer",
    "viewer-2": "restricted_viewer",
}
APPS = ("api", "mcp")


class PersonaSettings(BaseSettings):
    """The subscription's tenant (ARM_TENANT_ID) holds the vault; the identity tenant
    holds the users and the service principals."""

    arm_tenant_id: str
    identity_tenant_id: str
    identity_domain: str
    api_client_id: str
    mcp_client_id: str


@dataclass(frozen=True)
class Resource:
    service_principal_id: UUID
    role_ids: dict[str, UUID]


@dataclass(frozen=True)
class State:
    stored_passwords: frozenset[str]
    users: dict[str, UUID]
    assignments: frozenset[tuple[UUID, UUID, UUID]]


@dataclass(frozen=True)
class Change:
    kind: Literal["password", "user", "assignment"]
    persona: str
    app: str = ""


def changes(state: State, resources: dict[str, Resource]) -> list[Change]:
    """What apply would do, in order: per persona its password, its user, its roles."""
    planned: list[Change] = []
    for persona, role in PERSONAS.items():
        user_id = state.users.get(persona)
        if user_id is None and persona not in state.stored_passwords:
            planned.append(Change("password", persona))
        if user_id is None:
            planned.append(Change("user", persona))
        for app in APPS:
            resource = resources[app]
            assigned = (
                user_id,
                resource.service_principal_id,
                resource.role_ids[role],
            ) in state.assignments
            if not assigned:
                planned.append(Change("assignment", persona, app))
    return planned


def generate_password() -> str:
    """All four character classes, where Entra asks for three of them."""
    alphabet = string.ascii_letters + string.digits + string.punctuation
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(PASSWORD_LENGTH))
        if all(
            any(character in group for character in password)
            for group in (
                string.ascii_lowercase,
                string.ascii_uppercase,
                string.digits,
                string.punctuation,
            )
        ):
            return password


def secret_name(persona: str) -> str:
    return f"persona-{persona}"


def user_principal_name(persona: str, settings: PersonaSettings) -> str:
    return f"{persona}@{settings.identity_domain}"


async def read_resources(
    graph: GraphServiceClient, settings: PersonaSettings
) -> dict[str, Resource]:
    resources = {}
    for app, client_id in (
        ("api", settings.api_client_id),
        ("mcp", settings.mcp_client_id),
    ):
        principal = await graph.service_principals_with_app_id(client_id).get()
        assert principal and principal.id and principal.app_roles is not None
        resources[app] = Resource(
            UUID(principal.id),
            {
                role.value: role.id
                for role in principal.app_roles
                if role.value and role.id
            },
        )
    return resources


async def read_state(
    graph: GraphServiceClient,
    vault: SecretClient,
    resources: dict[str, Resource],
    settings: PersonaSettings,
) -> State:
    stored, users = set(), {}
    for persona in PERSONAS:
        try:
            await vault.get_secret(secret_name(persona))
            stored.add(persona)
        except ResourceNotFoundError:
            pass
        try:
            user = await graph.users.by_user_id(
                user_principal_name(persona, settings)
            ).get()
        except APIError as error:
            if error.response_status_code != 404:
                raise
        else:
            assert user and user.id
            users[persona] = UUID(user.id)

    assignments = set()
    for resource in resources.values():
        page = await graph.service_principals.by_service_principal_id(
            str(resource.service_principal_id)
        ).app_role_assigned_to.get()
        assert page and page.value is not None
        if page.odata_next_link:
            raise RuntimeError("more app-role assignments than one page holds")
        assignments |= {
            (a.principal_id, a.resource_id, a.app_role_id)
            for a in page.value
            if a.principal_id and a.resource_id and a.app_role_id
        }
    return State(frozenset(stored), users, frozenset(assignments))


async def apply_change(
    change: Change,
    graph: GraphServiceClient,
    vault: SecretClient,
    resources: dict[str, Resource],
    users: dict[str, UUID],
    settings: PersonaSettings,
) -> None:
    if change.kind == "password":
        await vault.set_secret(secret_name(change.persona), generate_password())
    elif change.kind == "user":
        password = await vault.get_secret(secret_name(change.persona))
        created = await graph.users.post(
            User(
                account_enabled=True,
                display_name=change.persona,
                mail_nickname=change.persona,
                user_principal_name=user_principal_name(change.persona, settings),
                password_profile=PasswordProfile(
                    password=password.value,
                    force_change_password_next_sign_in=False,
                ),
            )
        )
        assert created and created.id
        users[change.persona] = UUID(created.id)
    else:
        resource = resources[change.app]
        await graph.service_principals.by_service_principal_id(
            str(resource.service_principal_id)
        ).app_role_assigned_to.post(
            AppRoleAssignment(
                principal_id=users[change.persona],
                resource_id=resource.service_principal_id,
                app_role_id=resource.role_ids[PERSONAS[change.persona]],
            )
        )


async def main(command: str) -> None:
    settings = PersonaSettings()
    async with (
        AzureCliCredential(tenant_id=settings.identity_tenant_id) as graph_credential,
        AzureCliCredential(tenant_id=settings.arm_tenant_id) as vault_credential,
        SecretClient(VAULT_URL, vault_credential) as vault,
    ):
        graph = GraphServiceClient(graph_credential, scopes=GRAPH_SCOPES)
        resources = await read_resources(graph, settings)
        state = await read_state(graph, vault, resources, settings)
        planned = changes(state, resources)
        for change in planned:
            print(change.kind, change.persona, change.app)
        print(f"{len(planned)} changes")
        if command == "apply":
            users = dict(state.users)
            for change in planned:
                await apply_change(change, graph, vault, resources, users, settings)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["plan", "apply"])
    asyncio.run(main(parser.parse_args().command))
