---
name: architecture-docs
description: "Create or improve repository-grounded software architecture and business-logic documentation using progressive disclosure: L1 plain-language system context for managers, L2 process scenarios with simplified sequence diagrams, L3 technical reference with full diagrams and contracts, plus ADRs for rationale. Use whenever a user asks to document a codebase, explain system architecture or business flows, create architecture HTML/Markdown, onboard engineers, turn code/API docs into readable architecture docs, or reproduce the documentation style bundled with this skill. Analyze the implementation before writing, keep each fact in one canonical place, link to existing API docs instead of duplicating them, and mark unverified details rather than inventing them."
---

# Architecture Docs

> **Repository conventions win.** If the repository has its own documentation rules (for example `AGENTS.md` → «Стиль тексту» in chibot-docs), follow them for section templates, headings, ADR format and wording; this skill gives the general method.

Create architecture documentation that leads the reader from the simple and general to the complex
and specific. A manager reads L1–L2 and understands the system; an engineer skims the L1–L2
diagrams, then drills into L3 only where needed.

Use this workflow:

1. Discover the repository and existing documentation.
2. Build an evidence-backed architecture inventory.
3. Design L1, L2, L3 and ADR placement before writing prose (decide the canonical place of every fact).
4. **Pilot first:** write L1 plus one or two related L2 scenarios with their L3 sections. Show them for
   review before scaling the same pattern to the remaining scenarios.
5. Write the rest with diagrams and cross-links.
6. Verify every important statement against code.
7. Validate the generated HTML (links, Mermaid syntax, structure).

## Core rule: levels by audience and depth

| Level | Audience | Answers | Entry diagram | Must NOT contain |
|---|---|---|---|---|
| **L1 Context** | everyone, incl. managers | What is the system, what blocks it has, how they connect, what limits to keep in mind | Architecture (context) diagram | repo paths, stack, ports, chip/model names, constants, endpoints, field names |
| **L2 Processes** | managers + engineers | How blocks interact in one scenario, why it is built this way, what constraints and guarantees matter | Simplified sequence diagram per scenario | SQL, function/file names, env vars, constant names, pins, HTTP codes, DB tables, full endpoint lists |
| **L3 Reference** | engineers | Exact contracts, fields, validation, limits, errors, code locations | Full sequence diagram / ERD / state machine | Retelling the L2 scenario, rationale (→ ADR), operator runbooks |
| **ADR** | engineers, future maintainers | Why a consequential decision was made | none | Full description of the mechanism |

Each level adds detail without repeating the previous one. If a reader going down a level thinks
"I have already seen this", the content is duplicated — cut it.

Do not add an L4. If L3 becomes too large, split it by component.

## One canonical place per fact

Every fact lives in exactly one place; everywhere else links to it.

| Kind of fact | Canonical place |
|---|---|
| Number, constant, timeout, error code, field | L3 of the owning component |
| Order of steps in a scenario | L2, one diagram |
| "Why is it done this way" | ADR (L2 may keep one plain sentence + link) |
| Operator procedure | Runbook / ops document, not L3 |
| Exhaustive endpoint contracts | API reference (OpenAPI/Swagger), linked |
| Enumerable config / route tables | Generated block, not hand-typed |

L2 may repeat a few key numbers a manager will ask about (e.g. "the setup window is 10 minutes"),
in plain words, without constant names. Before finishing, grep the document for repeated numbers
and repeated flows; if one fact appears in 3+ places, keep one and link the others. Duplicates drift
and become contradictions.

## Bundled resource URIs

Use canonical skill URIs when loading bundled resources. Do not rely on the caller's current working
directory or clickable relative Markdown links.

- Research checklist: `skills://architecture-docs/references/research-checklist.md`
- PM requirements: `skills://architecture-docs/references/pm-documentation-requirements.md`
- ByChat HTML reference: `skills://architecture-docs/references/bychat-backend-architecture-example.html`
- HTML validator: `skills://architecture-docs/scripts/validate_architecture_html.py`

