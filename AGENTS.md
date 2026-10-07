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
Digest · <date>

<Rubric>
<compact synthesized reader items>

<Rubric>
<compact synthesized reader items>

[Statistics only when enabled by configuration]
```

Item formatting is flexible: the writer may choose a natural sentence, a compact bullet, or an optional emoji / scan-label when it aids reading:

```text
⚡ Несколько связанных фактов об электроснабжении...
```

or:

```text
📶 **Мобильная связь.** Несколько связанных фактов...
```

Example shape:

```text
Digest · 02 сентября 2026

Коммунальная обстановка

⚡ Related electricity reports are synthesized into one concrete,
information-dense item covering locations, durations, current state,
and known repair information.

💧 Related water reports are synthesized into one item covering affected
areas, cause/status where supported, and practical water-access
information.

Связь и интернет

📶 **Мобильная связь.** Related connectivity observations, locations,
time patterns, and supported tariff changes are consolidated into one
reader item.
```

Rules:

- The digest must not contain a separate dashboard, traffic-light/status panel, or mandatory synthetic `City Situation` layer.
- Do not propose, restore, or reintroduce dashboard-style digest presentation unless the user explicitly requests it.
- Operational facts are normal editorial inputs. They belong in the appropriate thematic synthesis together with related local reports.
- Internal status/severity metadata may be used for reasoning, prioritization, validation, or grouping, but it must not dictate a reader-facing traffic-light UI.
- Reader-facing item count is independent from Story count and material fact count.
- Multiple related Stories and material facts should normally be consolidated into one coherent reader-facing item when they describe the same subject, situation, service, location pattern, or practical consequence.
- One Story must not automatically become one headline or one bullet.
- Coverage validation applies to the claims contained inside synthesized items, not to the number of visible items.
- Compression should remove repetition and fragmentation, not supported facts or concrete local detail.
- **Telegram single-message budget:** The channel digest is sized to be read in seconds as a **single Telegram post** (technical ceiling 4096 characters, target editorial range 2500–3700 characters). When source material is voluminous, Selection & Presentation prioritize high-impact urban domains (utilities, power, water, strikes/safety, connectivity, public transit, essential civic services) into a focused presentation plan (target 12–16 topic bundles). Secondary private inquiries (e.g. where to buy books, individual pet shops, routine commercial notices) are filtered at the Selection/Budgeting stage and must not inflate the digest into a multi-message document.
- **100% Story Coverage Scope:** Every selected substantive digest Story approved in `DigestPresentationPlan` must be represented in the final digest (100% final Story coverage of selected stories). Once approved into the plan, an item cannot be dropped by the writer.
- Every required material fact must be represented by at least one grounded claim in the final digest.
- Final Story coverage and material-fact coverage must remain 100%.
- A single reader-facing item may satisfy coverage for multiple Stories and multiple material facts.
- Every covered Story/fact must remain traceable to valid supporting evidence.
- `service_access` represents a concrete current or scheduled availability/access state of an external resident-facing utility or service (utilities, transport, communications, banking/municipal). Operational observations must be grounded in `service_access` evidence.
- Subjective chat disputes, sarcasm, rhetorical exclamations, and emotional neighbor complaints (e.g. «вся гора с водой круглосуточно, а тут срамота», «какое круглосуточно», «боюсь сглазить») are conversational context/frustration, NOT operational `service_access` facts, and must never be promoted to mandatory `RequiredDigestFact`s.
- **Localized contrast synthesis:** When reports from different streets/blocks within the same district exhibit differing availability (e.g. water restored on Shevchenko, but absent on Dimitrova due to a pump generator breakdown), the narrative must synthesize them as a **localized contrast** («в нагорной части ситуация неоднородная: на одних улицах... тогда как на других...»), rather than asserting mutually contradictory claims as simultaneous truths in consecutive sentences.
- Resident coping behaviors such as household generators, battery workarounds, or neighbor assistance are community reports and should be represented editorially as such rather than promoted to an operational service state.
- `resident_question` is context, not a fact and not an operational status.
- A question such as "Работает ли пенсионный фонд?" must not become "пенсионный фонд не работает" or create an operational state by itself, and must not become meta-news ("жители интересуются...") in thematic headlines.
- A useful short community report must not be discarded merely because it is conversational, single-source, or unofficial.
- A location-only clarification (for example, which district contains a street or where a landmark stands) is context, not a publishable Story or reader-facing item. Keep the location when it anchors a concrete event, service state, or practical access detail.
- Narrow deterministic causal relation validation rejects unsupported mechanism/cause claims with `UNSUPPORTED_DIGEST_RELATION`.
- Related stories may be grouped for presentation inside their deterministic rubric/block, but legitimate coverage must not silently disappear.
- An internal `SAME_SITUATION` relation is editorial navigation only. It requires a shared resolved place, service, and known reported state; it does not merge or delete fact/Story/source identities, establish continuous duration or cause, or make separate source reports identical. State a supported shared condition once where natural, while preserving each fact's distinct time, location detail, attribution, uncertainty, and scope. Keep conflicting states distinct.
- Commercial classifieds, private disputes, personal accusations, phone-number spam, repetitive ad copy, and directory-style payload must not dominate the digest.
- Community reports must preserve their epistemic status through natural attribution without duplicating attribution phrases in both headline and body.
- The digest should be compact and easy to scan, but not so aggressively compressed that meaningful local facts disappear.
- Grouped/channel digests render directly with title -> thematic blocks -> stats (when enabled); the publication lead is intentionally empty (`lead = ""`) to avoid duplicating the lead story.
- Render statistics only when statistics are enabled by configuration. When `include_statistics: false`, the final digest must contain no statistics section or synthetic replacement footer.
- Deterministic fallback rendering is strictly prohibited (`digest_allow_deterministic_fallback: false`). The digest must either be published as a cohesive, verified journalistic AI narrative or fail closed (`PublicationGenerationError`). Never publish technical message concatenations, raw fragment dumps, or fallback boilerplate.


**Digest optimization target:** broad coverage + fast scanning + operational usefulness (within the single Telegram post budget).


## 0.3 Article product contract: city-life long read

The Event-First article is a **city-life long read**.

It is not limited to 3–4 major stories. It may cover many meaningful parts of city life when the reporting window is rich enough.

Selection controls editorial hierarchy, rank, and presentation intent; it does not have subjective authority to delete a legitimate hard-eligible sealed Story.

`ArticleCoveragePlan` establishes the thematic roadmap and editorial depth (DEVELOP, WEAVE, BRIEF) for the reporting window. The AI writer must aim for broad, faithful coverage of the plan by developing key storylines and synthesizing smaller items into coherent chapters.

Mechanical coverage chasing must never degrade literary quality: never append raw fragment dumps, synthetic filler paragraphs, or deterministic boilerplate to the article draft. The article is evaluated as a cohesive journalistic long read.

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

For article writer material, omit an individual support if conservative contact/directory projection leaves it with no citable text. When suppressing a Story because projection left it with no citable material, do so only when every non-question `PUBLISH` support for that Story projects to empty text; keep a mixed Story whenever at least one support remains. This is a material-availability check, not an `ARTICLE_WORTHY` or corroboration gate. Preserve the original support and provenance internally.

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
- answers to resident questions;
- spatial proximity between disparate neighborhoods or elevation zones (never transfer relative distance locators like «буквально через два квартала» between different districts);
- routine service states from emotional chat sarcasm (never invent adverbs like «обычно» to turn neighbor envy into an operational fact).

A community report may be published as a community report. It must not be rewritten as an official or established fact unless the evidence supports that upgrade.

Recognized street aliases (e.g. Центральная = Карла Маркса = Тверская; Горбенко = Лютеранская) describe the same physical street; the writer must unify them rather than asserting mutually contradictory availability schedules for the same street in consecutive sentences.

### Direct quotes

Quotation marks mean exact primary-source wording.

- Never grammar-correct a direct quote.
- Never translate text inside quotation marks and still present it as the original quote.
- Never merge or shorten a direct quote while pretending it is exact.
- If translation, correction, or compression is needed, use indirect speech.
- Keep the words in every retained direct quote immutable during targeted editing; remove a quote into indirect speech rather than rewriting its contents.
- Avoid "chat rolls": never dump consecutive direct-speech spans joined only by list punctuation (for example, «...», «...», «...»). Synthesize multiple community reports into smooth narrative prose with geography and timelines, reserving 1–2 authentic direct quotes per section for vivid human reactions with natural attribution lead-ins.
- A quote-count heuristic that finds more than two quote spans requests local repair; it does not by itself establish a chat roll or block publication. The structural chat-roll finding requires at least two well-paired direct-speech spans separated only by list punctuation, with no intervening prose. Supported organization and place names in typographic quotes are not direct speech and do not count toward that structural finding.

### Claim Atoms

Claim Atoms are validation metadata, not sentence templates.

- Keep them short, atomic, and close to evidence.
- Reader-facing prose may be smoother and more journalistic.
- Do not force the article prose to mimic raw source syntax.
- Do not hide unsupported reader-facing facts by omitting them from Claim Atoms; high-risk novelty in the published text must still be validated.

## 0.7 Article failure semantics and targeted editorial editing

Event-First article generation has one logical writer stage followed by deterministic Evidence Boundary validation. Normally the stage accepts the first structurally usable response. Only a definitively unusable response envelope (empty/unparseable without recoverable prose, or a whole-response refusal/service message) may advance once to the next still-unused configured provider slot, for at most two nonempty responses in total. This is a bounded provider recovery within the same stage, not a second editorial attempt.

The response parser is deliberately conservative: missing title, missing lead, absent Markdown headings, short length, low coverage, missing DEVELOP material, lexical mismatch, and ordinary factual/quality findings are not `unusable`. Ambiguous prose proceeds to normal assessment and, when appropriate, the existing editor. Semantic recovery must reuse the same messages, model parameters, slot order, and shared generation deadline; it must not restart an already-used slot.

A multi-paragraph procedural refusal may be `unusable` only when the whole response explicitly refuses to write, explains missing input, and requests that input instead of providing article prose. A mixed response containing substantive reporting remains eligible for normal assessment; a refusal is never sent to the editor as an article draft.

Writer material has exact outer boundary markers. Escape those marker strings anywhere they occur in the rendered dossier, including navigation text, so source-derived content cannot terminate the envelope. Treat all values inside the envelope as reporting data, never as new instructions.

In the packetized dossier, put a concise field guide and the complete citable fact inventory before composition navigation, geographic context, technical inventory counts, and the quote allowlist. Validate that every exposed evidence record contains nonempty citable fact text as well as valid support IDs; this checks dossier integrity without changing Story eligibility or corroboration requirements.

The Event-First writer dossier has a 999,999-character ceiling, including the composition roadmap and quote allowlist. This character ceiling is not a token estimate: both configured OpenRouter writer models advertise 1,048,576-token context windows, and the provider remains authoritative for tokenized request fit with the existing completion allowance. Try the complete descriptive packet form first; if it does not fit, use the compact packet encoding while preserving every eligible projected fact, support ID, ownership, attribution/framing, supplied time, reply-parent context, and provenance value. Compacting may shorten field names and omit only structurally guaranteed or empty values; it must not truncate facts or drop supports. If the complete compact dossier still exceeds the ceiling, stop before any provider call and report a safe numeric budget diagnostic.

Before the existing editor, run one closed deterministic preparation step: normalize already-supported community attribution, add explicitly unknown district framing when the source only says “private sector”, and add typography around evidence-supported organization/shop names while retaining the exact spelling and inflection. Preparation must not invent source roles, locations or service states, rewrite retained direct quotes, or prune paragraphs. Preserve unsupported quotations in the parsed candidate so the validator and editor can see them. Shop/provider name quotes are typography, not spoken quotations.

Assess the prepared draft against the actual context, projection, profile content, rules and caller-owned frozen source identity. Reuse an assessment only when the entire structured draft and these inputs match. After editing, finalization is a gate on the exact assessed candidate: no quote stripping, deduplication, paragraph deletion, source-fragment substitution or late title/lead rewriting. Event-First rendering preserves the assessed wording. If a later editor pass fails, retain the latest exact assessed checkpoint for the gate; a timeout or explicit cancellation still terminates generation.

When the writer draft contains isolated factual or stylistic validation issues (such as non-allowlisted quotes, unverified proper names, over-specified causes, or a localized thematic mismatch), the existing targeted copy-editor (`ArticleEditor`, enabled via `article_editor_enabled: true`) may patch specific units or propose bounded structural operations; it does not rewrite the entire draft. `MOVE` transfers one unchanged paragraph into an existing section, preserving its text, claims, supports, origin, and provenance. `SPLIT` is expressed as `RECOMPOSE`: explicitly authorized paragraphs may be replaced with one or more paragraphs in an existing section, or in a `CREATE_SECTION` only when no supported existing destination fits. Rewritten prose is regrounded from its actual text against only the explicitly authorized eligible supports; the editor cannot supply Claim Atoms or support IDs. Structural operations may touch only pass-authorized units and destinations. Their positional IDs are immutable for that pass and bound to the full base-draft fingerprint; a later editor call receives a fresh registry.

All structural operations returned in one response are resolved against the same immutable base before application and form one atomic batch. The complete resulting draft is assessed, including Evidence Boundary and reader-quality checks. Any operation error, new blocker, ambiguous/global finding, or assessment error rolls back the entire structural batch to the exact pre-structure checkpoint. If the response also contains legacy text patches, only those already accepted at that checkpoint remain. Text-only patches keep their existing per-unit quarantine behavior. Each editor call re-validates the exact resulting draft and checkpoint; an unsafe final candidate still fails closed.

Event-First article generation remains limited to one logical writer stage (at most two nonempty responses only under the unusable-response rule above) and at most two calls to this existing editor, under the shared generation deadline. Digest repair uses its separate budget in §0.10. There is no extra planner, independent writer retry, or per-operation model call. Structural editing is skipped with a safe outcome if the complete article context or required eligible support context cannot fit the current budgets; a truncated article must never be presented as complete context.

Ordinary colloquial author prose is a nonblocking register repair; actual disclosure of collection mechanics remains `CHAT_KITCHEN_LEAK`. Never treat harmless colloquial wording as evidence of an unsupported fact.

Reader-quality findings use one explicit policy registry for finding class, severity, repair scope, and publication effect. This registry does not replace or weaken the Evidence Boundary validator; factual findings remain governed by `article_validator`. Unknown reader-quality codes are policy/configuration errors, not safe findings.

Only Evidence Boundary safety failures or findings that demonstrate materially misleading or unreadable published prose may block publication. A raw quote/address count, a run of one-sentence paragraphs from distinct Stories, cosmetic heading or provider-name typography, or incomplete-quantity grammar cue cannot block by itself. Compound structural checks may block when they establish a multi-place service roster without narrative relation or a section dominated by source-backed routine directory material. A report that says only “private sector” remains publishable when it is honestly attributed and its unspecified district is clarified. An asserted wrong area remains a geographic safety blocker.

Evidence Boundary is fail-closed for unverified assertions: the published article must never contain ungrounded facts.

Safe writer output that meets the Evidence Boundary is published as an authentic journalistic long read. When substantive material exists, isolated reader-quality issues on specific paragraphs are offered to `ArticleEditor` within its configured attempt budget and edits are re-validated through the Evidence Boundary. A missing `DEVELOP` storyline is a readiness diagnostic: attempt a targeted repair only when a suitable existing unit exists; if it remains missing or no suitable unit exists, report editorial acceptance as incomplete. Do not turn the finding or a numerical coverage percentage into a publication veto, and do not append filler to compensate. If an article cannot be verified, the pipeline fails closed (`ArticlePublicationRejected`). Never dump raw fragments or append artificial filler paragraphs to compensate for missing coverage.

Frozen-run preview (`scripts/preview_article.py --run-id`) is a read-only replay and never delivers or changes publication state. Its typed result is `accepted`, `rejected`, or `failed`; writer, editor, and finalization checkpoints stay in process memory and retain their exact assessment when one is available. Accepted/rejected previews require the authoritative source-bound finalization checkpoint. Failed previews may show a labelled unassessed latest candidate, with the previous assessed checkpoint identified separately. Safe diagnostics may include only allowlisted operation/unit/pass metadata, hashes and non-factual composition counts; they must omit article prose, source prose, headings, and other candidate text. Write requested Markdown and diagnostics outputs on both success and failure; label rejected candidate text `REJECTED PREVIEW — DO NOT PUBLISH`, explain when no draft exists, retain production exceptions in the typed result, and exit nonzero without printing raw exception text. Preview must not enable prompt/draft debug artifact persistence.

## 0.8 Reader hierarchy, not destructive selection

The article should feel hierarchical:

```text
major development          → DEVELOP → several paragraphs
important supporting line  → WEAVE   → one or more compact paragraphs
small useful city-life item→ BRIEF   → one compact mention
```

It should **not** feel like every source item has equal importance.

It should also **not** discard smaller legitimate material merely because it is not dramatic enough for a newspaper front page.

Selection controls presentation priority, editorial depth, and ranking within the coverage plan; it does not discard legitimate sealed candidate stories.

Support-level topic hints guide where individual evidence belongs in the prose; they are advisory navigation, not facts, causal links, geography, or publication eligibility. A Story retains one canonical composition membership even when its separate supports fit different thematic passages. Breadth, missing detail, and missing DEVELOP material remain editorial readiness concerns: they do not create a numerical coverage quota, publication veto, or reason to add filler.

The correct goal is:

> **Not less information — better organized information.**

## 0.9 Forbidden editorial regressions

Agents must not make changes whose effect is to:

- reduce the city-life long read to only 3–4 selected headlines;
- allow subjective article selection dropping legitimate sealed Stories;
- silently drop major storylines from the coverage plan;
- append raw fragment dumps, synthetic filler paragraphs, or deterministic boilerplate to the article draft;
- sacrifice literary cohesion, narrative bridges, or readability to chase mechanical coverage metrics;
- force mutually contradictory assertions in consecutive digest sentences to satisfy mechanical check-lists (when local reports vary across streets, synthesize them as localized contrast/heterogeneity);
- inflate the scan-first digest beyond the single Telegram post budget (4096 characters) by including low-priority private inquiries or directory noise when the city situation is rich with major news;
- turn the article into a disconnected collection of single-sentence bullet-like paragraphs;
- fall back to deterministic concatenation, raw fragment dumps, or legacy message-based generation for digests or articles (`digest_allow_deterministic_fallback: false`, `article_allow_deterministic_fallback: false` — fail closed: either a verified, cohesive journalistic publication or `PublicationGenerationError` / `ArticlePublicationRejected`). Technical concatenation fallbacks are strictly prohibited;
- turn digest presentation caps into knowledge-loss caps;
- require official confirmation or 2+ sources for legitimate local reports;
- treat resident questions as established facts or operational service states;
- flatten supported microdetails into generic summaries;
- force every publishable item to receive equal article space;
- let classified ads, price lists, phone numbers, booking links, or promotional copy dominate prose;
- convert a broad local article into a directory of services;
- convert a broad local article into vague high-level commentary with little concrete city life;
- weaken hard Evidence Boundary checks merely to make an article pass;
- promote a quote-count heuristic alone into a publication blocker, or rewrite words inside a retained direct quote;
- turn a raw address or quote count, repeated headings or thesis, provider-name typography, incomplete-quantity grammar, or a missing district in a faithfully attributed community report into a publication blocker;
- weaken structural blockers for an address/status roster without narrative relation or source-backed routine directory material dominating a section;
- turn a missing major-storyline readiness diagnostic or a numerical article coverage percentage into a publication veto, or append filler to compensate;
- treat a support-level topic hint as proof of a service state, causal relation, shared chronology, geographic relation, or publication eligibility;
- change article eligibility or require corroboration because an ArticleSupport has a topic hint, or suppress a useful legitimate single-source community report for lack of corroboration;
- use structural editing to change units outside the current pass's explicit allowlist, reuse stale positional IDs, transfer old Claim Atoms to rewritten prose, or partially apply a structural batch after any operation or full-candidate assessment failure;
- for Event-First articles, add a planner, independent second writer stage, per-operation model call, or editor call beyond the two-call budget or shared generation deadline; the sole exception is the bounded next-slot transition for a definitively unusable response described in §0.7. Digest editor repairs follow the separate three-call rule below;
- strengthen verification so aggressively that legitimate community news disappears;
- reintroduce claim-first per-message LLM explosion as the default processing architecture;
- hardcode one city's geography or examples into generic production prompt logic;
- leak Telegram ingestion mechanics, chat kitchen, or source channel names into reader-facing text (e.g. "перекличка", "в чатах", "в каналах", "участник чата", "в пабликах" — translate community check-ins and reports into natural journalistic attribution without revealing technical kitchen);
- transfer relative distance phrases across disparate neighborhoods or elevation zones (e.g. asserting that uphill plateau streets are "через два квартала" from downhill sea-level streets);
- treat known street aliases (e.g. Центральная / Карла Маркса / Тверская; Горбенко / Лютеранская) as separate competing streets with contradictory schedules;
- promote chat sarcasm, hyperbole, or neighbor complaints into operational baseline statements across digests or articles;
- conflate utility domains (e.g. asserting water shutoffs based on electrical outage reports);
- configure or run AI models not explicitly declared in `.env` (strict runtime allowlist enforced in `src/ai_providers.py`).


## 0.10 Digest editorial verification lessons

- Verify material delivery at the actual provider boundary: each referenced PUBLISH support must have its citable text and source identity in the editor request. A complete internal evidence map does not establish that the model received it.
- Protected numeric/time details stay attached to their owning fact, location, service and supplied conditions. Check these bindings in actual candidate prose; do not let another fact's true number or destination justify a different assertion. An unresolved binding remains NOT_EVALUATED, not a corroboration or eligibility veto.
- Geographic identifiers from municipal, colloquial and elevation views are not interchangeable. Disjoint identifiers in different views do not by themselves prove different physical locations.
- Forward the specific findings from a rejected editor candidate to the next existing call. Retain the prior exact assessed checkpoint until a replacement passes; never silently delete failing sentences or add calls to compensate.
- Fewer visible items do not by themselves mean better synthesis. Assess whether each paragraph has a clear subject and readable internal relations; replacing scattered bullets with one long service/address inventory is not an editorial success. Paragraph or character counts remain diagnostics, not new publication vetoes.
- A replay of an old frozen presentation plan tests composition, not the current Selection policy. If the old plan already approves promotional or conversational material, keep it for a faithful replay and report the upstream defect separately; never silently delete it in the writer to improve a quality score.
- A technical `accepted` result, valid fact IDs and 100% membership coverage do not establish semantic fidelity or readable journalism. Report `NOT_EVALUATED` bindings explicitly; never describe them as verified. Assess the exact rendered prose against its own supporting evidence as well as the selected Story/fact inventory.
- Trace defects through the original source, writer-facing material, raw writer response, each existing editor response and final rendering before changing prompts or validators. The editor may introduce ambiguity or fragmentation into a better writer draft; assess the resulting candidate, not the intended repair.
- Observation timestamps remain internal provenance for validation; do not expose them as digest event dates in writer/editor fact rows. Preserve explicit source dates and times in citable text. This digest projection rule does not change the article dossier contract.
- Keep a fare attached to the paid journey leg. A passing bus's final destination is not necessarily the destination purchased for that price. Preserve the supplied leg in writer material and edited prose; do not infer a missing leg from the route name.
- Technical support aliases must retain their canonical evidence/source identity and consistent source-role metadata. Multiple IDs pointing to the same evidence are not additional witnesses. An ambiguous alias must not be assigned an invented single source identity.
- `reply-parent`, `parent message` and «родительское сообщение» in metadata mean the preceding message in a reply chain. They establish neither a family relationship nor an operational fact. Preserve genuine family testimony, but omit collection mechanics from reader prose. Source-role wording must follow the evidence: several messages do not by themselves prove several people or groups of residents.
- Text serialized as `in_reply_to` is not citable wording from the current author. Exclude the parent text from digest fact records, Claim Atoms, citable support text, and quote allowlists. When the writer dossier needs reply context, retain it only in an explicitly labelled `background_only_not_citable` field; preserve the original message and parent linkage internally for provenance.
- Direct-quote fidelity includes internal punctuation and numeric spelling. Typography around a quote may change; its contents must remain exact. If prose requires correction or compression, use faithful indirect speech. Do not silently alter or strip a failed quotation during finalization.
- Deterministic attribution cleanup must preserve surrounding predicates, prepositions, facts and quoted spans. A generic pattern must not consume an arbitrary word following “chat” or “channel”. Verify full sentences and repeated application, not only regex matches.
- Compare changes on frozen, identical inputs and retain failed/rejected runs. Reused development windows are not independent holdouts; manual self-review is not blind review. A targeted defect disappearing does not establish overall quality improvement. Model upgrades require demonstrated reader benefit, not technical acceptance alone.
- Improve material organization and bounded editing without suppressing useful single-source reports, adding stylistic publication vetoes, or adding model stages. Keep editorial readiness separate from Evidence Boundary safety. Store experiment-specific scores, costs and prose outside this canonical contract.

## 0.11 Target reader experience

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
        ├── Digest: coverage-preserving selection -> DigestPresentationPlan -> thematic AI synthesis and bounded editing -> evidence/coverage validation -> DigestCoverageTrace -> publication
        └── Article: coverage-preserving selection -> ArticleCoveragePlan -> writer attempt -> targeted validation/editing -> ArticleClaimTrace -> publication
        ↓
Delivery
```

