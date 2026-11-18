import string
import uuid

from personas import (
    APPS,
    PASSWORD_LENGTH,
    PERSONAS,
    Change,
    Resource,
    State,
    changes,
    generate_password,
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
