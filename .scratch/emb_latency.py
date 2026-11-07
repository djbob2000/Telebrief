import os, time, json, urllib.request
key = os.environ["OPENROUTER_API_KEY"]
texts = [f"Тестовое сообщение номер {i} о подаче воды в Бердянске, улица Нагорная." for i in range(128)]
def call(model):
    body = json.dumps({"model": model, "input": texts, "dimensions": 1536}).encode()
    req = urllib.request.Request("https://openrouter.ai/api/v1/embeddings", data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    t0 = time.monotonic()
    try:
        r = json.load(urllib.request.urlopen(req, timeout=300))
        n = len(r.get("data", []))
        return f"{time.monotonic()-t0:.1f}s vectors={n}"
    except Exception as e:
        return f"{time.monotonic()-t0:.1f}s ERR {type(e).__name__} {getattr(e,'code','')}"
for m in ["qwen/qwen3-embedding-8b:floor", "qwen/qwen3-embedding-8b", "qwen/qwen3-embedding-8b:floor"]:
    print(m, call(m), flush=True)