The canonical design is **Event-First**, not claim-first.

Legacy floor guards offline regressions; Event-First truth drives production. Presentation budgets never redefine publishable knowledge.

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

### Broad-region scope guard

Gate v7 outputs `scope_basis_fragment_ids` identifying which fragments support the scope classification. A narrow deterministic guard (`broad_region_without_focus_impact`) normalizes broad regional news (e.g. general oblast-wide reports, frontline summaries, or non-focus settlements) to `OUT_OF_SCOPE / DROP` unless the cited scope-basis fragments specifically mention the edition's focus places.

### Canonical service state representation

Service availability truth is unified in `EvidenceItemPayload.service_state: ServiceStatePayload`. LLMs output nested `service_state` within `service_access` evidence items only; there is no separate top-level LLM-authored operational observations array. Deterministic pipeline normalization (`normalize_service_state_evidence` / `derive_operational_observations`) cleans states and projects them into `OperationalObservationPayload` for downstream rollups and card building.

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
- `service_access` (carries nested `service_state: ServiceStatePayload`)
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
- canonical service-state evidence items (`service_access.service_state`);
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
- `src/publication/digest_presentation.py`
- `src/publication/digest_narrative.py`
- `src/publication/digest_relation_support.py`
- `src/publication/renderers.py`
- `src/publication/narrative_contract.py`

