"""Local, offline kNN check on exported Gate-labelled embeddings. No LLM, no DB."""

import json
import sys
from pathlib import Path

import numpy as np

K = 15
THRESHOLDS = (0.95, 0.97, 0.98, 0.99, 0.995)
base = Path(sys.argv[1])
meta = json.loads((base / "knn_meta.json").read_text(encoding="utf-8"))
dims = 1536
vecs = np.fromfile(base / "knn_vecs.f32", dtype=np.float32).reshape(len(meta), dims)
vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
ts = np.array([m["ts"] for m in meta])
print(f"rows={len(meta)}")

# Time split: last 2 days are test; the earlier part is train.
cut = np.quantile(ts, 0.7)
tr = np.where(ts < cut)[0]
te = np.where(ts >= cut)[0]
ytr = np.array([1.0 if meta[i]["label"] == "DROP" else 0.0 for i in tr], dtype=np.float32)
xtr = vecs[tr]
p_drop = np.zeros(len(te), dtype=np.float32)
for s in range(0, len(te), 1000):
    block = vecs[te[s : s + 1000]]
    sims = block @ xtr.T
    idx = np.argpartition(-sims, K, axis=1)[:, :K]
    w = np.clip(np.take_along_axis(sims, idx, axis=1), 0, None)
    p_drop[s : s + 1000] = (w * ytr[idx]).sum(axis=1) / (w.sum(axis=1) + 1e-9)

stories = {}
for j, i in enumerate(te):
    m = meta[i]
    st = stories.setdefault(m["story_id"], {"label": m["label"], "min_p": 1.0, "service": False})
    st["min_p"] = min(st["min_p"], float(p_drop[j]))
    st["service"] = st["service"] or m["service"]

n = len(stories)
keep = sum(1 for s in stories.values() if s["label"] == "KEEP")
drop = n - keep
svc = sum(1 for s in stories.values() if s["label"] == "DROP" and s["service"])
print(f"train_rows={len(tr)} test_rows={len(te)} test_stories={n} keep={keep} drop={drop} drop_with_service={svc}")
print("threshold  pre_dropped  share  keep_lost  service_drop_lost")
for t in THRESHOLDS:
    pre = [s for s in stories.values() if s["min_p"] >= t]
    kl = sum(1 for s in pre if s["label"] == "KEEP")
    sl = sum(1 for s in pre if s["label"] == "DROP" and s["service"])
    print(f"{t:<10}{len(pre):>12}{len(pre) / n * 100:>7.1f}%{kl:>11}{sl:>18}")

print("\nWITH GUARD (no service marker may be pre-dropped)")
print("threshold  pre_dropped  share  keep_lost  drop_kept")
for t in THRESHOLDS:
    pre = [s for s in stories.values() if s["min_p"] >= t and not s["service"]]
    kl = sum(1 for s in pre if s["label"] == "KEEP")
    print(f"{t:<10}{len(pre):>12}{len(pre) / n * 100:>7.1f}%{kl:>11}{'':>9}")
