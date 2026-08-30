# AGENTS.md

> Canonical product, architecture, and engineering instructions for AI coding agents working on **Telebrief**.
>
> **Read this file before changing ingestion, Event-First processing, publication selection, digest generation, article generation, validation, prompts, or editorial tests.**

---

# 0. Product Editorial Contract — READ THIS FIRST

This section is the product north star for Telebrief.

**If older plans, comments, prompts, legacy code, historical architecture notes, or tests conflict with this section, this section wins unless the user explicitly says otherwise.**

Telebrief is not a generic summarizer and not a system whose goal is to reduce a city to a few headlines. It is a reader product that turns a large stream of local source material into two different publication experiences:

- a **scan-first digest** for quickly understanding the current city situation and the breadth of meaningful updates;
- a **city-life long read** that gives a rich, concrete, readable picture of how the city lived during the reporting window.

The internal Event-First knowledge model exists to support those reader experiences safely, efficiently, and with traceable evidence.

## 0.1 Core editorial philosophy

Telebrief verifies **faithful representation**, not external truth in the abstract.

A legitimate local report does **not** need two independent sources or official confirmation to be publishable. A single community message may be useful news when it is represented honestly.

The system must distinguish:

```text
unconfirmed source
!=
unsupported statement invented by the writer
```

Examples:

```text
Source: "На Горе света нет"
Allowed: "По сообщениям жителей, на Горе нет света."

Source: "На Горе света нет"
Forbidden: "Авария на подстанции оставила Гору без света."
```

Corroboration may strengthen confidence, wording, or prominence, but **lack of corroboration alone must never suppress a legitimate local report**.

## 0.2 Digest product contract

The digest is a **scan-first coverage product**.

Its job is to let a resident understand, in seconds:

1. what the city situation is now;
2. what meaningful things happened during the reporting window;
3. what practical local information is useful;
4. what is known, reported, uncertain, scheduled, or unresolved.

Target structure:

```text
Digest: <edition> · <date>

City Situation
<operational point-in-time statuses>

<Rubric>
• **Short scan headline**: compact explanatory narrative.
• **Short scan headline**: compact explanatory narrative.

<Rubric>
...

Statistics
```

Rules:

- City Situation comes first when operational observations exist.
- City Situation contains actual operational states, not merely topics people ask about.
- `resident_question` is context, not a fact and not an operational status.
- A question such as "Работает ли пенсионный фонд?" must not become "пенсионный фонд не работает" or create a dashboard status by itself.
- A useful short community report must not be discarded merely because it is conversational, single-source, or unofficial.
- Related stories may be grouped for presentation inside their deterministic rubric/block, but legitimate coverage must not silently disappear.
- Commercial classifieds, private disputes, personal accusations, phone-number spam, repetitive ad copy, and directory-style payload must not dominate the digest.
- Community reports must preserve their epistemic status through natural attribution.
- The digest should be compact and easy to scan, but not so aggressively compressed that meaningful local facts disappear.

**Digest optimization target:** broad coverage + fast scanning + operational usefulness.

## 0.3 Article product contract: city-life long read

The Event-First article is a **city-life long read**.

It is not limited to 3–4 major stories. It may cover many meaningful parts of city life when the reporting window is rich enough.

The desired transformation is:

```text
many publishable local events
        ↓
remove real noise / unsafe material / directory payload
        ↓
group into coherent themes
        ↓
give topics different editorial depth
        ↓
preserve vivid supported microdetails
        ↓
write one cohesive city-life long read
```

Do **not** introduce an `ARTICLE_WORTHY yes/no` gate whose purpose is to throw away smaller legitimate stories.

Instead, use editorial depth / weighting such as:

- `DEVELOP` — major storyline; several paragraphs or a full section;
- `WEAVE` — meaningful supporting city-life material woven into a broader section;
- `BRIEF` — useful smaller item retained compactly.

These levels control **space and prominence**, not whether the underlying legitimate material is allowed to exist.

Major developments should dominate the article, but smaller stories should enrich it rather than vanish.

## 0.4 Microdetails are product value

Concrete local detail is a core feature of Telebrief.