## 5.1 City Situation

City Situation is a point-in-time operational dashboard rendered deterministically from `DigestPresentationPlan`.

Good dimensions include actual current state for services such as:

- electricity;
- water;
- gas;
- heating;
- connectivity;
- urban transport;
- active safety status where supported.

Rules:
- Mixed positive and negative availability observations for the same subject/dimension consolidate into a single `CONFLICTING` group rendering yellow (🟡) with both positive and non-positive detail lines before capping.
- Pure positive statuses render as distinct subject-coherent dashboard groups (🟢) up to `digest_city_situation_max_positive_items` (default 2), reserving at least 1 slot on mixed days. There is no global catch-all `available_services` group.
- The LLM does NOT author or rename dashboard groups; dashboard presentation is fully deterministic across single-call, deterministic, and fallback modes.
- `resident_question` by itself must not create a City Situation row.


## 5.2 Scan-first digest narrative

The digest should render compact reader-facing items with strong mini-headlines and short explanatory bodies.

Rules:
- Thematic items act according to canonical presentation modes (`DASHBOARD_ONLY`, `DETAIL_ONLY`, `DASHBOARD_AND_DRILLDOWN`).
- Stories overlapping the dashboard with distinct microdetails are marked `DASHBOARD_AND_DRILLDOWN` and must cite distinct detail evidence rather than duplicating dashboard status.
- Stories overlapping the dashboard without distinct microdetails are marked `DASHBOARD_ONLY` and omitted from thematic blocks, while fully represented in City Situation.
- Every selected substantive digest Story must be represented in the final digest (100% final Story coverage).
- Narrow deterministic relation safety checks (`find_unsupported_digest_relations`) reject unsupported causal claims (e.g. invented causal mechanisms like "Авария на подстанции оставила Гору без света" from pure outage reports) with code `UNSUPPORTED_DIGEST_RELATION`.
- The LLM may synthesize closely related Stories within a deterministic rubric/block when the current contract permits it, but it may not invent Story membership or move Stories across rubrics.

