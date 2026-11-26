# ADR 0010: App tables under row security, and the bank calendar

Status: Accepted. Supersedes 0005 in part: in 0005 the "Role bound at connect, and its reach." bullet's "the login can set exactly those three roles and no other" and "a `SET ROLE` to another of the three", and the "What it does." bullet's "one of the three roles" and "another of the three" (the login now also sets a fourth role, `app_rw`).

## Context

A case, the action proposed for it, the approval of that action and the case's audit trail live in the database, and the database must hold the design's guarantees on them even when the application above it is wrong. Those guarantees are: each role sees and writes only its own rows, an approver never approves a case they prepared, an action is decided at most once, and the audit trail is never rewritten. The ledger the mock bank posts to needs the same one-key-per-write rule. Deadlines count business days, so the bank needs a calendar fixed in a reviewed file, so that CI and the deadline tool read the same dates whatever holidays release is installed, for the two years the test dates fall in.

## Decision

- **Revision 0004, forward-only on 0003, in one transaction** (`transaction_per_migration=True` in `migrations/env.py`). It creates `app.cases`, `app.actions`, `app.approvals`, `app.audit_events` and `bank.ledger_entries`.
  - `app_owner` owns the four app tables and `bank_owner` owns the ledger.
  - Row-level security is enabled and forced on the four app tables. It is forced because PostgreSQL 17 lets an owner past it otherwise: "Table owners normally bypass row security as well, though a table owner can choose to be subject to row security with ALTER TABLE".
  - The ledger has no policy and no grant to any role the application can set.
- **The role `app_rw`**, NOLOGIN, granted to `dcw_service` `WITH INHERIT FALSE`, as the tier roles are. A connection binds it with `-c role=app_rw`.
- **The principal comes from two transaction-scoped settings**, `app.oid` and `app.roles` (space-separated role names).
  - The policies read them as `NULLIF(current_setting(…, true), '')`.
  - A missing or empty setting therefore reads as NULL: no comparison with the principal holds and no role is held, so no row is visible and no insert passes.
- **One permissive policy per command, SELECT and INSERT, on each app table.** There is no `FOR ALL` policy and no UPDATE or DELETE policy. Visibility is the matrix the design decided:

  | Role | Sees |
  |---|---|
  | Preparer | the cases they prepared |
  | Approver | cases in `awaiting_approval`, `approved`, `returned`, `rejected`, `executed` or `closed` that they did not prepare |
  | Restricted viewer | every case, and writes nothing |

  - An action, an approval or an audit event is visible when its case is.
  - Whether "assigned" enters the matrix is decided with the Cerbos ownership rule, in step 11.
- **What each role may insert:**
  - A case: only a preparer, naming themself as its preparer.
  - An action: only a preparer, on a case they prepared.
  - An approval: only an approver, naming themself as its approver, of an action they can see.
  - An audit event: only a preparer or an approver, naming themself as its actor, on a case they can see.
- **Grants.**
  - `app_rw` holds USAGE on `app`, SELECT on the four tables, and INSERT on named columns only. `status`, `event_id` and `occurred_at` take their defaults.
  - No role the application can set holds UPDATE, DELETE or TRUNCATE on an app table.
  - Every foreign key is NO ACTION. A referential action runs as the referencing table's owner with forced row security off, so a cascade would change audit rows outside every policy.
- **One decision per action.** `app.approvals` carries `UNIQUE (action_id)` and `UNIQUE (idempotency_key)`, both NOT NULL. `bank.ledger_entries` is minimal at this step: `entry_id` and `idempotency_key`, NOT NULL and UNIQUE.
- **Approver ≠ preparer, declaratively.**
  - `app.actions` and `app.approvals` copy the case's `preparer_oid`, NOT NULL. Composite foreign keys tie the copy to `cases(case_id, preparer_oid)` and to `actions(action_id, preparer_oid)`.
  - The copy is NOT NULL because a NULL would pass both checks. PostgreSQL 17: "a check constraint is satisfied if the check expression evaluates to true or the null value", and "MATCH SIMPLE allows any of the foreign key columns to be null; if any of them are null, the row is not required to have a match in the referenced table."
  - `CHECK (approver_oid <> preparer_oid)` refuses an approval by the preparer.
- **One login.** The application keeps one login. Its reach is recorded with two limits:
  - Granting `app_rw` to `dcw_service` lets any connection of the login, a tier connection included, `SET ROLE app_rw`.
  - Any session can set `app.oid` and `app.roles`. The policies therefore catch application mistakes, not code that can run SQL on an app connection.
