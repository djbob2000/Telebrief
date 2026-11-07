"""Second, offline check: max cosine to training DROP vs training KEEP fragments."""
import json
from pathlib import Path

import numpy as np

base = Path(".")
meta = json.loads((base / "knn_meta.json").read_text(encoding="utf-8"))
vecs = np.fromfile(base / "knn_vecs.f32", dtype=np.float32).reshape(len(meta), 1536)
vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
ts = np.array([m["ts"] for m in meta])
cut = np.quantile(ts, 0.7)
tr = np.where(ts < cut)[0]
te = np.where(ts >= cut)[0]
is_drop_tr = np.array([meta[i]["label"] == "DROP" for i in tr])
xdrop = vecs[tr][is_drop_tr]
xkeep = vecs[tr][~is_drop_tr]
best_drop = np.zeros(len(te), dtype=np.float32)
best_keep = np.zeros(len(te), dtype=np.float32)
for s in range(0, len(te), 1000):
    block = vecs[te[s : s + 1000]]
    best_drop[s : s + 1000] = (block @ xdrop.T).max(axis=1)
    best_keep[s : s + 1000] = (block @ xkeep.T).max(axis=1)

stories = {}
for j, i in enumerate(te):
    m = meta[i]
    st = stories.setdefault(m["story_id"], {"label": m["label"], "drop_sim": 0.0, "keep_sim": 0.0, "service": False})
    st["drop_sim"] = max(st["drop_sim"], float(best_drop[j]))
    st["keep_sim"] = max(st["keep_sim"], float(best_keep[j]))
    st["service"] = st["service"] or m["service"]
n = len(stories)
print("rule: pre-drop if max_sim_to_DROP >= t AND max_sim_to_DROP > max_sim_to_KEEP AND no service marker")
print("threshold  margin  pre_dropped  share  keep_lost")
for t in (0.85, 0.9, 0.93, 0.95, 0.97):
    for margin in (0.0, 0.02):
        pre = [s for s in stories.values() if s["drop_sim"] >= t and s["drop_sim"] > s["keep_sim"] + margin and not s["service"]]
        kl = sum(1 for s in pre if s["label"] == "KEEP")
        print(f"{t:<10}{margin:<8}{len(pre):>10}{len(pre) / n * 100:>7.1f}%{kl:>11}")

pre_ids = [sid for sid, s in stories.items() if s["drop_sim"] >= 0.95 and s["drop_sim"] > s["keep_sim"] and not s["service"] and s["label"] == "KEEP"]
print("KEEP story ids lost at 0.95:", pre_ids)