## 5.3 Digest failure behavior

Single-call narrative mode falls back to the deterministic Event-First digest draft built from `DigestPresentationPlan` when its narrative overlay is invalid or missing, guaranteeing 100% final Story coverage.

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

`ArticleSupport` may carry an optional service-subject hint projected from structured `service_access.service_state`. Only exact keys map to known families: `water_supply` → `water`, `power_supply` → `power`, `gas_supply` → `gas`, `heating` → `heating`, and `connectivity` → `telecom`. The hint preserves the structured subject key and label; a generic family is never inferred from a localized label or `dimension`. Unknown subjects remain unmapped. Existing lexical service-family hints may also be derived from that support's text, so one support can have several advisory themes. These hints describe navigation for the writer, not the truth of a claim, service availability, or a relationship between supports.

Keep one canonical composition group per Story. Evaluate a thematic mismatch from the supports cited by the actual paragraph, not from the Story's overall topic. Unknown themes alone do not establish an error. A mixed-theme bridge is recognized only when the prose and one cited projected support that carries both themes express the same causal or temporal connection; contrast phrasing alone is not enough. Such composition findings use the existing non-blocking repair/readiness policy and cannot create an eligibility gate.

## 6.4 Adaptive article size

Do not enforce one fixed long-read length.

Thin days should remain concise. Rich days may expand substantially when evidence supports it.

