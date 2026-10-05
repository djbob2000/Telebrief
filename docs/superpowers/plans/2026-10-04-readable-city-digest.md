# Связный городской дайджест — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Пользователь запретил субагентов: все задачи и проверки выполняются лично, последовательно. Предложения согласованы заранее; дополнительных вопросов не требуется.

**Goal:** Получать устойчиво связный, полезный городской дайджест с сохранением всех выбранных Stories, фактов и одиночных сообщений жителей.

**Architecture:** Существующие writer, editor, evidence validation и Telegram renderer остаются production-компонентами. Добавляются фиксированные replay inputs, единый assessment, компактное полное writer-досье и разрешённая редактура целых тематических блоков. Сравнения выполняются отдельно от production, без независимого AI-критика.

**Tech Stack:** Python, существующие dataclasses/config parser, pytest, Ruff, mypy, текущий AI provider, канонический RenderedDigestArtifact. Новые runtime-зависимости и миграции БД не требуются.

**Spec:** [Редакционная спецификация](../specs/2026-10-04-readable-city-digest-design.md).

## Global Constraints

- `AGENTS.md` §0 имеет приоритет над этим документом и историческими планами.
- 100% выбранных substantive Stories и 100% RequiredDigestFacts должны сохраниться. Покрытие подтверждается содержанием grounded claims, а не только перечислением ID.
- Одиночное полезное сообщение жителя допустимо с честной атрибуцией. Число источников и официальное подтверждение не становятся новыми gates.
- Сохраняются суммы, сроки, места, конкретные действия жителей и полезные ограничения знания. Поддержанные прямые цитаты неизменны.
- Один Telegram post: существующий канонический artifact должен пройти лимит 4096, включая проверку UTF-16. 2500–3700 — ориентир при достаточном материале, не обязательный минимум.
- `digest_allow_deterministic_fallback: false`. Никакой склейки фрагментов или наполнителя.
- Одна авторская стадия и не более двух вызовов существующего редактора; общий срок генерации сохраняется. Никаких отдельных production planner/critic вызовов.
- Article pipeline, eligibility, знания и production Selection не меняются в этой работе.
- Стилевые наблюдения не получают самостоятельного publication veto; существующие factual/render blockers сохраняются.
- Выбранные факты не выкидываются ради литературного балла, неизвестное место не дополняется догадкой, источник не превращается в официальное подтверждение.

## Review Focus

1. Summary-only Story без required facts должен пережить полную перекомпоновку блока — тест задачи 5.
2. Семейное сообщение без района должно встроиться с ограничением знания, без приписывания района соседнего предложения — тест задач 3 и 5.
3. Две разные улицы и два имени одной улицы требуют разной географической обработки — тесты задачи 3.
4. Безопасный writer с нулём regex warnings пропускает production редактор; обязательный обзор доступен только в offline эксперименте. No-op не означает сбой публикации — тест задачи 6.
5. Ошибка второго редактора, cancellation и непоместившийся контекст не должны превращаться в доставку непроверенного кандидата — тесты задач 5 и 6.

## Карта файлов и границы

Новые production-модули:

- `src/publication/digest_assessment.py` — общий assessment точного кандидата, извлечённый из generation без изменения правил.
- `src/publication/digest_writer_material.py` — только построение компактного writer-facing досье.
- `src/publication/digest_edit_scope.py` — immutable разрешение на структурную правку, fingerprint и membership.

Новые offline-модули:

- `scripts/digest_evaluation/fixtures.py` — versioned inputs и проверка полноты снимка.
- `scripts/digest_evaluation/replay.py` — изолированный запуск существующих writer/editor и assessment.
- `scripts/evaluate_digest.py` — CLI сравнения и безопасный отчёт.
- `tests/fixtures/digest_editorial/` — synthetic JSON inputs/ожидаемые ограничения, без live исходников.
- `docs/editorial/digest-quality-evaluation.md` — manifest сценариев, оценочная шкала, результаты и rollout журнал.

