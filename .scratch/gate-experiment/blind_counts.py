"""Automatic counters per blind file, without revealing the fp4/fp8 key (scratch)."""
import glob
import re

GENERIC = [
    "ситуация сложная", "жители адаптируются", "наблюдаются трудности",
    "ищут альтернативные решения", "город живёт", "ситуация остаётся",
]
for path in sorted(glob.glob("/home/opc/gate_experiment/blind/run34[26]_*.md")) + sorted(glob.glob("/home/opc/gate_experiment/blind/run350_*.md")):
    text = open(path, encoding="utf-8").read()
    low = text.lower()
    print(
        path.split("/")[-1],
        "words=", len(text.split()),
        "quotes=", text.count("«"),
        "digits=", len(re.findall(r"\d", text)),
        "generic=", sum(low.count(g) for g in GENERIC),
        "headings=", len(re.findall(r"^## ", text, flags=re.M)),
    )