Current product direction for rich city-life coverage allows materially more room than the earlier `800–1400 / 3–5 sections` selective-article design. Follow the current `ArticleLengthProfile` implementation/config and the latest city-life long-read spec when changing concrete defaults.

For the daily rich profile, the soft upper target is `min(config.article_max_words, profile.hard_max_words)` (up to 2400 words with the current configuration). Do not enforce a lower target. The composition richness summary exposes only counts of distinct planned support themes, DEVELOP lines, and distinct normalized detail anchors; these counts are editorial context, not word quotas, coverage guarantees, or publication gates. Existing hard word and section limits retain their current semantics, and other profile targets remain unchanged.

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

Structural editing must preserve the same boundary. A `MOVE` keeps the paragraph and all its evidence metadata unchanged. `RECOMPOSE` builds claim metadata from the resulting prose and the operation's explicit union of eligible source supports; matching support IDs alone do not validate new wording. Direct-quote words remain immutable, although a supported paraphrase may use indirect speech. Run Evidence Boundary, quote, geography, service-state, and reader-quality/structural checks on the complete candidate after each structural batch. If that candidate has a new blocker, cannot be assessed exactly, or has a finding that cannot be mapped reliably to a changed unit, restore the exact pre-structure checkpoint. This rollback preserves a safe base; it does not weaken fail-closed publication when the final candidate itself is unsafe.

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

