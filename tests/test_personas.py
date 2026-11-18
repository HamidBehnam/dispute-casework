import asyncio
import string
import uuid
from types import SimpleNamespace
from typing import Any

from azure.core.exceptions import ResourceNotFoundError
from msgraph.generated.models.o_data_errors.o_data_error import ODataError
from personas import (
    APPS,
    PASSWORD_LENGTH,
    PERSONAS,
    Change,
    PersonaSettings,
    Resource,
    State,
    changes,
    generate_password,
    run,
    secret_name,
)

RESOURCES = {
    app: Resource(uuid.uuid4(), {role: uuid.uuid4() for role in set(PERSONAS.values())})
    for app in APPS
}


def provisioned() -> State:
    users = {persona: uuid.uuid4() for persona in PERSONAS}
    return State(
        frozenset(PERSONAS),
        users,
        frozenset(
            (users[persona], resource.service_principal_id, resource.role_ids[role])
            for persona, role in PERSONAS.items()
            for resource in RESOURCES.values()
        ),
    )


def test_an_empty_tenant_gets_each_password_before_its_user_then_both_roles() -> None:
    planned = changes(State(frozenset(), {}, frozenset()), RESOURCES)
    assert planned == [
        change
        for persona in PERSONAS
        for change in (
            Change("password", persona),
            Change("user", persona),
            Change("assignment", persona, "api"),
            Change("assignment", persona, "mcp"),
        )
    ]


def test_a_provisioned_tenant_needs_no_change() -> None:
    assert changes(provisioned(), RESOURCES) == []


def test_a_stored_password_is_reused_for_a_user_not_yet_created() -> None:
    state = provisioned()
    users = dict(state.users)
    del users["approver-2"]
    resumed = State(state.stored_passwords, users, state.assignments)
    assert changes(resumed, RESOURCES) == [
        Change("user", "approver-2"),
        Change("assignment", "approver-2", "api"),
        Change("assignment", "approver-2", "mcp"),
    ]


def test_only_a_missing_assignment_is_added() -> None:
    state = provisioned()
    missing = (
        state.users["viewer-1"],
        RESOURCES["mcp"].service_principal_id,
        RESOURCES["mcp"].role_ids["restricted_viewer"],
    )
    partial = State(state.stored_passwords, state.users, state.assignments - {missing})
    assert changes(partial, RESOURCES) == [Change("assignment", "viewer-1", "mcp")]


def test_two_personas_per_role() -> None:
    roles = list(PERSONAS.values())
    assert sorted(set(roles)) == ["approver", "preparer", "restricted_viewer"]
    assert all(roles.count(role) == 2 for role in roles)


def test_a_generated_password_has_every_character_class() -> None:
    for _ in range(50):
        password = generate_password()
        assert len(password) == PASSWORD_LENGTH
        for group in (
            string.ascii_lowercase,
            string.ascii_uppercase,
            string.digits,
            string.punctuation,
        ):
            assert any(character in group for character in password)


class FakeTenant:
    """The Graph and Key Vault calls the script makes, over in-memory state."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []
        self.secrets: dict[str, str] = {}
        self.accounts: dict[str, str] = {}
        self.assignments: dict[str, list[Any]] = {}
        self.principals = {
            client_id: SimpleNamespace(
                id=str(uuid.uuid4()),
                app_roles=[
                    SimpleNamespace(value=role, id=uuid.uuid4())
                    for role in set(PERSONAS.values())
                ],
            )
            for client_id in ("api-client", "mcp-client")
        }
        for principal in self.principals.values():
            self.assignments[principal.id] = []
        self.users = SimpleNamespace(
            by_user_id=self.user_by_name, post=self.create_user
        )
        self.service_principals = SimpleNamespace(
            by_service_principal_id=self.assigned_to
        )

    def service_principals_with_app_id(self, app_id: str) -> SimpleNamespace:
        return SimpleNamespace(get=lambda: self.ready(self.principals[app_id]))

    @staticmethod
    async def ready(value: Any) -> Any:
        return value

    def user_by_name(self, upn: str) -> SimpleNamespace:
        async def get() -> SimpleNamespace:
            if upn not in self.accounts:
                error = ODataError()
                error.response_status_code = 404
                raise error
            return SimpleNamespace(id=self.accounts[upn])

        return SimpleNamespace(get=get)

    async def create_user(self, user: Any) -> SimpleNamespace:
        assert (
            user.password_profile.password
            == self.secrets[secret_name(user.display_name)]
        )
        assert user.password_profile.force_change_password_next_sign_in is False
        self.writes.append(("user", user.display_name))
        self.accounts[user.user_principal_name] = str(uuid.uuid4())
        return SimpleNamespace(id=self.accounts[user.user_principal_name])

    def assigned_to(self, principal_id: str) -> SimpleNamespace:
        async def get() -> SimpleNamespace:
            return SimpleNamespace(
                value=list(self.assignments[principal_id]), odata_next_link=None
            )

        async def post(assignment: Any) -> None:
            self.writes.append(("assignment", str(assignment.principal_id)))
            self.assignments[principal_id].append(assignment)

        return SimpleNamespace(app_role_assigned_to=SimpleNamespace(get=get, post=post))

    async def get_secret(self, name: str) -> SimpleNamespace:
        if name not in self.secrets:
            raise ResourceNotFoundError(name)
        return SimpleNamespace(value=self.secrets[name])

    async def set_secret(self, name: str, value: str) -> None:
        self.writes.append(("password", name.removeprefix("persona-")))
        self.secrets[name] = value


SETTINGS = PersonaSettings(
    arm_tenant_id="home",
    identity_tenant_id="identity",
    identity_domain="example.test",
    api_client_id="api-client",
    mcp_client_id="mcp-client",
)


def test_plan_writes_nothing() -> None:
    tenant = FakeTenant()
    asyncio.run(run("plan", tenant, tenant, SETTINGS))  # type: ignore[arg-type]
    assert tenant.writes == []


def test_apply_stores_each_password_before_its_user_and_repeats_as_a_no_op() -> None:
    tenant = FakeTenant()
    asyncio.run(run("apply", tenant, tenant, SETTINGS))  # type: ignore[arg-type]
    kinds = [kind for kind, _ in tenant.writes]
    assert kinds.count("password") == 6 and kinds.count("user") == 6
    assert kinds.count("assignment") == 12
    for persona in PERSONAS:
        assert tenant.writes.index(("password", persona)) < tenant.writes.index(
            ("user", persona)
        )
    tenant.writes.clear()
    asyncio.run(run("apply", tenant, tenant, SETTINGS))  # type: ignore[arg-type]
    assert tenant.writes == []
