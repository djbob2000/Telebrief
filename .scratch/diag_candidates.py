import asyncio, datetime as dt, os, random, time, math, tracemalloc
import psycopg
from src.repositories.event_clusters import EventClusterRepository
from src.processing.event_clustering import CandidateCentroidCache

def proc():
    out = {}
    for line in open('/proc/self/status'):
        k, _, v = line.partition(':')
        if k in ('VmRSS', 'VmHWM', 'VmSwap'):
            out[k] = v.strip()
    return out

async def main():
    async with await psycopg.AsyncConnection.connect(os.environ['DATABASE_URL']) as conn:
        cur = await conn.execute("SELECT model, dimensions, count(*) FROM story_cluster_state GROUP BY 1,2")
        print("models", await cur.fetchall(), flush=True)
        model, dims = "qwen/qwen3-embedding-8b:floor", 1536
        repo = EventClusterRepository()
        since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=72)
        t = time.monotonic()
        rows = await repo.list_candidate_metadata(conn, 1, model=model, dimensions=dims, active_since=since)
        print(f"metadata rows={len(rows)} t={time.monotonic()-t:.2f}s {proc()}", flush=True)
        ids = [r.story_id for r in rows]
        tracemalloc.start()
        t = time.monotonic()
        loaded = await repo.load_centroids(conn, ids)
        cur_mem, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(f"load_centroids loaded={len(loaded)} t={time.monotonic()-t:.2f}s py_peak={peak/2**20:.1f}MB py_now={cur_mem/2**20:.1f}MB {proc()}", flush=True)
        del loaded
        rng = random.Random(1)
        raw = [rng.gauss(0, 1) for _ in range(dims)]
        n = math.sqrt(sum(x * x for x in raw)); q = [x / n for x in raw]
        cache = CandidateCentroidCache(repo)
        for label in ("cold", "warm"):
            t = time.monotonic()
            found = await cache.find_candidates(conn, 1, q, model=model, dimensions=dims, active_since=since, limit=20)
            print(f"find_candidates {label} n={len(found)} t={time.monotonic()-t:.2f}s {proc()}", flush=True)

asyncio.run(main())