For structural edits, the editor returns prose and bounded operations, not Claim Atoms or support IDs. The server rebuilds claims for rewritten paragraphs from the actual output and re-grounds them only to the operation's authorized eligible supports. A successful candidate must retain an exact full-draft assessment; paragraph-local review alone is insufficient.

## 6.7 Single-call budget and fail-closed publication

Event-First article generation uses one logical writer stage, followed by Evidence Boundary validation and optional targeted copy-editing (`ArticleEditor`) for isolated issues. A structurally unusable response may advance once to the next unused configured provider slot, with a maximum of two nonempty responses for the stage; no other response may trigger another writer request. Missing title/lead or headings remain repairable format findings, not provider-recovery triggers. The editor may be called at most twice, and all writer/editor work shares the generation deadline. Do not add a planner, an independent writer retry/stage, or a per-operation model call.

Before each editor call, construct an immutable pass-local registry of structural unit IDs bound to the fingerprint of the complete base draft. Resolve every source, destination, and insertion reference against that same base before changing anything; IDs are not stable across passes. Give the editor the complete article as read-only orientation plus an explicit allowlist of editable units and permitted destinations. Required support packets must be eligible and complete: include all unit, claim, issue and structural-source citations. Text-patch limits apply per unit (64 packets, 32,000 characters total, and 4,000 characters per packet); structural limits apply to the required batch union, along with the full prompt/context budget. If the complete article or required supports do not fit, skip the structural operation with a safe reason; never silently truncate the context and call it complete.