If the runtime materializes a bundled resource to the filesystem, use the materialized absolute
filesystem path for code execution. Keep `skills://...` URIs for loading/reading skill resources.

## Repository discovery

Before drafting, inspect enough of the repository to answer these questions with evidence:

- What are the actual runtime components and entry points?
- What calls what across process/service boundaries?
- Where are HTTP/WebSocket/RPC/event/queue boundaries?
- Where are persistence, cache, files, and external providers used?
- Which business flows matter to users or operators?
- Where do authentication, authorization, tenancy, billing, retries, timeouts, rate limits, caching,
  background work, logging, and error handling live?
- Which contracts are already documented in Swagger/OpenAPI/GraphQL schemas or other generated references?
- Which invariants are enforced by code rather than merely described in prompts/comments?

Trace representative flows through implementation, not just filenames or README claims. For
multi-repository systems (e.g. device firmware + mobile app + cloud), trace each flow across all
repositories.

For large repositories, explore major areas independently when the environment supports
parallel/sub-agent exploration. Synthesize only after the main paths are understood.

Read `skills://architecture-docs/references/research-checklist.md` when the repository is
non-trivial or unfamiliar.

## Evidence discipline

Treat runtime implementation as the strongest evidence, then generated API/schema artifacts, tests,
configuration, and maintained docs. Comments and old prose are weaker evidence.

Never invent behavior, numeric limits, timeouts, retries, payload fields, metrics, or rationale.
Typical invented claims to check explicitly:

- response shapes in diagrams — copy them from the handler/serializer, not from memory;
- observability — are metrics actually exported (a `/metrics` route, a CloudWatch/Prometheus
  client), or only counted in-process? Are logs centralized with retention, or only stdout?
- "the system guarantees X" — find the enforcing code, constraint, or test;
- security properties — e.g. is a device key registered at the factory, or trusted on first use?

When evidence is insufficient, write `Needs verification` near the statement. If code and prose
disagree, document the code and note the discrepancy. When a claim about database behavior is
disputed (indexes, constraints, locking), reproduce it on a real database with a throwaway table and
keep the script next to the docs.

## Current state, not history

Documentation describes how the system works **now**.

- No "already fixed", "was changed", PR numbers, or "I will rework this".
- Planned or proposed behavior goes to an ADR with status `Proposed`, or to a "Known risks /
  technical debt" section — never mixed into the description of current behavior.
- Honest gaps are part of the current state: "metrics are not exported yet", "the key is trusted on
  first claim". Put them in L1 "Key limitations" (one line + link) and detail them in L3.
- No marketing tone ("100% fail-safe ✓", "zero-trust guaranteed", "sub-2-second" unless measured).

## Plain language and terminology

L1 and L2 are read by non-engineers. Write them in plain words; keep the technical term in
parentheses on first use so engineers can map it.

- "one-time verification code (challenge, nonce)" instead of "nonce challenge";
- "robot's pass (Bearer token)" instead of "opaque Bearer token";
- "robot's temporary Wi-Fi network (SoftAP)" instead of "SoftAP on port 80".

