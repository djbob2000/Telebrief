"""Gate prompt rules that keep a fact's duration and hedges as the source states them.

Article-fidelity W7.1 (ordinal durations) and W7.3 (hedges and hearsay). Prompt text
only; the rules add no model call and no deterministic gate. The examples are generic
on purpose: edition-specific places belong to the city profile, not to prompt code.
"""

from __future__ import annotations

GATE_ORDINAL_AND_HEDGE_RULES = """ORDINAL DURATIONS AND HEDGES:
- Ordinal forms count a day or week already in progress, not a total. "4 неделю" ("4-ю неделю"), "третьи сутки" and "11 день" say that the fourth week, the third day or the eleventh day is under way. Write the ordinal in the fact, for example «идёт четвёртая неделя» or «идут третьи сутки». Never turn an ordinal into a cardinal total such as «уже четыре недели» or «четыре недели подряд».
- Example — source: "Тут нет света 4 неделю". The fact must say that the fourth week without power is under way, not that power has been absent for four weeks.
- Hedges and hearsay belong to the claim. Keep "похоже", "кажется", "вроде", "видимо", "говорят", "по слухам" and attributions such as "со слов соседей" or "по словам жителя" in the fact text and the headline. Never drop them and never upgrade them into an established status.
- Example — source: "Кажется, всё уже отключили". The fact must keep the hedge, for example «по сообщению, похоже, всё уже отключили», not «всё отключено».
"""