Do not truncate an oversized support packet or silently omit required evidence. Defer that unit with a safe reason. Prioritize factual/kitchen blockers, then misleading composition, then cosmetics. Within each class, the second existing call prioritizes unattempted deferred units before partial progress and no-progress repeats. Record allowlisted unit/pass outcomes for malformed output, absent/no-op patches, unknown units, budget deferral, quarantine, application and rollback; no additional model call is permitted.

Apply one response's structural operations atomically, then assess the complete candidate and retain a checkpoint for that exact draft and input fingerprint. On an operation conflict, new blocker, ambiguous/global finding, or assessment error, roll the structural batch back to its exact pre-structure checkpoint. A subsequent editor pass builds a fresh registry and must assess its own exact result. Structural rollback is not partial positional quarantine; existing text-only patch quarantine remains in force.

Safe preview diagnostics may report allowlisted structural operation metadata (such as operation type, status, and safe reason/identifier) and composition counts, but must never persist article prose, source text, heading text, or candidate text in ordinary metadata. Frozen-run preview remains read-only for publication and delivery.

If validation fails or cannot be resolved:

- mark the generation attempt rejected;
- fail the publication run fail-closed (`ArticlePublicationRejected`);
- create no publication row;
- queue no delivery;
- do not publish a deterministic replacement article or concatenate raw source fragments.

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