Explain handshake-like mechanisms by analogy when it helps ("works like a password of the day: the
answer fits only this code and only for a short time").

Name components by **role**, not by repository or deployment name (e.g. "API", "Admin API",
"ML API", "User cabinet", "Admin console"). Add a glossary table mapping document names to code /
Kubernetes / repository names. If the team has not agreed on terminology, propose the role-based
names and flag the choice as an open question — do not silently rename things the team uses daily.

Write in the user's / team's language.

## Design L1 — Context

L1 must be understandable without reading source code. Use this order:

1. **Short overview** (3–6 sentences): what the system is, for whom, what it does. Then, if useful,
   the lifecycle in 3 plain steps, and how to read the document (L1 → L2 → L3 → ADR).
2. **Architecture diagram** — immediately after the overview; readers want to see it first.
3. **Main components** — directly under the diagram: component and what it does, in one or two
   plain sentences. No repository paths, tech stack, ports, versions, chip or model names.
4. **Communication channels** (optional): who talks to whom, how (in plain words: "local Wi-Fi of the
   device, no internet", "internet, user login"), and why. No headers, ports or protocols.
5. **Key principles / boundaries** — short cards, one idea each, with a link to where it is implemented.
6. **Key limitations** — only the highest-impact ones, each one line with a link to L2/L3/ADR.
7. **Scope / out of scope** — plain list.
8. **Glossary** — plain definitions + the document-name ↔ code-name table.

Move repository structure, stack, ports and file paths to an L3 section "Repositories and stack".
Never put local absolute paths (`/Users/...`, `file:///...`) anywhere; use repository-relative paths.

## Design L2 — Processes (scenarios)

L2 starts with an overview map, then one section per scenario.

### L2 overview map

- A phase flowchart (3–5 phases, plain labels, no endpoints).
- A **simplified** sequence diagram of the whole lifecycle: 4–5 participants (e.g. User, App,
  Device, Cloud as one block), numbered steps `①…⑭`, short plain labels.
- A step table: `Step | Who → whom | What happens | Scenario (L2)`. No constants, no tech-details column.
- A link to the full end-to-end diagram, which lives in L3.

### Each L2 scenario

Use this template:

1. **What and why** — 2–3 sentences: what the scenario achieves, who starts it.
2. **Participants** and **Result** — one line each.
3. **Simplified sequence diagram** — only the main interactions between blocks; the cloud may be one
   participant; no endpoints, DB writes, function names. Show failure branches as `alt` blocks
   inside the same diagram instead of separate "failure scenarios" sections.
4. **How it works** — a few bullets explaining the non-obvious points in plain words, especially
   "why is it done this way" (one sentence; full rationale → ADR), and known races or retries.
5. **Guarantees and constraints** — plain bullets, key numbers allowed, known risks linked.
6. **Details on L3** — a table `What | Where` linking to the L3 sections, ADRs and API reference.

Merge sub-steps (e.g. "post-claim initialization") and failure handling of a scenario into that
scenario rather than creating separate L2 sections. Create separate scenarios only for distinct
user/system situations that actually exist (including maintenance ones such as "change Wi-Fi",
"transfer to another owner", "delete account").

## Design L3 — Technical reference

L3 holds what engineers need to work with the code. Prefer organizing L3 by component
(device, API, database, crypto, OTA, …); a per-stage L3 section is fine when it is the technical
counterpart of one L2 scenario.

Start with one line linking back to the L2 scenario ("The process in plain words is in Scenario N"),
then the full sequence diagram (all services, endpoints, DB writes). Default structure, omitting
irrelevant parts:

- **Trigger**
- **Input** — only required, business-significant fields; link each endpoint to its API-reference operation.
- **Processing** — do not retell the diagram as a numbered list. Add only what the diagram cannot
  show (transactions, locks, ordering guarantees).
- **Output** — exact shape, copied from code.
- **Validation**
- **Constraints** — timeouts, retries, payload limits, rate limits, with their constant names.
- **Failure scenarios** — error codes and recovery.
- **Guarantees / invariants** — each with its enforcement mechanism.
- **Implementation references** — key files/symbols, repository-relative.

Also in L3: data model (ERD must match current schema, including new tables and indexes; explain
non-obvious constraints and why obvious alternatives are wrong), repositories and stack,
observability and logging (current state + gaps), security model and **known risks with options**.

## Architectural diagrams

- One concern per diagram; label nodes with a role; label arrows with an action.
- Architecture diagrams are **structured and layered** (e.g. people → clients/devices → cloud →
  external services, top to bottom), with edges that do not cross. Crossing edges blur the picture;
  re-order nodes or split the diagram until they are gone.
- Keep structural context (L1) separate from temporal sequence (L2/L3).
- Numbering must be identical in the diagram, the step table and the navigation. Do not use Mermaid
  `autonumber` on a diagram that sits next to a numbered step table; number the messages explicitly.
- Keep diagrams readable for the audience: an L2 diagram a manager cannot read in a minute is too detailed.
- Mermaid: `flowchart` for phases/context, `sequenceDiagram` for L2/L3 sequences, `erDiagram`,
  `stateDiagram-v2`. No `;` inside `sequenceDiagram` messages (it silently breaks rendering). Avoid
  `{}` and quotes in flowchart labels unless the label is quoted.

## Architectural invariants

Document important "always/never" properties discovered in the implementation, e.g. "browser never
calls the internal ML service directly", "the device never chooses its tenant". Verify each against
call paths, constraints, or tests. Do not invent invariants from desired architecture.

## ADRs

Create ADRs only for consequential decisions a future engineer could question. Use MADR:

- Status (`Proposed` / `Accepted` / `Superseded by …`) and date
- Context and problem
- Decision drivers
- Considered options (real ones — from history, issues, the user, or review discussions)
- Decision
- Consequences / trade-offs
- Evidence / implementation references

Link every ADR from the L2/L3 text where the decision matters. One decision = one ADR; merge ADRs
that describe the same decision. Never delete an ADR; supersede it. Rationale found in L3 moves here.

## Existing API documentation

Do not turn architecture docs into a second OpenAPI reference. Architecture docs answer why, who
owns it, when, what rule matters, what happens on failure. Link each endpoint mention to its
operation in the API reference; if the reference is only reachable inside the codebase, publish a
static copy (e.g. a filtered OpenAPI JSON rendered by Scalar/Redoc) next to the docs so reviewers
without code access can open it.

## HTML output

When the user wants the style of the bundled example, load:

- `skills://architecture-docs/references/pm-documentation-requirements.md`
- `skills://architecture-docs/references/bychat-backend-architecture-example.html` only as a structural/visual reference

Create one self-contained HTML document unless the user requests multiple files. Preserve: sidebar
navigation grouped by L1 / L2 / L3 / ADR (subsections readers must find — e.g. a known risk — get
their own menu entry); anchor links and backlinks; compact TL;DR callouts; readable tables; Mermaid;
responsive layout; light/dark; clear visual distinction between levels. Do not copy the example's content.

If the document carries reviewer comments anchored to blocks or quotes (e.g. a `comments.json`),
re-check after restructuring that every comment still finds its anchor, and re-anchor moved ones.

After creating HTML, run the bundled validator (resolve its local installed path first):

```bash
python <absolute-path-to-skill>/scripts/validate_architecture_html.py <output.html>
```

Fix errors before delivery. Also parse every Mermaid block (e.g. `mermaid.parse` in a headless
browser) — a syntax error renders as an empty box.

## Cross-linking

Navigable tree: `L1 component / limitation → L2 scenario → L3 details → ADR`. Every L2 scenario ends
with a "Details on L3" table; every L3 section links back to its L2 scenario; every limitation on L1
links to where it is explained.

## Style

- Start simple; short paragraphs; diagrams and tables over walls of text.
- Plain words on L1/L2, precise terms on L3.
- Show constraints next to the interaction where they matter.
- No promotional language, no history, no promises.

## Verification pass

Review the document as a reader who knows nothing about the project, then as a manager, then as an
engineer. Confirm:

1. A new reader understands the purpose within a few minutes from L1 alone.
2. L1 has overview → diagram → components → limitations, and no implementation noise.
3. The architecture diagram has no crossing edges.
4. Each L2 scenario has a simplified sequence diagram, plain explanation, constraints and a "Details on L3" table.
5. L2 and L3 diagrams are not the same diagram with a few extra arrows; L3 does not retell L2.
6. No fact is repeated in 3+ places; no contradictions between places that mention the same fact.
7. Numbering is consistent across diagrams, tables and navigation.
8. Response shapes, limits, metrics and security claims were checked against code.
9. Text describes current behavior; proposals and risks are labelled as such.
10. All file/symbol references exist and are repository-relative; no local absolute paths.
11. Internal links and Mermaid blocks pass validation.
12. Every ADR has status and date and is linked from the text.

Apply corrections before presenting the result. For a first version, prefer a polished pilot of one
or two scenarios over a complete but duplicated document.