Меняются существующие `digest_narrative.py`, `digest_editor.py`, `generation.py`, `digest_quality_diagnostics.py`, digest-часть `narrative_contract.py`, `src/config/schemas/publication.py`, `src/config/parsers/publication.py`, `config.yaml.example`. `src/config_loader.py` остаётся compatibility export; новые поля определяются в schema/parser.

Точные line numbers при исполнении брать из актуальной ветки; названные ниже functions являются границами изменения. Не воспроизводить уже реализованные исправления старых планов.

---

## Задача 1. Зафиксировать inputs и общий assessment

**Files:** create `src/publication/digest_assessment.py`, `scripts/digest_evaluation/fixtures.py`, `tests/test_digest_assessment.py`, `tests/test_digest_evaluation_inputs.py`, synthetic fixtures; modify `src/publication/generation.py::_evaluate_candidate`.

**Interfaces:**

- `DigestAssessmentContext`: frozen dataclass с `frozen: FrozenEditorialInput`, `plan: DigestNarrativePlan`, `presentation_plan: DigestPresentationPlan`, `evidence: Mapping[str, PublicationEvidence]`, `support_text_by_id: Mapping[str, str]`, `allowed_context_terms: tuple[str, ...]`, `snapshot_at: datetime`, `timezone_name: str`, `renderer: PublicationDigestRenderer`.
- `DigestAssessment`: frozen dataclass с exact `draft: DigestNarrativeDraft`, `validation: DigestNarrativeValidationResult`, `coverage: DigestCoverageTrace`, `artifact: RenderedDigestArtifact`, `audit: DigestQualityAudit`.
- `assess_digest_candidate(draft: DigestNarrativeDraft, *, context: DigestAssessmentContext) -> DigestAssessment`. Нормализация выполняется до получения checkpoint; assessment сохраняет именно оценённый structured draft. Повторные поздние преобразования текста запрещены.
- `FrozenDigestCase`: versioned container с полным `DigestAssessmentContext`, cards, конфигурацией генерации, моделью/параметрами и implementation versions. JSON codec явно кодирует поля текущих dataclasses и существующих моделей; не pickle и не live lookup. Renderer хранится как параметры rubric/settings и восстанавливается локально; объект renderer и provider не сериализуются.
- `load_digest_case(path: Path) -> FrozenDigestCase`, `write_digest_case(case: FrozenDigestCase, path: Path) -> str` возвращает canonical content hash.

- [ ] Написать `test_assessment_matches_current_generation_checks`: одинаковый candidate даёт прежние validation, coverage, artifact hash и audit; неподтверждённое число отклоняется, одиночный community report сохраняется.
- [ ] Написать `test_case_roundtrip_preserves_all_writer_inputs`: после JSON roundtrip равны supports/aliases, exact quotes, reply context, summaries, locations, timestamps, plan ownership, rules и versions; изменение любого поля изменяет hash.
- [ ] Написать `test_missing_required_input_rejected_before_provider_call`: неполный снимок не восполняется из runtime/БД.
- [ ] Выполнить `.venv/bin/pytest tests/test_digest_assessment.py tests/test_digest_evaluation_inputs.py -q --no-cov`; подтвердить осмысленный FAIL на отсутствующем новом поведении.
- [ ] Извлечь общий assessment и codec. Production callback и replay используют одну функцию, а не две реализации validators. Сохранять действующие safety semantics.
- [ ] Повторить тесты и `tests/test_digest_synthesis.py tests/test_digest_composition_identity.py`; ожидается PASS и одинаковый artifact для прежнего режима.
- [ ] Коммит: `refactor(digest): share exact candidate assessment and sealed replay inputs`.

## Задача 2. Изолированный replay и исходная оценка

**Files:** create `scripts/digest_evaluation/replay.py`, `scripts/evaluate_digest.py`, `tests/test_digest_evaluation_replay.py`, `docs/editorial/digest-quality-evaluation.md`.

**Interfaces:**