A city-life article becomes weak if compression turns specific lived experience into generic editorial language.

Bad compression:

```text
"Жильцы скинулись по 300 рублей на домовой генератор, чтобы подавать воду"
→ "Жители адаптируются к сложной ситуации."

"Автобус №4 ходит примерно раз в час"
→ "Наблюдаются изменения в работе транспорта."

"Житель запитал оборудование провайдера от своего генератора, и Wi-Fi появился в доме"
→ "Горожане используют альтернативные источники энергии."
```

The generic sentences may be technically true, but they destroy the reader value.

When supported and editorially relevant, preserve concrete details such as:

- neighborhood, street, or micro-location;
- exact interval or time;
- a small supported amount or practical cost;
- a concrete resident action;
- how a workaround actually works;
- a specific service state;
- a short exact quote or vivid indirect paraphrase;
- a contrast between districts, buildings, services, or time periods;
- a practical consequence visible in everyday life.

**Compression should remove repetition, not reality.**

The article should not become a flat inventory of messages, but it also must not become a collection of vague abstractions such as:

- "горожане адаптируются";
- "ситуация остаётся сложной";
- "наблюдаются трудности";
- "жители ищут альтернативные решения".

If a concrete supported detail explains what those phrases mean, prefer the detail.

## 0.5 Breadth without a bulletin-board effect

Broad coverage does not mean reproducing every advertisement verbatim.

Telebrief should distinguish **city-life information** from **directory payload**.

Potentially useful city-life fact:

```text
A sports school opened free enrollment for children before the school year.
```

Usually unnecessary directory payload:

```text
contact person + full phone number + floor + office + booking URL + full ad copy
```

Potentially useful service fact:

```text
Intercity buses continue to operate toward several destinations.
```

Usually unnecessary directory payload:

```text
booking phone + messenger list + website + sales copy
```

Do not globally strip all names, prices, addresses, phone numbers, or times. Some of them are genuine evidence and important microdetails. Instead, distinguish editorial detail from ad payload.

Raw source text and provenance must remain available internally even when writer-facing context is sanitized.

## 0.6 Evidence Boundary

Publication freedom does not permit fabrication.

The writer must never invent or silently upgrade:

- locations;
- names or organizations;
- numbers, percentages, prices, dates, or durations;
- causes or mechanisms;
- completion states;
- future deadlines;
- official confirmation;
- city-wide scope from a single local observation;
- trends from a single question;
- answers to resident questions.

A community report may be published as a community report. It must not be rewritten as an official or established fact unless the evidence supports that upgrade.

### Direct quotes

Quotation marks mean exact primary-source wording.

- Never grammar-correct a direct quote.
- Never translate text inside quotation marks and still present it as the original quote.
- Never merge or shorten a direct quote while pretending it is exact.
- If translation, correction, or compression is needed, use indirect speech.

### Claim Atoms

Claim Atoms are validation metadata, not sentence templates.

- Keep them short, atomic, and close to evidence.
- Reader-facing prose may be smoother and more journalistic.
- Do not force the article prose to mimic raw source syntax.
- Do not hide unsupported reader-facing facts by omitting them from Claim Atoms; high-risk novelty in the published text must still be validated.

## 0.7 Article failure semantics

Event-First article generation is **single-call and fail-closed**.

```text
one writer attempt
    ↓
PASS → create publication
FAIL → reject publication
```

Hard factual/evidence violations remain blockers.

Do not reintroduce:

- second LLM fact-check calls;
- LLM repair loops;
- regeneration with another model;
- "reviewer" LLM stages;
- deterministic article prose fallback after a failed Event-First writer;
- automatic retry merely because the writer draft failed evidence validation.

Infrastructure/orchestration failures are a different category and may retain ordinary retry semantics where appropriate.

The digest may still have a deterministic fallback because a digest is a coverage product and its fallback semantics are intentionally different from article publication.

## 0.8 Reader hierarchy, not destructive selection

The article should feel hierarchical:

```text
major development          → DEVELOP → several paragraphs
important supporting line  → WEAVE   → one or more compact paragraphs
small useful city-life item→ BRIEF   → one compact mention
```

