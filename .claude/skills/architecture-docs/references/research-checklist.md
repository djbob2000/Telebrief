# Repository Research Checklist

Use this checklist before writing non-trivial architecture documentation.

## 1. Project map

Identify:

- languages and frameworks;
- package/workspace boundaries;
- runtime/deployable units;
- entry points;
- configuration and environment files;
- existing README/docs/ADR/API specs.

## 2. Runtime components

For each deployable or logical component, capture:

- responsibility;
- entry point;
- inbound interfaces;
- outbound dependencies;
- persistence/state ownership;
- security/tenancy boundary.

Avoid turning every module/class into a component.

## 3. Main flows

Choose representative flows based on user-visible or operational behavior, not file layout. For each flow, trace:

1. trigger;
2. transport/boundary crossings;
3. validation/auth;
4. business logic;
5. data access;
6. external calls;
7. response/event/side effect;
8. failure path.

## 4. Cross-cutting concerns

Search implementation/config/tests for:

- authentication and authorization;
- tenant/account scoping;
- idempotency;
- retries/backoff;
- timeouts;
- rate/concurrency limits;
- caching and invalidation;
- queues/background jobs;
- transactions;
- logging/tracing/metrics;
- feature flags;
- billing/quotas;
- secrets handling;
- cancellation;
- data retention.

## 5. Evidence table

Maintain a compact working table while researching:

| Claim/flow | Evidence | Confidence | Docs location |
|---|---|---|---|
| API validates tenant before service call | `path/to/handler.ts`, test | high | L2 step ② / L3 |
| retry count is 3 | config only, no test | medium | L3 constraint |
| reason for queue choice | not found | unknown | ADR: Needs verification |

Use this to prevent unsupported claims.

## 6. Diagram review

For every arrow ask:

- Is the direction correct?
- Does this call happen directly, or through another component?
- Is the arrow request, response, event, stream, database access, or file transfer?
- Is it always used or only in one branch?
- Would a new reader understand the label without code knowledge?

## 7. Final verification

Re-open referenced routes/services/config/tests and spot-check the claims most likely to be challenged: numeric limits, timeouts, retry behavior, security boundaries, and failure semantics.
