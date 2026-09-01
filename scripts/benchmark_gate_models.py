"""Comprehensive Gate benchmark: Minimax-M3:Free vs DeepSeek-V4-Flash.

Evaluates batch sizes (8, 16, 32, 64), technical latency/reliability,
Gate quality (false drops on short facts, false keeps on chatter/ads/questions),
and conversational context envelope impact.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.ai_providers import create_provider
from src.config_loader import load_config
from src.processing.event_triage import _GATE_V2_SYSTEM_PROMPT

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("benchmark_gate")


@dataclass(frozen=True)
class BenchmarkStory:
    id: int
    text: str
    category: str
    expected_scope: str  # LOCAL, OUT_OF_SCOPE, UNCERTAIN
    expected_retention: str  # KEEP, DROP
    notes: str = ""


# 64 curated representative stories across all key categories
BENCHMARK_STORIES: list[BenchmarkStory] = [
    # 1. Standard civic / infrastructure / emergency (12 stories)
    BenchmarkStory(1, "Бердянскводоканал: в связи с аварией на водоводе по ул. Горького отключена подача воды в центре до 18:00.", "civic_standard", "LOCAL", "KEEP"),
    BenchmarkStory(2, "В Бердянске на подстанции 35 кВ начались восстановительные работы, свет обещают к вечеру.", "civic_standard", "LOCAL", "KEEP"),
    BenchmarkStory(3, "МЧС сообщает о локализации возгорания сухой травы в районе АКЗ, угрозы жилым домам нет.", "civic_standard", "LOCAL", "KEEP"),
    BenchmarkStory(4, "Внимание! В районе косы обнаружен неразорвавшийся боеприпас. Территория оцеплена.", "civic_standard", "LOCAL", "KEEP"),
    BenchmarkStory(5, "С 1 сентября городские автобусы №4 и №17 переходят на осенний график движения.", "civic_standard", "LOCAL", "KEEP"),
    BenchmarkStory(6, "Городская больница Бердянска: прием узких специалистов временно перенесен в здание поликлиники на Коммунаров.", "civic_standard", "LOCAL", "KEEP"),
    BenchmarkStory(7, "Бердянское отделение ПСБ возобновило выдачу пенсий и социальных пособий наличными.", "civic_standard", "LOCAL", "KEEP"),
    BenchmarkStory(8, "В селе Осипенко Бердянского района завершен ремонт трансформатора, энергоснабжение восстановлено.", "civic_standard", "LOCAL", "KEEP"),
    BenchmarkStory(9, "В Бердянске на перекрестке Восточного и Пролетарского включили светофор после череды аварий.", "civic_standard", "LOCAL", "KEEP"),
    BenchmarkStory(10, "Газовая служба Бердянска информирует о плановой опрессовке сетей в микрорайоне Колония.", "civic_standard", "LOCAL", "KEEP"),
    BenchmarkStory(11, "Власти Запорожской области объявили о выплатах 10 000 рублей школьникам Бердянска к учебному году.", "civic_standard", "LOCAL", "KEEP"),
    BenchmarkStory(12, "Школы Бердянска с 1 сентября будут работать в дистанционном формате из соображений безопасности.", "civic_standard", "LOCAL", "KEEP"),

    # 2. Short genuine micro-facts (12 stories) - MUST SURVIVE (KEEP)
    BenchmarkStory(13, "Центр воду дали", "short_real_facts", "LOCAL", "KEEP"),
    BenchmarkStory(14, "Азмол — нет", "short_real_facts", "LOCAL", "KEEP", "Outage report: no power"),
    BenchmarkStory(15, "в нас дощ", "short_real_facts", "LOCAL", "KEEP", "Weather observation in city"),
    BenchmarkStory(16, "Слободка есть вода", "short_real_facts", "LOCAL", "KEEP"),
    BenchmarkStory(17, "На Пионерской электричества нет", "short_real_facts", "LOCAL", "KEEP"),
    BenchmarkStory(18, "В центре только в 9 утра связь воскресла 😁", "short_real_facts", "LOCAL", "KEEP"),
    BenchmarkStory(19, "На Гагарина 1 действует пункт зарядки телефонов (Бесплатно)", "short_real_facts", "LOCAL", "KEEP"),
    BenchmarkStory(20, "Маршрутка 4 ходит примерно раз в час", "short_real_facts", "LOCAL", "KEEP"),
    BenchmarkStory(21, "У порта медуз вчера не было, сегодня нанесло", "short_real_facts", "LOCAL", "KEEP"),
    BenchmarkStory(22, "На Шмидта третий день нет воды", "short_real_facts", "LOCAL", "KEEP"),
    BenchmarkStory(23, "На стадионе Энергия открыли бесплатный набор детей в секцию футбола", "short_real_facts", "LOCAL", "KEEP"),
    BenchmarkStory(24, "В районе Самолёта SMS-сообщения снова доходят", "short_real_facts", "LOCAL", "KEEP"),

    # 3. Pure chatter noise & uncertainty (10 stories) - MUST DROP
    BenchmarkStory(25, "Не знаю.", "chatter_noise", "LOCAL", "DROP"),
    BenchmarkStory(26, "хз, сам в шоке", "chatter_noise", "LOCAL", "DROP"),
    BenchmarkStory(27, "Спасибо большое за ответ!", "chatter_noise", "LOCAL", "DROP"),
    BenchmarkStory(28, "Доброе утро всем бердянцам ☕️", "chatter_noise", "LOCAL", "DROP"),
    BenchmarkStory(29, "Ясно, понятно", "chatter_noise", "LOCAL", "DROP"),
    BenchmarkStory(30, "лол 😂", "chatter_noise", "LOCAL", "DROP"),
    BenchmarkStory(31, "Плюсую, согласен на 100%", "chatter_noise", "LOCAL", "DROP"),
    BenchmarkStory(32, "Без понятия, спроси в группе", "chatter_noise", "LOCAL", "DROP"),
    BenchmarkStory(33, "Кто его знает, поживем увидим", "chatter_noise", "LOCAL", "DROP"),
    BenchmarkStory(34, "Сам понял что написал?", "chatter_noise", "LOCAL", "DROP"),

    # 4. Classifieds, sales & directory payload (10 stories) - MUST DROP
    BenchmarkStory(35, "Продам айфон 16 про Макс 35к, писать в лс", "classifieds_ads", "LOCAL", "DROP"),
    BenchmarkStory(36, "‼️Сниму 3-х комнатную квартиру или частный дом на длительный срок. Варианты в ЛС", "classifieds_ads", "LOCAL", "DROP"),
    BenchmarkStory(37, "Питьевая вода на розлив — 3 ₽/литр. Вниманию предпринимателей, доставка от 1 тонны. +79900235890", "classifieds_ads", "LOCAL", "DROP"),
    BenchmarkStory(38, "💅 ИЩЕМ МОДЕЛЕК НА МАНИКЮР. Оплата 1500 руб, покрытие гель-лак. Запись в лс", "classifieds_ads", "LOCAL", "DROP"),
    BenchmarkStory(39, "Пассажирские перевозки Бердянск - Ростов - Москва. Через КПП Мокраны. Без предоплат. +79781948808", "classifieds_ads", "LOCAL", "DROP"),
    BenchmarkStory(40, "🔥 ПОКУПАЕМ МЕТАЛЛОЛОМ 🔥 Черные и цветные металлы, самовывоз, высокие цены. Звоните прямо сейчас", "classifieds_ads", "LOCAL", "DROP"),
    BenchmarkStory(41, "🐶 В продаже щенки Мальтипу F1, привиты по возрасту, ветпаспорт, чип. Цена в личку", "classifieds_ads", "LOCAL", "DROP"),
    BenchmarkStory(42, "Магазин «Ресурс» на Коммунаров, 49 — стройматериалы по хорошим ценам, в наличии и под заказ", "classifieds_ads", "LOCAL", "DROP"),
    BenchmarkStory(43, "Купим дом. Район Самолета и близлежащие. Предложения в лс. +79900256141", "classifieds_ads", "LOCAL", "DROP"),
    BenchmarkStory(44, "Заправлю ваш газовый баллон б/у, недорого. Обращаться в лс", "classifieds_ads", "LOCAL", "DROP"),

    # 5. Pure questions without answers (10 stories) - MUST DROP or CONTEXT (not PUBLISH)
    BenchmarkStory(45, "А возле Грации?", "pure_questions", "LOCAL", "DROP"),
    BenchmarkStory(46, "А интернет как?", "pure_questions", "LOCAL", "DROP"),
    BenchmarkStory(47, "до скольки работает кож-вен?", "pure_questions", "LOCAL", "DROP"),
    BenchmarkStory(48, "Кто знает, где можно перевести документы с нотариальным заверением?", "pure_questions", "LOCAL", "DROP"),
    BenchmarkStory(49, "Что со светом есть какие то новости? Хоть какие-то ?", "pure_questions", "LOCAL", "DROP"),
    BenchmarkStory(50, "А дроны чьи? Или кто то видел откуда они взлетают, что бы утверждать?", "pure_questions", "LOCAL", "DROP"),
    BenchmarkStory(51, "Выросли цены?", "pure_questions", "LOCAL", "DROP"),
    BenchmarkStory(52, "Куйбышево было название раньше?", "pure_questions", "LOCAL", "DROP"),
    BenchmarkStory(53, "Подскажите где купить детский электросамокат", "pure_questions", "LOCAL", "DROP"),
    BenchmarkStory(54, "Вам сообщение в личку не отсылается?", "pure_questions", "LOCAL", "DROP"),

    # 6. Distant / Out of Scope (10 stories) - MUST OUT_OF_SCOPE / DROP
    BenchmarkStory(55, "В Киеве прогремели взрывы в районе правого берега, работает ПВО.", "out_of_scope", "OUT_OF_SCOPE", "DROP"),
    BenchmarkStory(56, "Мэр Одессы сообщил о последствиях ракетного удара по портовой инфраструктуре.", "out_of_scope", "OUT_OF_SCOPE", "DROP"),
    BenchmarkStory(57, "В Харькове вводятся аварийные графики отключения света на 4 часа.", "out_of_scope", "OUT_OF_SCOPE", "DROP"),
    BenchmarkStory(58, "ДТП с тремя автомобилями произошло на трассе Симферополь - Севастополь.", "out_of_scope", "OUT_OF_SCOPE", "DROP"),
    BenchmarkStory(59, "В Кривом Роге спасатели ликвидировали пожар на промышленном объекте.", "out_of_scope", "OUT_OF_SCOPE", "DROP"),
    BenchmarkStory(60, "На Купянском направлении продолжаются позиционные бои.", "out_of_scope", "OUT_OF_SCOPE", "DROP"),
    BenchmarkStory(61, "В Москве открылась новая станция метро Большой кольцевой линии.", "out_of_scope", "OUT_OF_SCOPE", "DROP"),
    BenchmarkStory(62, "Губернатор Краснодарского края сообщил об отражении атаки БПЛА.", "out_of_scope", "OUT_OF_SCOPE", "DROP"),
    BenchmarkStory(63, "В Запорожье частично перекрыто движение по плотине Днепрогэса.", "out_of_scope", "OUT_OF_SCOPE", "DROP"),
    BenchmarkStory(64, "Погода в Донецке: завтра ожидается гроза и шквалистый ветер.", "out_of_scope", "OUT_OF_SCOPE", "DROP"),
]

# 20 Conversation-dependent QA pairs
CONVERSATION_PAIRS: list[dict[str, str]] = [
    {"q": "На Азмоле есть свет?", "a": "Нет", "topic": "power_outage"},
    {"q": "Когда воду дали?", "a": "В девять", "topic": "water_supply"},
    {"q": "А интернет как?", "a": "Миранда нормально", "topic": "telecom"},
    {"q": "Маршрутка 17 ходит?", "a": "Да, каждые 20 минут", "topic": "transport"},
    {"q": "В центре стреляют?", "a": "Тихо пока", "topic": "safety"},
    {"q": "Воду подвозят?", "a": "Только к школе", "topic": "water_distribution"},
    {"q": "Банкомат ПСБ работает?", "a": "Наличка есть", "topic": "banking"},
    {"q": "Свет включили?", "a": "Только что дали", "topic": "power_restore"},
    {"q": "На Горе связь ловит?", "a": "Только у окна", "topic": "telecom_coverage"},
    {"q": "Аптека на Ленина открыта?", "a": "До пяти", "topic": "pharmacy"},
    {"q": "На Косу проехать можно?", "a": "Дорога открыта", "topic": "road_access"},
    {"q": "Больница принимает?", "a": "Только дежурный врач", "topic": "hospital"},
    {"q": "Связь Киевстар есть?", "a": "Вообще нет", "topic": "mobile_network"},
    {"q": "Газ не отключали?", "a": "Есть газ", "topic": "gas_supply"},
    {"q": "Школы с 1 сентября очно?", "a": "Дистанционка", "topic": "education"},
    {"q": "Генератор сильно шумит?", "a": "Гул на весь двор", "topic": "community_noise"},
    {"q": "Очередь в пенсионный большая?", "a": "Человек 40 с утра", "topic": "social_services"},
    {"q": "Паспортный стол принимает?", "a": "По талонам", "topic": "civil_registry"},
    {"q": "Бензин на заправке есть?", "a": "95-й по 65", "topic": "fuel_price"},
    {"q": "Светофор на Пролетарском починили?", "a": "Мигает желтым", "topic": "traffic_light"},
]


def build_gate_prompt(stories: list[tuple[int, str]]) -> str:
    lines = [
        "GEOGRAPHIC FOCUS CONTRACT FOR BERDYANSK:",
        "- Edition Focus Area: Berdyansk and immediate settlements of Berdyansk district (Osypenko, Novovasylivka, Azovske, Dmytrivka, Troyany, Andrivka, Berestove).",
        "- Events outside this focus area are OUT_OF_SCOPE unless direct concrete consequence in Berdyansk is explicitly stated.",
        "",
        "Stories for Gate V2 triage and brief synthesis:",
    ]
    for sid, text in stories:
        lines.append(f"Story #{sid} (fragments=1, sources=1):\n- [frag={sid} time=2026-09-01T12:00:00 source=telegram] {text}\n")
    return "\n".join(lines)


async def execute_batch(
    provider: Any,
    model: str,
    stories: list[tuple[int, str]],
) -> tuple[dict[int, dict[str, Any]], float, int, str | None]:
    prompt = build_gate_prompt(stories)
    t0 = time.perf_counter()
    err_str = None
    results_map: dict[int, dict[str, Any]] = {}
    tok_usage = 0

    try:
        raw_res = await provider.chat_completion(
            messages=[
                {"role": "system", "content": _GATE_V2_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            model=model,
            temperature=0.0,
            max_tokens=65536,
            response_format={"type": "json_object"},
        )
        latency = time.perf_counter() - t0

        # Parse JSON
        parsed = json.loads(raw_res)
        items = parsed.get("results") or []
        for it in items:
            sid = it.get("story_id")
            if sid is not None:
                results_map[int(sid)] = it
    except Exception as exc:
        latency = time.perf_counter() - t0
        err_str = f"{type(exc).__name__}: {exc}"
        logger.warning("Batch execution failed for %s (%d items): %s", model, len(stories), err_str)

    return results_map, latency, tok_usage, err_str


async def run_benchmark_matrix():
    cfg = load_config()
    provider = create_provider(
        "openrouter",
        logger=logger,
        openrouter_api_key=cfg.openrouter_api_key,
        openrouter_base_url="https://openrouter.ai/api/v1",
    )

    import os
    target_model = os.environ.get("OPENROUTER_MODEL") or getattr(cfg.settings, "ai_model", None) or "deepseek/deepseek-v4-flash-0731:floor"
    logger.info("Using target model from .env/config: %s", target_model)

    models_and_batches = [
        ("Batch 8", target_model, 8),
        ("Batch 16", target_model, 16),
        ("Batch 32", target_model, 32),
        ("Batch 64", target_model, 64),
    ]

    all_runs_metrics: dict[str, Any] = {}
    all_raw_results: dict[str, dict[int, Any]] = {}

    story_tuples = [(s.id, s.text) for s in BENCHMARK_STORIES]
    truth_by_id = {s.id: s for s in BENCHMARK_STORIES}

    print("\n" + "=" * 80)
    print("🚀 STARTING GATE BENCHMARK: 64 STORIES MATRIX")
    print("=" * 80)

    for run_name, model_name, batch_size in models_and_batches:
        print(f"\n▶ Running {run_name}: model={model_name}, batch_size={batch_size}...")
        total_lat = 0.0
        batch_count = 0
        timeouts = 0
        json_errors = 0
        model_results: dict[int, dict[str, Any]] = {}

        # Chunk into batches
        chunks = [story_tuples[i : i + batch_size] for i in range(0, len(story_tuples), batch_size)]

        for idx, chunk in enumerate(chunks, 1):
            logger.info("  Processing batch %d/%d (%d stories)...", idx, len(chunks), len(chunk))
            res_map, lat, _, err = await execute_batch(provider, model_name, chunk)
            total_lat += lat
            batch_count += 1

            if err:
                timeouts += 1
            elif not res_map:
                json_errors += 1
            else:
                model_results.update(res_map)

            # Small cooldown for free model
            if "free" in model_name:
                await asyncio.sleep(2.0)

        all_raw_results[run_name] = model_results

        # Evaluate quality metrics against ground truth
        evaluated_count = len(model_results)
        correct_retention = 0
        correct_scope = 0

        # Sub-category metrics
        false_drop_short_facts = []
        false_keep_chatter = []
        false_keep_ads = []
        false_keep_questions = []

        for sid, truth in truth_by_id.items():
            pred = model_results.get(sid)
            if not pred:
                continue

            pred_scope = str(pred.get("scope", "")).upper()
            pred_ret = str(pred.get("retention", "")).upper()

            if pred_scope == truth.expected_scope:
                correct_scope += 1
            if pred_ret == truth.expected_retention:
                correct_retention += 1

            # Category tracking
            if truth.category == "short_real_facts":
                if pred_ret != "KEEP":
                    false_drop_short_facts.append(truth.text)

            elif truth.category == "chatter_noise":
                if pred_ret == "KEEP":
                    false_keep_chatter.append(truth.text)

            elif truth.category == "classifieds_ads":
                if pred_ret == "KEEP":
                    false_keep_ads.append(truth.text)

            elif truth.category == "pure_questions":
                pub = (pred.get("brief_payload") or {}).get("publishability")
                if pred_ret == "KEEP" and pub in ("news", "brief"):
                    false_keep_questions.append(truth.text)

        metrics = {
            "model": model_name,
            "batch_size": batch_size,
            "total_latency_sec": round(total_lat, 2),
            "avg_batch_latency_sec": round(total_lat / max(1, batch_count), 2),
            "stories_evaluated": evaluated_count,
            "total_stories": len(BENCHMARK_STORIES),
            "timeouts": timeouts,
            "json_errors": json_errors,
            "retention_accuracy": f"{correct_retention}/{evaluated_count} ({round(correct_retention / max(1, evaluated_count) * 100, 1)}%)",
            "scope_accuracy": f"{correct_scope}/{evaluated_count} ({round(correct_scope / max(1, evaluated_count) * 100, 1)}%)",
            "false_drops_short_facts_count": len(false_drop_short_facts),
            "false_drops_short_facts": false_drop_short_facts,
            "false_keeps_chatter_count": len(false_keep_chatter),
            "false_keeps_chatter": false_keep_chatter,
            "false_keeps_ads_count": len(false_keep_ads),
            "false_keeps_ads": false_keep_ads,
            "false_keeps_questions_count": len(false_keep_questions),
            "false_keeps_questions": false_keep_questions,
        }
        all_runs_metrics[run_name] = metrics
        print(f"  ✓ Finished {run_name}: Latency={metrics['total_latency_sec']}s, Accuracy={metrics['retention_accuracy']}, FalseDrops={metrics['false_drops_short_facts_count']}, FalseKeeps(Ads={metrics['false_keeps_ads_count']}, Chatter={metrics['false_keeps_chatter_count']})")

    # =========================================================================
    # PART 2: CONTEXTUAL ENVELOPE EXPERIMENT (20 PAIRS)
    # =========================================================================
    print("\n" + "=" * 80)
    print("🔬 CONVERSATION ENVELOPE EXPERIMENT (Mode 1: Isolated vs Mode 2: Contextual)")
    print("=" * 80)

    deepseek_model = target_model

    # Mode 1: Isolated Answers
    mode1_tuples = [(100 + i, pair["a"]) for i, pair in enumerate(CONVERSATION_PAIRS)]
    # Mode 2: Contextual Envelope
    mode2_tuples = [
        (200 + i, f"[REPLY_TO: «{pair['q']}» (topic={pair['topic']})]\nTARGET: «{pair['a']}»")
        for i, pair in enumerate(CONVERSATION_PAIRS)
    ]

    print("▶ Evaluating Mode 1 (Isolated replies, e.g. 'Нет', 'В девять')...")
    res_m1, lat_m1, _, err_m1 = await execute_batch(provider, deepseek_model, mode1_tuples)

    print("▶ Evaluating Mode 2 (Contextual envelope, [REPLY_TO: Q] TARGET: A)...")
    res_m2, lat_m2, _, err_m2 = await execute_batch(provider, deepseek_model, mode2_tuples)

    context_comparisons = []
    flipped_to_useful = 0

    for i, pair in enumerate(CONVERSATION_PAIRS):
        m1 = res_m1.get(100 + i, {})
        m2 = res_m2.get(200 + i, {})

        m1_ret = m1.get("retention", "ERR")
        m1_scope = m1.get("scope", "ERR")
        m1_topic = (m1.get("brief_payload") or {}).get("topic", "")

        m2_ret = m2.get("retention", "ERR")
        m2_scope = m2.get("scope", "ERR")
        m2_topic = (m2.get("brief_payload") or {}).get("topic", "")

        gained_civic_value = (m1_ret != "KEEP" and m2_ret == "KEEP") or (not m1_topic and bool(m2_topic))
        if gained_civic_value:
            flipped_to_useful += 1

        context_comparisons.append(
            {
                "question": pair["q"],
                "answer": pair["a"],
                "mode1_isolated": f"scope={m1_scope}, ret={m1_ret}, topic={m1_topic!r}",
                "mode2_contextual": f"scope={m2_scope}, ret={m2_ret}, topic={m2_topic!r}",
                "semantic_gain": gained_civic_value,
            }
        )

    envelope_summary = {
        "pairs_tested": len(CONVERSATION_PAIRS),
        "flipped_to_useful_count": flipped_to_useful,
        "flipped_percentage": f"{round(flipped_to_useful / len(CONVERSATION_PAIRS) * 100, 1)}%",
        "mode1_latency_sec": round(lat_m1, 2),
        "mode2_latency_sec": round(lat_m2, 2),
        "details": context_comparisons,
    }

    # Save artifact
    output_dir = Path("/Users/air/.gemini/antigravity-ide/brain/a331c9d6-a80c-4911-ae2b-d379e7ab4cf4")
    artifact_path = output_dir / "gate_benchmark_results.json"
    full_report = {
        "benchmark_matrix": all_runs_metrics,
        "conversation_envelope_experiment": envelope_summary,
        "raw_results": all_raw_results,
    }
    with open(artifact_path, "w", encoding="utf-8") as f:
        json.dump(full_report, f, ensure_ascii=False, indent=2)

    logger.info("Saved full benchmark results to %s", artifact_path)
    print("\n✅ BENCHMARK COMPLETE! Results saved to", artifact_path)


if __name__ == "__main__":
    asyncio.run(run_benchmark_matrix())