It should **not** feel like every source item has equal importance.

It should also **not** discard smaller legitimate material merely because it is not dramatic enough for a newspaper front page.

The correct goal is:

> **Not less information — better organized information.**

## 0.9 Forbidden editorial regressions

Agents must not make changes whose effect is to:

- reduce the city-life long read to only 3–4 selected headlines;
- require official confirmation or 2+ sources for legitimate local reports;
- treat resident questions as established facts or City Situation statuses;
- flatten supported microdetails into generic summaries;
- force every publishable item to receive equal article space;
- let classified ads, price lists, phone numbers, booking links, or promotional copy dominate prose;
- convert a broad local article into a directory of services;
- convert a broad local article into vague high-level commentary with little concrete city life;
- weaken hard Evidence Boundary checks merely to make an article pass;
- strengthen verification so aggressively that legitimate community news disappears;
- reintroduce claim-first per-message LLM explosion as the default processing architecture;
- hardcode one city's geography or examples into generic production prompt logic.

## 0.10 Target reader experience

**Digest:** fast to scan, broad enough to be useful, operationally clear, epistemically honest.

**Article:** rich, readable, concrete city-life long read. Major developments dominate; secondary stories enrich; microdetails make the city feel real; advertisements and directory payload do not take over the prose.

---

# 1. Canonical System Architecture

Telebrief is a multi-source local-news ingestion, Event-First knowledge, editorial synthesis, and publication system.

Canonical Event-First flow:

```text
Telegram / Facebook / RSS / Web
        ↓
Source Items + Revisions
        ↓
Deterministic Fragments
        ↓
Embeddings
        ↓
Vector / temporal Story clustering
        ↓
Gate V2 / semantic scope + retention + enrichment
        ↓
BRIEF or Rich Event Analysis
        ↓
EventPayload + exact fragment provenance
        ↓
Publication snapshot + selection
        ↓
        ├── Digest: City Situation + scan-first rubric coverage
        └── Article: city-life coverage plan + single-call writer + Evidence Boundary
        ↓
Delivery
```

The canonical design is **Event-First**, not claim-first.

Legacy/custom/message-based paths may remain for compatibility, comparison, migration, or benchmarking. Do not treat them as the target architecture unless the user explicitly asks to modify a legacy path.

## 1.1 Cost and scale principle

The pipeline must remain practical for large source sets, including potentially hundreds of Telegram channels and Facebook groups.

Prefer:

- deterministic preprocessing;
- batched embeddings;
- vector clustering;
- event-level LLM work after coalescing;
- one-call publication synthesis where designed;
- cached semantic decisions with explicit versioning.

Avoid reintroducing thousands of per-message generative calls.

---

# 2. Event-First Processing Semantics

## 2.1 Deterministic fragmentation

`src/processing/fragments.py`

Fragments are the smallest processing units used for embeddings and Story formation.

Important invariant:

- trivial acknowledgements/noise may be dropped;
- short but useful civic reports must survive;
- length alone must not erase useful community information.

Do not regress to a threshold that drops messages such as a short outage or service-status report before Gate sees it.

## 2.2 Story clustering

`src/processing/event_clustering.py`

Stories are clusters of related evidence, not publication paragraphs.

Do not modify `join_similarity` casually to solve publication presentation problems. Atomic Events may correctly stay separate even when the final digest/article should synthesize them together.

Publication composition and knowledge clustering are different layers.

## 2.3 Gate V2

`src/processing/event_triage.py`

Gate V2 handles three different decisions together:

- geographic scope;
- retention;
- enrichment depth.

Scope values:

- `LOCAL`
- `DIRECT_IMPACT`
- `OUT_OF_SCOPE`
- `UNCERTAIN`

Retention/enrichment must preserve useful local reports and only hard-drop high-confidence noise/commercial-only material according to the current contract.

### Resident questions

Evidence kind `resident_question` means:

- preserve as context;
- default to `publication_use=CONTEXT`;
- do not create an operational observation by itself;
- do not treat the question as an answer;
- do not infer a trend from one question.