Even if provider infrastructure supports multiple models, Event-First article publication keeps one logical generative writer stage. The only allowed semantic provider transition is once to the next unused configured slot after a definitively unusable response; this yields at most two nonempty responses and reuses the same request and deadline. Coverage, factual, or reader-quality findings never authorize another writer stage. Other publication consumers retain the ordinary first-nonempty ProviderCascade behavior.

Do not reintroduce:

```text
Analyzer → Writer → FactChecker → Repair → Polish
```

for the canonical Event-First article path.

Use deterministic validation and traceable evidence instead.
 
## 13.1 Model Configuration & Allowlist

Model declarations are configured exclusively via `.env`, NEVER in `config.yaml`.
`config.yaml` must not declare `ai_model`.

- **Primary Model**: `OPENROUTER_MODEL=anthropic/claude-haiku-5.5,z-ai/glm-5.3-flash` (comma-separated slots: primary, then fallback)

**STRICT MODEL ALLOWLIST**:
- Telebrief strictly enforces a runtime allowlist in `src/ai_providers.py` (`validate_model_allowed`).
- ONLY models explicitly configured in `.env` (`OPENROUTER_MODEL`, `OPENROUTER_MODEL_2`, `OPENROUTER_IMAGE_MODEL`, `OPENAI_MODEL`, `AI_MODEL`, `GEMINI_MODEL`, `EMBEDDING_MODEL`) are permitted to execute.
- Any attempt to call an unconfigured model fails immediately before any network request with a `ValueError`.


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

## 17.8 Legacy Floor Parity and Offline Audit Verification

When auditing Event-First publication parity against historic/legacy floors:

1. **Compare against frozen legacy floors**: Offline legacy floor evaluation compares Event-First runs against frozen legacy floor fixtures (`tests/fixtures/*_legacy_floor.json`), never live legacy pipeline executions.
2. **Independent raw source corpus**: The source corpus in exported regression cases must represent real raw edition source fragments inside the lookback window, independently of whether stories were formed.
3. **Canonical reader-visible text for microdetails**: Microdetails are measured strictly from the exact canonical published reader text (`dashboard_texts` and `detail_texts` in `DigestCoverageTrace`, or assertion assertions in `ArticleClaimTrace`). Do not use raw source presence as a shortcut for publication retention.
4. **Mandatory presentation plans**: `DigestPresentationPlan` is mandatory audit metadata in publication records (`pub.metadata["digest_presentation_plan"]`). Missing presentation plan metadata triggers `MISSING_DIGEST_PRESENTATION_PLAN_METADATA` and fails digest parity audits.
5. **100% digest final trace Story coverage**: Every story selected into `DigestPresentationPlan` must be represented in `DigestCoverageTrace` (`final_digest_story_coverage = 1.0`).

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

### Digest editorial repair checkpoints

Thematic digest editing may reorganize authorized blocks on the first editor call. The second call repairs remaining factual or prose findings. A third and final call is allowed only for a fully covered candidate whose remaining issue is text size, or for specific reader-quality warnings on an otherwise safe candidate. For a size-only failure, pass the exact latest assessed candidate to the final editor, preserve every selected Story and required fact, and recompose the authorized themes to fit Telegram and per-item limits. A length-failing candidate is input for repair, never a publishable checkpoint; the final candidate must pass the complete canonical assessment. Keep the configured repair deadline. Never truncate text or remove selected facts to meet a character limit. Article editing retains its separate two-call ceiling. A source-backed measurement repeated from one approved fact is a repair advisory, not a publication blocker or corroboration requirement. Quarantine edits that introduce repetition or ambiguous fare meaning into an already safe checkpoint; retain the exact previous assessment. Equal measurements in separate approved facts are not proof of duplication. A successful replay of a previously used frozen run remains a regression check, not an independent quality evaluation. Inspect actual prose and evaluate new reporting windows with current Selection before claiming editorial quality.

A single speaker's unlocated statement that their own light, water and gas are available, without a change, interruption, practical detail or other local anchor, is private status chatter and may be left out of the scan-first digest before its presentation plan is approved. Keep the evidence internally. Preserve localized positive availability reports, outage/repair changes, practical consequences, and reports from multiple distinct source items.