- **The bank calendar.**
  - `python -m dispute_casework.calendar_build` writes `src/dispute_casework/calendar/bank-2026-2027.json`, which ships in the package. It holds the first and last covered day, the weekdays the bank is closed with their names, and the rule "Monday to Friday, except the closed weekdays".
  - The closed weekdays are the weekday entries of holidays' `financial_holidays("XFED", years=(2026, 2027), expand=False)`, written with sorted keys.
  - The file records `holidays 0.109, market XFED`, and `tests/test_calendar.py` regenerates it and fails when the committed file differs.
  - XFED keeps a Saturday holiday on Saturday, so the Friday before it stays a business day, and moves a Sunday holiday to Monday (`holidays/financial/federal_reserve.py`: "Holidays falling on Saturday are not moved: the Federal Reserve Banks are open the preceding Friday"; `observed_rule` `SUN_TO_NEXT_MON`). The project overrides nothing.
- **holidays is pinned exactly, `holidays==0.109`.** This is a further exception to ADR 0001's floors for runtime dependencies. The library's data is the committed artifact, which records the version it came from, and a version change moves that data.
- **Every clause of the four INSERT policies has a test that fails when that clause alone is removed.** The unique keys, the CHECK, the approval's NOT NULL `preparer_oid` and the composite keys each have a test that fails without them.
- **On Azure,** revision 0004 was applied on psql-dcw-cus on Nov 26, 2026, as the administrators group.
  - A read-only catalog comparison with the local database matched in owners, row-security flags, policies, privileges, `app_rw` and its `dcw_service` membership, constraints and `alembic_version`.
  - The one difference is the row that grants `app_rw` to its creator: the migration runner locally, the administrators group on Azure, `WITH ADMIN TRUE, SET FALSE, INHERIT FALSE` on both. PostgreSQL 17: "When a non-superuser creates a role using the CREATEROLE privilege, the created role is automatically granted back to the creating user, just as if the bootstrap superuser had executed the command GRANT created_user TO creating_user WITH ADMIN TRUE, SET FALSE, INHERIT FALSE."

## What it does

A connection bound to `app_rw`, with the user's object ID and roles set for the transaction, sees only that role's rows, and Postgres refuses:
- a write the role may not make;
- a second decision on an action;
- an approval by the case's preparer;
- an approval naming a preparer other than the case's;
- an audit event in another person's name or on a case the writer cannot see;
- any update, delete or truncate of the audit trail by the application's roles.

The application's `app_rw` engine and the function that sets the two settings land with their first caller; at this step the tests set them with SQL. The deadline tool (step 10) will read its business days from the committed file, not from a library call at run time.

## What it changed and what it costs

**What it changed.** The login can now set a fourth role, and with it reach the app tables from any of its connections. The setting is a convention the policies trust, not an identity they verify.

**What it costs:**
- **Append-only holds against the application's roles only.** The administrator holds `app_owner`, which is the accepted bound of append-only by grants, with Confidential Ledger as the alternative.
- **Unique and foreign-key checks bypass row security**, so a refused second approval tells its sender that a decision exists. The sender already holds the approver role on a visible case.
- **An edited or returned action is a new row**, because an action takes one approval.
- **Each holidays release** that changes the data means a regenerated calendar and a new exact pin.

**Left to later steps:**
- **Step 15:** status changes and the checks of a case's status in the write policies; whether case, action and approval IDs stay client-chosen.
- **Step 11:** the wider write reach of a principal holding restricted viewer together with a write role.
- **The step that adds the `app_rw` engine and its setter:** a pooled connection returned with both settings cleared.
- **Step 8, with the loader's rebuild:** TRUNCATE or DELETE for the corpus loader.
- **Step 10:** dates outside 2026–2027, which the deadline tool refuses.

## Why it over the alternatives

- **The declarative approver ≠ preparer constraints** over the trigger the design first named. A `BEFORE INSERT` trigger reading the preparer through actions and cases is code of the project's own, while foreign keys, NOT NULL and a CHECK do the whole job.
- **Grants for append-only** over a trigger refusing UPDATE and DELETE. A trigger misses TRUNCATE, and an owner can drop it.
- **`UNIQUE (action_id)` with `UNIQUE (idempotency_key)`** over one unique pair. The pair `(action_id, idempotency_key)` admits a second approval of the same action under a new key.
- **Transaction-scoped settings** over a database role per persona. The design names the setting for the app tables, and the corpus's reasons against it, the vendor search on its own pooled connections, do not apply here.
- **One login** over one login per tier. Separate logins would stop a `SET ROLE` from one tier to another inside a session, but not a compromised process that holds every credential, since the replica's containers share their identities. They would also need one principal and one connection limit per tier against one shared budget.
- **The calendar now, from XFED,** over waiting for the deadline tool or writing the dates by hand. The Saturday rule is tested now against the Federal Reserve's own dates, and a hand-written list would not be vendor-based.
- **XFED** over `holidays.US` or the stock-exchange calendar with an override in the project. Those calendars close the Friday before a Saturday holiday, which the Federal Reserve Banks keep open, so they would need an override. XFED needs none.