A real answer to a resident question may become separate publishable evidence such as `service_access`, `community_report`, `official_statement`, or `established_fact` depending on source semantics.

## 2.4 Epistemic evidence kinds

Current important kinds include:

- `established_fact`
- `community_report`
- `resident_question`
- `service_access`
- `official_statement`
- `commercial_offer`

Publication use is distinct from evidence kind:

- `PUBLISH`
- `CONTEXT`
- `EXCLUDE`

Do not collapse these concepts.

A `community_report / PUBLISH` item is valid publication material.

## 2.5 Rich Event Analysis

`src/processing/event_analysis.py`

Rich analysis should preserve:

- exact evidence provenance;
- official vs community status;
- uncertainty/conflict;
- operational observations;
- temporal meaning;
- source fragment IDs.

Rich analysis must not become a second verification gate that suppresses a Gate-kept local story merely because it is community sourced or single-source.

---

# 3. Geography Contract

Relevant modules include:

- `src/domain/edition_geography.py`
- `src/processing/edition_scope.py`
- `src/processing/event_triage.py`
- edition/city profiles under `data/`

The geography layer is generic per edition.

Edition profiles provide authoritative local identity such as:

- local places and aliases;
- districts and neighborhoods;
- streets and old/new names;
- edition-specific microgeography.

The resolver is **not a world geocoder**. Arbitrary external geography may still be interpreted semantically by Gate from source text.

Rules:

- source membership alone does not prove locality;
- same region/nation/front-direction terminology alone does not prove local scope;
- explicit external geography overrides assumptions based on a local source;
- writer must never decide geography;
- do not hardcode one edition's city, streets, districts, or local examples in generic production prompt code.

---

# 4. Publication Pipeline

Relevant modules include:

- `src/publication/repository.py`
- `src/publication/selection.py`
- `src/publication/selection_ai.py`
- `src/publication/event_editorial_adapter.py`
- `src/publication/generation.py`
- `src/publication/renderers.py`

Publication is a view over frozen Event-First knowledge. It must not silently reinterpret source truth.

## 4.1 Eligibility

Do not add a corroboration threshold such as `source_count >= 2` as a general publication condition.

Single-source/community material remains eligible when Gate and scope contracts allow it.

## 4.2 Selection

Selection controls presentation priority and inclusion within the current publication product. It is not a new factual-verification layer.

For digest coverage, avoid subjective omissions of legitimate local stories merely because they are less dramatic.

For articles, product behavior is governed by the city-life long-read contract in Section 0: use hierarchy/depth rather than aggressively shrinking the corpus to a few stories.

## 4.3 Versioned semantics and stale persisted payloads

When Gate, analysis, evidence-kind, or other persisted semantic meaning changes, bump the relevant semantic version and consider persisted rows/backfill behavior.

Do not assume changing prompt/code automatically changes already-persisted Story revisions.

Comparison scripts that only run:

```text
snapshot → selection → generation
```

may still be testing old persisted semantics unless the underlying active Stories have been refreshed.

When evaluating semantic changes, make sure the test corpus was produced by the current Gate/Analysis versions.

---

# 5. Digest Architecture

Relevant modules include:

- `src/publication/city_situation.py`
- `src/publication/digest_narrative.py`
- `src/publication/renderers.py`
- `src/publication/narrative_contract.py`

## 5.1 City Situation

City Situation is a point-in-time operational dashboard.

Good dimensions include actual current state for services such as:

- electricity;
- water;
- gas;
- heating;
- connectivity;
- urban transport;
- active safety status where supported.

Do not fill City Situation with questions, generic demand, or unrelated directory/service listings.

`resident_question` by itself must not create a City Situation row.

## 5.2 Scan-first digest narrative

The digest should render compact reader-facing items with strong mini-headlines and short explanatory bodies.

The LLM may synthesize closely related Stories within a deterministic rubric/block when the current contract permits it, but it may not invent Story membership or move Stories across rubrics.

The digest is not an article and should not expand every item into long prose.

## 5.3 Digest failure behavior

Single-call narrative mode may fall back to the deterministic digest when its narrative overlay is invalid.

This is intentionally different from Event-First article failure behavior.