- `DigestReplayResult`: status `accepted|rejected|failed`, final assessed checkpoint при наличии, allowlisted diagnostics, provider-call counters и elapsed time. Label rejected text; unassessed latest candidate не выдавать за финальный output.
- `async replay_digest(case: FrozenDigestCase, *, provider: Any) -> DigestReplayResult` использует текущий writer, `_repair_digest_candidate`, общий assessment и in-memory observer.
- CLI: `--fixture PATH --variant baseline|compact|thematic|combined --repeat N --output-dir PATH`. Variants выставляют только согласованные flags. Нельзя неявно включать новые настройки для baseline.
- Output: явно запрошенный final text и safe JSON с hashes/versions/IDs/counts/check codes/calls/duration/cost availability. Без prompts, source prose и промежуточных drafts.

- [ ] Написать `test_replay_never_touches_runtime_database_or_delivery`: forbidden bootstrap/runtime/DB/job/delivery методы падают при вызове; accepted replay с fake provider всё равно работает.
- [ ] Написать `test_rejected_and_failed_outputs_label_checkpoint_status`: неуспех сохраняет diagnostics, rejected preview помечен, отсутствие draft описано, raw exception/source text отсутствуют.
- [ ] Запустить `.venv/bin/pytest tests/test_digest_evaluation_replay.py -q --no-cov`; подтвердить FAIL, реализовать replay/CLI, повторить до PASS.
- [ ] Зафиксировать шесть сценариев и split 4 development / 2 holdout до настройки prompts. Реальные inputs сохранять явно в игнорируемом `data/previews/digest-evaluation/`; в Git — synthetic cases и безопасные manifests. Если существующий run не содержит полного frozen input, реконструировать и запечатать снимок однократно, явно обозначив это в manifest.
- [ ] Выполнить baseline на четырёх development случаях дважды и записать оценки каждого текста, failures, timings и calls. Эти 8 генераций входят в общий начальный бюджет 16, не прибавляются к нему повторно.
- [ ] Коммит: `test(digest): add isolated editorial replay and baseline cases`.

## Задача 3. Компактное полное авторское досье

**Files:** create `src/publication/digest_writer_material.py`, `tests/test_digest_writer_material.py`; modify `digest_narrative.py::_generate_composition_draft`, digest-часть `narrative_contract.py`, publication schema/parser и `config.yaml.example`.

**Interfaces:**

- `build_compact_digest_material(*, plan: DigestNarrativePlan, evidence: Mapping[str, PublicationEvidence], cards: Sequence[StoryCard]) -> dict[str, Any]`: `facts`, `supports`, `units`, `blocks`, `relations`, quote allowlist. Таблицы имеют уникальные IDs и полные связи из спецификации §6.1.
- `PublicationEditorialConfig.digest_writer_material_format: Literal['legacy', 'compact_v1'] = 'legacy'`; parser и dataclass validation отклоняют неизвестные значения.
- `DigestNarrativeWriter` принимает этот mode как keyword-only параметр конструктора с default `legacy`; новые inputs проходят прежний output parser/validator.

- [ ] Написать `test_compact_material_preserves_exact_membership_and_distinct_supports`: повторные ссылки не дублируют записи, все разные разрешённые тексты сохранены, fact/support/unit/story sets совпадают с прежним payload.
- [ ] Написать `test_unknown_household_location_and_single_source_are_preserved`, `test_street_aliases_do_not_create_new_geographic_relations`, `test_quotes_reply_context_and_precise_tariff_commands_survive`. Числа, команды и quotes сравниваются посимвольно.
- [ ] Написать `test_compact_material_budget_never_truncates_facts`: непоместившийся полный input даёт явную диагностическую ошибку до provider call; synthetic text превышает действующий context budget provider adapter. Не вводить произвольный новый лимит числа фактов или supports.
- [ ] Написать `test_material_format_default_and_unknown_value`; legacy default сохраняет прежний payload, неизвестный enum — config error.
- [ ] Запустить `.venv/bin/pytest tests/test_digest_writer_material.py tests/test_digest_source_material.py -q --no-cov`, подтвердить FAIL новых tests; реализовать tables и mode. Validator support alias index не менять.
- [ ] Упростить writer brief до редакционной задачи и существующих safety правил; не добавлять новые обязательные поля источника и квоты абзацев.
- [ ] Повторить tests, затем `.venv/bin/pytest tests/test_digest_*.py -q --no-cov`; ожидается PASS.
- [ ] Провести compact-вариант на тех же четырёх inputs дважды: оставшиеся 8 генераций начального бюджета. Если нет наблюдаемого улучшения, документировать отрицательный результат и пересмотреть досье перед следующими model-backed экспериментами.
- [ ] Коммит: `feat(digest): add complete compact writer material behind configuration`.

