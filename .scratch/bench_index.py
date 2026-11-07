import sys, time, datetime as dt, resource
sys.path.insert(0, ".")
import numpy as np
from src.processing.cluster_index import ClusterIndex
rng = np.random.default_rng(0)
N, D = 6660, 1536
BASE = dt.datetime(2026, 11, 7, tzinfo=dt.UTC)
rows = rng.standard_normal((N, D), dtype=np.float32)
idx = ClusterIndex(dimensions=D, capacity=N + 80)
t0 = time.perf_counter()
for i in range(N):
    idx.set_row(i + 1, rows[i], BASE - dt.timedelta(hours=float(i % 70)), 1)
print(f"load {N} rows: {time.perf_counter()-t0:.2f}s")
since = BASE - dt.timedelta(hours=72)
qs = rng.standard_normal((70, D)).astype(np.float32)
t0 = time.perf_counter()
for q in qs:
    idx.nearest(q.tolist(), active_since=since, limit=20)
per = (time.perf_counter() - t0) / len(qs)
print(f"nearest per fragment: {per*1000:.2f} ms  (batch of 70: {per*70:.2f}s)")
print(f"maxrss: {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.0f} MB")