Do not copy article fail-closed semantics onto the digest unless explicitly redesigning the product.

---

# 6. Article Architecture

Relevant modules include:

- `src/publication/article_context.py`
- `src/publication/article_length.py`
- `src/publication/article_models.py`
- `src/publication/article_claims.py`
- `src/publication/article_claim_support.py`
- `src/publication/article_semantic_support.py`
- `src/publication/article_validator.py`
- `src/publication/article_trace.py`
- `src/publication/narrative_contract.py`
- `src/article_generator.py`

## 6.1 ArticleEditorialContext

The writer receives structured `ArticleSupport` items with exact provenance.

Important metadata includes:

- support ID;
- normalized fact text;
- primary `source_text`;
- evidence kind;
- publication use;
- source role;
- fragment/source item IDs;
- timestamps;
- temporal role;
- framing.

Preserve raw/primary source text for validation and audit even if writer-facing context is sanitized.

## 6.2 Article Coverage Plan

For city-life long reads, a deterministic coverage-planning layer may classify material by editorial depth, e.g.:

- `DEVELOP`
- `WEAVE`
- `BRIEF`

The plan should help the writer understand prominence and grouping without introducing a second generative planning call.

The planner must not become a destructive publication gate for smaller legitimate stories.

## 6.3 Microdetail anchors

Coverage planning should identify strong supported details worth preserving.

Examples of useful anchor types:

- exact local place;
- concrete resident workaround;
- supported amount or interval;
- current service state;
- observable sequence of events;
- precise contrast;
- short high-value quote;
- practical effect on daily routines.

Anchors guide writer quality. Missing an editorial-quality anchor should normally be a diagnostic/quality issue, **not a hard factual publication rejection**.

Hard rejection remains the job of Evidence Boundary violations.

## 6.4 Adaptive article size

Do not enforce one fixed long-read length.

Thin days should remain concise. Rich days may expand substantially when evidence supports it.

Current product direction for rich city-life coverage allows materially more room than the earlier `800–1400 / 3–5 sections` selective-article design. Follow the current `ArticleLengthProfile` implementation/config and the latest city-life long-read spec when changing concrete defaults.

Do not pad thin days with filler.

Do not truncate rich days merely to satisfy an obsolete target from an older plan.

## 6.5 Evidence validation

Hard validation should target factual risk, not ordinary journalistic paraphrase.

Hard blockers include, where applicable:

- unknown support IDs;
- unsupported numbers/dates/times/prices;
- unsupported locations/proper names;
- unsupported causes/mechanisms;
- temporal contradictions;
- unsupported direct quotes;
- epistemic upgrades;
- question-context overclaims;
- meaningful new factual content absent from supports.

Low lexical overlap alone must not be treated as proof that a faithful paraphrase is false.

Lexical/morphological diagnostics may remain useful for debugging and benchmarking.

### Cross-language evidence

Sources may contain Russian, Ukrainian, or mixed language while the article output is Russian.

A faithful translation/paraphrase is not factual novelty merely because stems differ across languages.

Do not solve this by disabling hard factual checks.

Use deterministic normalization/equivalence only as a support tool around the real risk checks.

## 6.6 Reader prose vs Claim Atoms

Reader prose should sound like a professional local article.

Claim Atoms should remain source-close validation metadata.

A polished paragraph may map to several simple Claim Atoms.

Do not force Claim Atoms to contain every editorial connective phrase.

Do not let the writer hide an unsupported fact in prose merely by keeping it out of Claim Atoms.

## 6.7 Single-call budget and fail-closed publication

Event-First article generation uses one generative writer attempt.

If deterministic validation fails after the writer starts:

- mark the generation attempt rejected;
- fail the publication run with the stable article rejection semantics;
- create no publication row;
- queue no delivery;
- do not publish a deterministic replacement article;
- do not automatically run another writer/fact-check/repair model.

---

# 7. Comparison and Quality Evaluation

A/B comparison exists to evaluate product quality, not only whether code ran.

Useful scripts include the current repository versions of:

```bash
python scripts/compare_digest_approaches.py
python scripts/compare_article_approaches.py
python scripts/benchmark_publication_quality.py --hours 24 --edition <edition>
```

