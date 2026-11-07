"""Pack fp4/fp8 article pairs into blind A/B files; the key is written to a separate folder."""

import json
import random
import shutil
from pathlib import Path

OUT = Path("/home/opc/gate_experiment/out")
BLIND = Path("/home/opc/gate_experiment/blind")
KEY = Path("/home/opc/gate_experiment/key")
RUNS = [346, 350, 342, 338]
SEED = 20261102

BLIND.mkdir(parents=True, exist_ok=True)
KEY.mkdir(parents=True, exist_ok=True)
rng = random.Random(SEED)
mapping = {}
for run in RUNS:
    fp4 = OUT / f"r{run}_fp4.md"
    fp8 = OUT / f"r{run}_fp8.md"
    if not (fp4.exists() and fp8.exists()):
        print(f"run {run}: missing text, skipped")
        continue
    pair = [("fp4", fp4), ("fp8", fp8)]
    rng.shuffle(pair)
    labels = {"A": pair[0][0], "B": pair[1][0]}
    for label, (variant, src) in zip(("A", "B"), pair):
        shutil.copy(src, BLIND / f"run{run}_{label}.md")
    mapping[f"run{run}"] = labels
    print(f"run {run}: packed")

(KEY / "key.json").write_text(json.dumps(mapping, indent=2))
print("blind pairs:", sorted(p.name for p in BLIND.iterdir()))
