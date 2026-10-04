# Проверка редакционного качества дайджеста

План: `docs/superpowers/plans/2026-10-04-readable-city-digest.md`.
Снимки synthetic-v1 созданы до настройки нового writer/editor; live source prose в Git не сохраняется.

Development: rich_utilities, quiet_day, local_contrasts, practical_services.
Holdout: partial_announcements, community_microdetails. Не использовать holdout для настройки prompts.

Критерии: 100% выбранных Stories/facts; honest single-source attribution; неизменные цитаты; лимит Telegram; no fallback. Editorial score: пять измерений 0–4 (hierarchy, cohesion, repetition, usefulness, detail/attribution), сумма ×5. Оценки выставляются по готовому тексту с примерами; отсутствующие source details не дописываются.

Synthetic cases используют один общий тестовый блок, чтобы сравнивать синтез независимо от Selection/rubric assignment. Этот набор не заменяет реальные multi-rubric снимки и не подтверждает production rollout сам по себе.

Baseline и новые варианты выполняются через offline replay, без базы и доставки. Отчёты/тексты: `data/previews/digest-evaluation/` (ignored). Отказы учитываются вместе с успешными результатами.

Статус: реализация и сравнение в работе. Rollout пока не принят.