## Задача 4. Редакционные наблюдения без новых запретов

**Files:** modify `src/publication/digest_quality_diagnostics.py`; create `tests/test_digest_editorial_readiness.py`; update evaluation document.

**Interfaces:**

- `DigestEditorialReadiness`: frozen dataclass `observations: tuple[DigestQualityWarning, ...]`, `semantic_review_status: Literal['not_evaluated', 'reviewed']`. Баллы offline оценки не присваиваются regex-коду.
- `assess_digest_editorial_readiness(draft: DigestNarrativeDraft) -> DigestEditorialReadiness` использует прежние advisory observations; надёжно обнаруживаемые новые формы source-meta и tautology имеют только advisory эффект.
- Safe metadata содержит codes/block IDs/item IDs и status, без warning text, headlines и source prose. Не включать новые advisory codes в `_BLOCKING_PROSE_CODES`.

- [ ] Написать `test_named_chat_meta_is_advisory`, `test_safe_single_sentence_is_not_fragmentation_blocker`, `test_zero_observations_is_not_semantic_quality_certificate`.
- [ ] Написать `test_readiness_does_not_change_coverage_or_publishability`: один и тот же safe candidate остаётся publishable при стилевом observation.
- [ ] Запустить `.venv/bin/pytest tests/test_digest_editorial_readiness.py -q --no-cov`, подтвердить FAIL; реализовать readiness/metadata; повторить до PASS.
- [ ] Запустить существующие `tests/test_digest_compact_prose.py tests/test_digest_attribution.py`; ожидается PASS.
- [ ] Коммит: `feat(digest): report editorial readiness separately from safety`.

## Задача 5. Разрешённая редактура полных тематических блоков

**Files:** create `src/publication/digest_edit_scope.py`, `tests/test_digest_thematic_edit_scope.py`; modify `src/publication/digest_editor.py::_polish_composition` и `polish_and_compress`.

**Interfaces:**

- `DigestBlockEditScope`: frozen dataclass с base fingerprint, block IDs, item IDs и tuples ожидаемых fact/unit/story/support memberships.
- `build_digest_block_edit_scope(draft: DigestNarrativeDraft, *, plan: DigestNarrativePlan, block_ids: Sequence[str]) -> DigestBlockEditScope`.
- `validate_digest_block_replacement(base: DigestNarrativeDraft, replacement: DigestNarrativeDraft, *, scope: DigestBlockEditScope, plan: DigestNarrativePlan) -> None`; ошибка — `DigestRecompositionError`, никакого частичного результата.
- `DigestEditor.polish_and_compress` получает optional keyword `edit_scope: DigestBlockEditScope | None = None`. Legacy вызовы совместимы. Replacement schema не смешивается с text patch schema.

