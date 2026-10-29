# ADR 0007: Container Apps topology and streaming through Cloudflare

Status: Proposed.

## Context

The production topology is one Azure Container App with four containers (the backend, the FastMCP server, the Cerbos policy engine and the mock bank as sidecars), one replica, two user-assigned managed identities (one for image pull and Key Vault references that is never available to code, one for the code), app-scoped Key Vault references, per-container probes, a Container Apps job, and Cloudflare in front of the origin with an Origin CA certificate in Full (strict) mode, an IP allow-list of Cloudflare's ranges and a secret request header. The spike asks three questions: can that topology be provisioned as reviewed, idempotent code on this subscription; does a five-minute streamed response with a 15-second heartbeat survive Cloudflare's proxy and the Container Apps ingress end to end; and what are the cold-start time from zero replicas and the monthly cost of one warm replica.

The root `infra/app` provisions it: a workload-profiles environment with the Consumption profile only; the app in single revision mode with at most one replica and the minimum set by a variable; the platform identity on the app and the job with lifecycle `None` and the main identity with lifecycle `Main`, both set through azapi because azurerm 5.11.0 has no argument for them; a standard Key Vault under RBAC holding the origin header secret and the Origin CA private key, which is generated inside the vault and signed by Cloudflare from the exported request; the Azure Container Registry created by the bootstrap root and pulled by identity; TCP probes on the three application containers and Cerbos's own HTTP readiness path; a job with a manual trigger that runs the measurement client inside the environment; the Cloudflare zone's proxied A record, the domain-verification TXT record, Full (strict), the header rule, the rate rule and the allow-list read from Cloudflare's published IPv4 ranges. In this spike the backend container serves the streaming endpoint, the header check and nothing else; the FastMCP and mock-bank containers run the same image on their own ports, so that four containers with probes share one replica; Cerbos runs at 0.55.0 with strict evaluation and schema rejection on an empty policies directory.

## Pre-registration

Written before any run. A deviation during the run is recorded as such.

### Cold start