When semantic versions changed, ensure the underlying active Stories were refreshed before claiming a clean A/B.

## 7.1 Digest evaluation questions

Ask:

- Did City Situation reflect actual current operational state?
- Did resident questions stay out of operational status?
- Did useful short community information survive?
- Did obvious commercial/classified/private-noise material stay out?
- Is the result easy to scan?
- Is broad local coverage preserved?
- Is attribution honest?

## 7.2 Article evaluation questions

Ask:

- Does the article feel like a coherent city-life long read rather than a database dump?
- Are major developments given more depth than small items?
- Are meaningful secondary stories still present?
- Are supported microdetails preserved?
- Does the article avoid generic filler?
- Does commercial directory payload stay compressed or omitted?
- Does the title/lead reflect the actual article?
- Are direct quotes exact?
- Are community reports attributed correctly?
- Did Evidence Boundary reject real inventions while allowing normal paraphrase/translation?
- Was the one-call budget preserved?

Do not use "number of topics removed" as a quality metric for article improvement.

---

# 8. Repository Map

Important paths:

```text
src/
├── article_generator.py              # Event-First article coordinator + legacy article paths
├── ai_providers.py                   # Provider abstraction/configuration
├── bootstrap.py                      # Runtime/infrastructure bootstrap
├── collector.py                      # Telegram collection / legacy collection paths
├── config_loader.py                  # YAML/env dataclass configuration
├── db/                               # PostgreSQL schema/version/unit-of-work
├── domain/                           # Event/evidence/geography/operational domain models
├── ingestion/                        # Multi-source ingestion
├── jobs/                             # Procrastinate collection/processing/publication jobs
├── processing/
│   ├── fragments.py                  # Deterministic fragmentation/noise filtering
│   ├── embeddings.py                 # Fragment embeddings
│   ├── event_clustering.py           # Event-First Story clustering
│   ├── evidence_sampling.py          # Representative evidence selection
│   ├── event_triage.py               # Gate V2
│   ├── event_brief.py                # BRIEF payload synthesis
│   ├── event_analysis.py             # Rich Event Analysis
│   └── edition_scope.py              # Edition scope contract
├── publication/
│   ├── repository.py                 # Publication snapshots/candidates/inputs
│   ├── selection.py                  # Selection orchestration/fallback behavior
│   ├── selection_ai.py               # AI selection contract
│   ├── event_editorial_adapter.py    # Frozen Event-First → editorial input
│   ├── city_situation.py             # City Situation rollup
│   ├── digest_narrative.py           # Scan-first digest narrative
│   ├── article_context.py            # ArticleSupport / ArticleEditorialContext
│   ├── article_length.py             # Adaptive article profile
│   ├── article_models.py             # StructuredArticleDraft / Claim Atoms
│   ├── article_claims.py             # Deterministic concrete-claim extraction
│   ├── article_claim_support.py      # Claim/support assessment
│   ├── article_semantic_support.py   # Semantic novelty/equivalence checks
│   ├── article_validator.py          # Evidence Boundary
│   ├── article_trace.py              # Claim provenance trace
│   ├── narrative_contract.py         # Reader-facing editorial contract
│   ├── generation.py                 # Publication generation orchestration
│   └── renderers.py                  # Publication formatting
├── providers/                        # External source/provider adapters
├── repositories/                     # Domain persistence/query layer
├── runtime.py                        # Shared process runtime
├── sender.py                         # Telegram delivery
├── telegraph.py                      # Telegra.ph client
└── worker.py                         # Procrastinate worker entry point
```

Legacy modules may still exist. Check call sites before assuming they are canonical.

---

# 9. Setup and Installation