- [ ] Написать `test_whole_block_recomposition_preserves_summary_only_story`: можно объединить fact-bearing и summary-only items; source membership и coverage summary Story остаются полными.
- [ ] Написать `test_unspecified_household_woven_without_invented_area`, `test_unrelated_block_is_unchanged`, `test_stale_fingerprint_and_lost_fact_rollback_entire_batch`.
- [ ] Написать `test_known_id_without_grounded_claim_is_not_enough`: все fact IDs перечислены, но replacement подменяет материал; общий assessment не принимает кандидата как доказанно безопасный. Не скрывать существующие `not_evaluated` ограничения.
- [ ] Написать `test_complete_context_budget_skip_before_provider`: при недостаточном бюджете провайдер не получает урезанное досье.
- [ ] Запустить `.venv/bin/pytest tests/test_digest_thematic_edit_scope.py -q --no-cov`, подтвердить FAIL; реализовать полный scope, schema и atomic validation.
- [ ] Выполнить общий assessment для replacement, не доверять возвращённым AI claims/support IDs. Summary-only поддержка проверяется существующим механизмом явно разрешённых supports.
- [ ] Повторить новые tests и `tests/test_digest_synthesis.py tests/test_digest_composition_identity.py`; ожидается PASS.
- [ ] Коммит: `feat(digest): authorize atomic thematic edits with complete unit coverage`.

## Задача 6. Встроить редактуру в действующие лимиты

**Files:** modify `src/publication/generation.py::_digest_repair_request`, `_repair_digest_candidate`, digest generation; schema/parser и `config.yaml.example`; create `tests/test_digest_editor_budget.py`.

**Interfaces:**

- `PublicationEditorialConfig.digest_editor_scope: Literal['targeted_items', 'thematic_blocks'] = 'targeted_items'`.
- `_repair_digest_candidate` получает keyword `editor_scope: str = 'targeted_items'`, сохраняет возвращаемый checkpoint/used/call count contract.
- Новый mode даёт первый scope на полные существующие блоки при наличии usable writer candidate и замечаний. Вариант без findings проверяется только offline флагом `--review-clean-text`; отклонён для production после измеренного роста стоимости 2,806x. Последующий scope пересчитывается по exact checkpoint. Второй вызов только при unresolved findings/rejected edit.
- Observer safe metadata `editor_outcome` имеет значения из spec §6.3. `unchanged_safe` может завершить редактуру успешно без изменения текста; не считать его factual failure.

- [ ] Написать `test_zero_warnings_still_get_one_editorial_call_in_thematic_mode` и `test_legacy_mode_zero_warnings_still_skips_editor`.
- [ ] Написать `test_noop_safe_edit_does_not_force_second_call`, `test_second_unsafe_edit_retains_first_exact_assessed_checkpoint`, `test_total_editor_calls_never_exceed_two`.
- [ ] Написать `test_timeout_and_cancellation_follow_existing_generation_contract`, `test_skipped_context_budget_returns_existing_checkpoint_for_final_gate`, `test_unknown_scope_config_rejected`.
- [ ] Запустить `.venv/bin/pytest tests/test_digest_editor_budget.py -q --no-cov`, подтвердить FAIL; реализовать mode, statuses, deadline propagation, fresh scope и финальную проверку exact candidate. Никакого нового provider slot или отдельной critic stage.
- [ ] Повторить `.venv/bin/pytest tests/test_digest_*.py -q --no-cov`; ожидается PASS.
- [ ] Выполнить `.venv/bin/pre-commit run --all-files`; требуются успешные применимые Ruff/mypy hooks. Расширять testing при фактической новой проблеме, не повторять зелёные проверки без изменения.
- [ ] Коммит: `feat(digest): use bounded thematic editing without extra generation stages`.

## Задача 7. Сравнить тексты и принять решение о rollout

**Files:** update `docs/editorial/digest-quality-evaluation.md`; create `tests/test_digest_evaluation_reporting.py`; extend `scripts/evaluate_digest.py` reporting.

**Interfaces:** CLI вариантов задачи 2. Comparison report содержит все outcomes, не только лучшие тексты; summary включает 5 editorial dimensions, per-case медианы, min, failures, P95 duration, calls и cost availability.