Cold start is the time from the first request sent to a scaled-to-zero revision until the response is usable. It is read three ways and all three are recorded: the measurement client's timed first request through the public hostname, with the time to the response headers and the time to the first stream chunk; the `Replicas` metric of the app over the window, read with the Azure CLI at one-minute granularity; and the rows of the environment's system log table in the window, recorded as they appear (the vendor documents revision provisioning messages for that table and no replica event, so these rows are context, not a timing source). Repetitions: 10. A repetition starts only after an idle window of at least 600 seconds with no request and only when a vendor read shows zero replicas: `az containerapp replica list` for the app, filtered to the count of replicas, reads 0 (the vendor's scale behaviour table gives a 300-second cool-down period and a 300-second scale-down stabilization window, does not say whether they add, and states that replica quantities are a target, not a guarantee). A repetition whose read is not 0 waits and reads again before it starts. The read is recorded as `replicas_before`; a repetition whose record shows a non-zero read, or whose `Replicas` metric for the minute before the request is not 0, is counted and excluded, and the figure is reported as k of 10 valid repetitions. The configuration under test is the committed root with `min_replicas = 0`. Figures land under `cold_start[]` with the keys `run`, `idle_window_s`, `replicas_before`, `t_headers_s`, `t_first_chunk_s`, `replicas_series`, `system_log_rows`, `valid`.

### The five-minute stream

One run is a request to `/stream` that asks for 300 seconds of output. Two arms share the route and differ in the body: the heartbeat arm emits a small payload chunk every 30 seconds with assistant-stream's 15-second heartbeat on, so bytes flow at least every 15 seconds; the silent arm emits one chunk at once after the response headers, then nothing for 300 seconds, then one final chunk, with the heartbeat off, so that any idle timer shorter than 300 seconds cuts it and the cut time names the timer. A run survives when all of the following hold: the HTTP status is 200; every gap between two received byte groups is at most 20 seconds (the 15-second heartbeat plus a 5-second tolerance); the total duration is at least 300 seconds; the body ends because the server closed it, not because the connection reset or a proxy answered an error page. Legs and counts, fixed here:

| Leg | Client | Path | Heartbeat arm | Silent arm |
|---|---|---|---|---|
| Cloudflare | developer machine | public hostname → Cloudflare → ingress → replica | 5 | 3 |
| Ingress | developer machine, its address allowed for the window | app FQDN → ingress → replica | 5 | 3 |
| Internal | the job, inside the environment | app name → replica, no ingress proxy | 3 | 2 |
| Ingress from inside | the job | app FQDN → ingress → replica | 2 | 0 |

The gate is a count against a known denominator per leg: with the heartbeat, k of n runs survive, and the gate holds when k equals n on the Cloudflare, Ingress and Internal legs (5/5, 5/5, 3/3). The silent-arm runs are observations, not gates; they decide claims 2 and 3 below. A leg that cannot be run is reported as "no target" with the reason, not as a failure; the Ingress-from-inside leg is expected to be refused by the allow-list and is recorded either way. Runs are paired by order within a leg, and a run on one leg is compared with the run of the same arm and order on another. Attribution window: a silent run that ends at time t after the request started is attributed to a timer of T seconds when t lies in [T − 5 s, T + 10 s]; the timers under test are the ingress's 240 s and Cloudflare's 125 s; a silent run that ends outside both windows and before 300 s is "unattributed" and reported as such; a silent run that reaches its final chunk has met no timer under 300 s. Figures land under `stream[]` with the keys `leg`, `arm`, `status`, `duration_s`, `bytes`, `chunks`, `max_gap_s`, `ended_by`, `attributed_to`.

### Attribution between the ingress and Cloudflare

A failure on the Cloudflare leg is attributed to Cloudflare only when the same arm survives on the Ingress leg; a failure on both legs is attributed to the ingress or the application, and the Internal leg decides between the two. The Ingress leg exists for this attribution and for nothing else; the developer address is added to the allow-list only for the measurement window and removed by the next apply.

### Claims, each with both outcomes

1. The retail meter "Environment Management Hour" does not bill a workload-profiles environment that has only the Consumption profile. Decided by the environment's first cost rows, readable up to 72 hours after the first apply. If it bills, the environment's fixed monthly charge is added to the floor and the environment type is reconsidered; the warm-replica figure stays "pending the cost read" until then. Key `claims.environment_meter`; the projection from the retail rate lands under `projections.environment_meter_usd_per_month`.
2. The default ingress mode's 240-second request timeout is an idle timeout. Decided by the Ingress leg: heartbeat runs that reach 300 seconds while silent runs end in the 240-second window mean idle; heartbeat runs that end in the 240-second window mean a total-duration timeout, and the five-minute stream fails at the origin regardless of Cloudflare; silent runs that reach 300 seconds mean no timer under 300 seconds acts on an open response, and the claim is answered "no timer observed". Key `claims.ingress_timeout`.
3. Cloudflare's 125-second response wait covers the wait for the first response only, and bytes inside the body keep the stream open. Decided by the silent arm on the Cloudflare leg against the same arm on the Ingress leg: a silent run cut in the 125-second window on Cloudflare while the Ingress run of the same order lasts longer means the wait also governs gaps inside the body; a silent run that lasts past the 125-second window on Cloudflare means the wait covers the first response only. The heartbeat arm cannot separate the two readings, since both predict its survival; it is the gate, not the decider. If the wait governs gaps, the 15-second heartbeat still stays inside it, and the record says so. Key `claims.cloudflare_timeout`.
4. The service accepts containers below 0.25 vCPU when the replica total is an allowed combination. Decided at the first apply of the app: accepted means the allocation stands; refused means each container takes 0.25 vCPU and 0.5 GiB and the idle floor rises accordingly. Key `claims.sub_quarter_vcpu`; the two projections land under `projections.idle_floor_usd_per_month` keyed by allocation.
5. The custom-domain binding validates through the `asuid` TXT record while the A record is proxied by Cloudflare. Decided at the binding apply: validated means the proxied record stands; refused means a DNS-only window during binding is recorded as the procedure. Key `claims.custom_domain_binding`.
6. A plan run without the certificate gates errors on `prevent_destroy` for a count reduction instead of planning a revoke. Decided by one plan run after the issuance, before the gate is committed. Key `claims.prevent_destroy_on_count`.
7. Cerbos 0.55.0 starts and answers its readiness path with an empty policies directory under strict evaluation and schema rejection. Decided at the first apply of the app: a ready sidecar means yes; a crash loop means a minimal policy file is added to the image, and the record says so. Key `claims.cerbos_empty_policies`.

### Cost

The spend floor from phase 0b is the projection; the measured figure is the Cost Management query at resource-group scope over the warm window, read no earlier than 72 hours after the window, and lands under `cost[]` with the keys `date`, `resource_type`, `usd` and under `warm_replica_usd_per_month`. Until that read, the ADR says "pending the cost read".

### Where the figures live

`docs/results/0007-topology.json`, generated by `dispute_casework.topology_results` from the committed raw readings; a test regenerates it and fails when the committed file differs. The ADR states the decision and points to the keys; no figure is typed into prose.

## Spend

Projected in phase 0b from the Retail Prices API: the warm replica's idle floor, the registry's daily unit, the Postgres server's storage already on the subscription. Measured: pending the cost read.

## Decision

Pending phase 1.

## Findings

Pending phase 1, except the following, recorded as they occurred.

- Claim 4, `claims.sub_quarter_vcpu`: not decided. The first apply of the app on 2026-10-29 was refused by the service with `ContainerAppInvalidCpuResource`: "container 'mcp' cpu '0.125' is invalid" and "must contain a decimal value to no more than two decimal places". That is a precision rule, not a rule on values below 0.25 vCPU, and no sub-0.25 value with two decimals was tried. Deviation from the pre-registration: the fallback allocation was applied without deciding the claim; every container runs at 0.25 vCPU and 0.5Gi, a replica total of 1.0 vCPU and 2.0Gi, with the idle-floor consequence under `projections.idle_floor_usd_per_month`.

## Consequences

Pending phase 1.

## Code written here

Pending phase 1.