## 9.1 Environment

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements-dev.txt
playwright install chromium
pre-commit install
cp .env.example .env
cp config.yaml.example config.yaml
```

## 9.2 Telegram session

```bash
python create_session.py
```

## 9.3 PostgreSQL and migrations

```bash
docker compose up -d postgres
python scripts/migrate.py
```

When adding a migration:

- use the next migration number;
- prefer idempotent DDL where repository conventions expect it;
- update schema compatibility/version gates where required;
- add migration tests when the repository pattern requires them.

Do not hardcode the current maximum migration number into new documentation unless you have just verified it from the repository.

---

# 10. Running Telebrief

## 10.1 Main process

```bash
python main.py
```

## 10.2 Digest / article CLI

Check `python main.py --help` before assuming historical flags still exist.

Typical commands may include:

```bash
python main.py --digest --hours 24
python main.py --digest --dry-run
python main.py --article --hours 48
python main.py --article --dry-run
```

If CLI behavior differs, trust current code/help over this document.

## 10.3 Worker

Always run Procrastinate through the Telebrief worker bootstrap:

```bash
python -m src.worker --concurrency=2
```

Do not use a bare Procrastinate worker command when it bypasses Telebrief runtime initialization.

## 10.4 Docker Compose

```bash
docker compose up -d --build
docker compose logs -f telebrief-app telebrief-worker postgres
```

## 10.5 Website

```bash
cd website
npm install
npm run dev
npm run build
npm run preview
```

---

# 11. Testing Instructions

Telebrief uses pytest and has unit, integration, and PostgreSQL-backed tests.

## 11.1 Focused tests

For focused development, use `--no-cov` to avoid failing the repository-wide coverage gate when running only a subset:

```bash
pytest tests/publication/test_article_validator.py -v --no-cov
pytest tests/publication/test_article_claim_support.py -v --no-cov
pytest tests/publication/test_narrative_contract.py -v --no-cov
pytest tests/publication/test_city_situation.py -v --no-cov
pytest tests/integration/test_event_first_narrative_publication.py -v --no-cov
```

## 11.2 Full suite

```bash
pytest
# or repository make target, if present:
make test
```

## 11.3 Before claiming completion

Run the smallest tests that prove the changed behavior, then the relevant regression suite, then broader checks appropriate to the change.

Do not claim a fix is complete merely because a prompt looks correct.

For publication behavior changes, prefer regression fixtures that prove both sides:

```text
faithful paraphrase / useful community report → survives
real unsupported addition / unsafe upgrade      → rejected
```

For article-quality changes, add or maintain regression coverage for both opposite failure modes:

```text
too selective / generic → loses city-life detail
flat inventory / ads    → loses editorial hierarchy
```

---

# 12. Code Style and Engineering Rules

Follow repository Ruff/MyPy/pre-commit configuration rather than assumptions from older documentation.

Typical quality commands:

```bash
make lint
ruff check src tests
ruff format --check src tests
mypy src
pre-commit run --all-files
```

General rules:

- use modern Python typing supported by the repository target;
- follow current formatting and line-length config;
- keep async code non-blocking;
- use repository/unit-of-work patterns for DB access;
- do not create ad-hoc connection pools in jobs/services;
- use the shared runtime container where the current architecture requires it;
- preserve explicit transaction boundaries;
- do not hide exceptions that should fail a publication run;
- do not introduce a second source of truth for editorial semantics.

---

# 13. AI Provider Rules

Telebrief supports multiple provider backends through repository abstractions/configuration.

Always inspect current provider resolution before changing model behavior.

Important distinction:

```text
provider capability/failover
!=
permission to add new editorial LLM stages
```

Even if provider infrastructure supports multiple models, Event-First article publication must keep its current single generative writer-attempt architecture unless the product contract is explicitly redesigned.

Do not reintroduce:

```text
Analyzer → Writer → FactChecker → Repair → Polish
```

for the canonical Event-First article path.

Use deterministic validation and traceable evidence instead.

---

# 14. Security and Privacy

Never commit:

- `.env` files;
- API keys;
- Telegram bot tokens;
- Telegram session files/hashes;
- database credentials;
- private authentication/browser profiles.

Avoid reproducing unnecessary personal contact information from source material in publication prose.

Personal accusations, private disputes, doxxing-like content, and irrelevant personally identifying details are not ordinary city-news material.

Source retention/audit needs may differ from publication display needs: preserving raw evidence internally does not imply printing every private detail publicly.

---

# 15. Pull Requests and Commits

Before submitting substantial changes:

1. run focused tests;
2. run relevant regression suites;
3. run formatting/lint/type checks appropriate to the touched code;
4. run broader tests when feasible;
5. inspect actual generated digest/article output for editorial changes;
6. compare costs/call counts when changing AI/publication behavior.

Use clear commit messages such as:

```text
feat(article): add city-life coverage hierarchy
fix(article): preserve microdetails during compression
test(article): add city-life long-read regression
docs(editorial): update Telebrief product contract
```

---

# 16. Documentation and Plan Precedence

Telebrief has many historical plans/specs under `docs/superpowers/`.

They are useful design history but may contain superseded decisions.

When conflicts exist, use this precedence unless the user says otherwise:

```text
1. explicit current user instruction
2. Section 0 of this AGENTS.md
3. current production code + current regression tests
4. newest explicitly superseding design/spec
5. older plans/specs
6. legacy/custom behavior
```

Important known supersessions:

- old claim-first architecture is not the canonical Event-First design;
- old Event-First article deterministic fallback is superseded by article fail-closed publication;
- old "article should select only a small number of central lines" guidance is superseded by the **city-life long-read** product direction;
- old rich-article `800–1400 / 3–5 sections` targets may be superseded by the latest adaptive city-life long-read profile;
- old assumptions that community reports require official confirmation are explicitly rejected;
- old behavior that treated resident questions as facts/status is explicitly rejected.

When implementing a plan, check whether later commits/specs already implemented or superseded part of it. Do not blindly replay historical tasks.

---

# 17. Troubleshooting and Common Gotchas

## 17.1 Stale semantic payloads after Gate/Analysis changes

Symptom:

- new code appears correct;
- generated digest/article still shows old semantic behavior.

Cause:

- publication is reading persisted Story revisions created by an older Gate/Analysis version.

Action:

- inspect semantic version fields;
- refresh/backfill the active reporting window;
- verify comparison scripts are using current semantic artifacts.

## 17.2 Article rejected after apparently faithful prose

Do not immediately lower thresholds.

Inspect blocking validation issues and separate:

- true unsupported factual additions;
- unsupported locations/proper names;
- unsupported cause/mechanism;
- direct-quote fidelity errors;
- cross-language/paraphrase false positives;
- bookkeeping warnings.

Evidence Boundary should block factual risk, not ordinary grammatical variation.

## 17.3 Digest becomes empty after verification changes

Check for accidental corroboration/official-source gates.

Remember:

> community/single-source/unverified is not itself a reason to suppress a useful local report.

## 17.4 Article becomes generic after compression/planning changes

Check whether concrete support anchors disappeared.

A successful article-quality refactor should not replace specific lived details with generic editorial abstractions.

## 17.5 Article becomes an advertisement directory

Check whether raw contact/booking/price-list payload is being passed directly into reader prose without editorial compression.

Keep evidence internally; sanitize writer-facing directory payload where appropriate; preserve meaningful non-commercial facts.

## 17.6 Runtime initialization errors

If background jobs fail because runtime is not initialized, verify the worker was started through:

```bash
python -m src.worker
```

## 17.7 Focused pytest fails coverage gate

Use:

```bash
pytest <focused-test-path> -v --no-cov
```

for focused development runs.

---

# 18. Final Agent Checklist

Before changing Telebrief editorial behavior, ask:

- Am I changing knowledge semantics, publication semantics, or only presentation?
- Could this change suppress useful community reports?
- Could this change turn questions into facts?
- Could this change flatten microdetails into generic prose?
- Could this change turn an article into a bulletin board or ad directory?
- Could this change make all stories look equally important?
- Could this change reintroduce a multi-call LLM cascade?
- Could this change let unsupported locations/numbers/causes through?
- Am I testing fresh Gate/Analysis semantics rather than stale persisted payloads?
- Does the generated output actually feel useful to a resident?

For digest work, optimize for:

> **scan speed + broad useful coverage + current operational clarity.**

For article work, optimize for:

> **rich city-life coverage + editorial hierarchy + preserved microdetails + evidence fidelity.**

That is the Telebrief product.