- [ ] Написать `test_failed_generations_remain_in_comparison`, `test_missing_cost_is_not_reported_as_zero`, `test_holdout_regression_prevents_rollout_recommendation`. Запустить соответствующий pytest, подтвердить FAIL, реализовать reporting, повторить до PASS.
- [ ] Сравнить baseline и combined на шести sealed inputs трижды: 36 генераций, максимум 108 provider calls, без автоматического повторения rejected до удачного текста. Сохранить model/parameters/hash/version для каждого результата.
- [ ] Лично прочитать каждый final post, поставить 0–4 по пяти параметрам и привести конкретные примеры недостатков. Проверить обязательный microdetail checklist каждого case. Не просить модель выставить себе балл.
- [ ] Проверить все acceptance thresholds spec §7.3: median ≥85, min ≥75, прирост median ≥5 с оговорённым случаем высокого baseline; holdout median ≥80 и не хуже baseline; нулевые coverage/safety regressions; не больше failures; latency/cost рост ≤20%, ≤2 editor calls.
- [ ] Зафиксировать `rollout_recommended` только при выполнении проверяемых критериев. При неизвестной стоимости отметить незакрытый критерий; при отрицательном результате изложить конкретный дефект и следующий ограниченный эксперимент, не включать flags.
- [ ] Коммит: `test(digest): document frozen editorial comparison and rollout evidence`.

## Задача 8. Серверный dry-run и контроль включения

**Files:** update rollout journal в `docs/editorial/digest-quality-evaluation.md`; production config меняется отдельно от tracked example и без раскрытия секретов.

- [ ] После положительного решения задачи 7 убедиться в clean diff, пройти relevant regression и применимые hooks после последних code changes. Закоммитить и push approved code обычным deployment путём; проверить фактическую версию app/worker.
- [ ] Выполнить на сервере `python scripts/preview_digest.py --edition berdyansk --hours 24 --as-of <ISO8601_WITH_OFFSET> --output <PREVIEW_PATH>` внутри действующего app runtime. Эта команда создаёт preview записи; она не readonly replay и не отправляет Telegram post. Сверить отключённую delivery, exact artifact, coverage и attempts.
- [ ] Прочитать готовый текст целиком: начало темы, переходы, география/время, attribution, детали, практическая польза. Автоматически проверить canonical budget/coverage и отсутствие fallback; stylistic flags не превращать в veto.
- [ ] Включить сначала `compact_v1`, затем `thematic_blocks`, только для согласованной edition через существующие настройки. Зафиксировать время, версии и предыдущие значения flags. Не запускать ручную доставку в рамках dry-run.
- [ ] По первым трём штатным выпускам проверить actual delivered post, generation duration, errors и соблюдение времени. В журнал внести и неудачные/пропущенные выпуски. Расписанные выпуски проверяются после их наступления; не объявлять наблюдение завершённым заранее.
- [ ] При регрессии вернуть flags в предыдущий проверенный AI mode; знания, план coverage и Evidence Boundary не ослаблять. Откат документировать с точной причиной.
- [ ] Документировать завершённые и ожидающие наблюдения. Коммит отчёта: `docs(digest): record deployment verification and editorial rollout`.

## Завершение и самопроверка плана

Каждая задача имеет собственные проверяемые результаты и отдельный коммит. До выполнения задач действующие production defaults сохраняются. Synthetic tests доказывают контракт; реальные готовые тексты подтверждают редакционный результат; observations после rollout проверяют операционную надёжность.

Покрытие спецификации: §1/5 → задачи 2, 3, 4, 7; §4 → regression задачи 1, 3, 5, 6; §6.1 → задача 3; §6.2 → задачи 5/6; §6.3 → задачи 4/6; §7 → задачи 1/2/7; §8 → задачи 3/6/8. Все пять Review Focus связаны с конкретными tests. Новые типы и flags объявлены до использования. Реализация не требует независимых подсистем, новых моделей или миграций.

План считается реализованным после code verification и положительного сравнения; стадия rollout observations отмечается отдельно и остаётся открытой до фактических штатных выпусков. Ни «все tests зелёные», ни один красивый dry-run не заменяют эту проверку.
