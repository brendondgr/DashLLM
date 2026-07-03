"""Seed synthetic telemetry so the dashboard has a 30-day history to render
during development (mirrors the prototype's genHistory() traffic shape).

Usage (from repo root):
    uv run --project web/backend python utils/seed_telemetry.py \
        [--days 30] [--db web/backend/data/relay.db] [--wipe]

Rows are marked with client_key '…seed' so they are distinguishable from
real traffic and easy to purge:
    DELETE FROM requests WHERE client_key = '…seed';
"""

import argparse
import random
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web" / "backend"))

from app.db import Database  # noqa: E402

MODELS = ["gemma-4-26B-it", "default-model"]


def endpoint_pool(db: Database) -> list[tuple[str, str]]:
    rows = db.query("SELECT id, name FROM endpoints")
    if rows:
        return [(r["id"], r["name"]) for r in rows]
    return [("seed-ep-1", "llama.cpp · local"), ("seed-ep-2", "vLLM · local")]


def hourly_request_count(ts: float) -> int:
    lt = time.localtime(ts)
    base = 2 if lt.tm_hour < 7 else 14 if lt.tm_hour < 10 \
        else 34 if lt.tm_hour < 19 else 24
    if lt.tm_wday >= 5:  # weekend dip
        base = int(base * 0.55)
    burst = 60 if random.random() < 0.05 else 0
    return max(0, round(base * (0.5 + random.random()) + burst))


def seed(db: Database, days: int) -> int:
    eps = endpoint_pool(db)
    now = time.time()
    rows: list[tuple] = []
    for hour_back in range(days * 24, 0, -1):
        bucket_start = now - hour_back * 3600
        for _ in range(hourly_request_count(bucket_start)):
            ts = bucket_start + random.random() * 3600
            if ts > now:
                continue
            ep_id, ep_name = random.choice(eps)
            model = random.choice(MODELS)
            tin = 200 + round(random.random() * 3800)
            tout = 80 + round(random.random() * 1900)
            tps = 25 + random.random() * 60
            ttft = 40 + random.random() * 400
            latency = ttft + tout / tps * 1000
            err = random.random() < 0.015
            rows.append((
                "req_seed_" + uuid.uuid4().hex[:10], ts, ep_id, ep_name,
                "chat.completions", model, "…seed", 1,
                500 if err else 200, 0 if err else 1,
                "upstream error (seeded)" if err else None,
                tin, 0 if err else tout, tin + (0 if err else tout),
                round(ttft, 1), round(latency, 1),
                0 if err else round(tps, 2), 0.0,
                round(random.random() * 1.2, 1),
                random.choice([512, 1024, 2048, 4096]),
            ))
    db.executemany(
        "INSERT OR REPLACE INTO requests (id, ts, endpoint_id, endpoint_name,"
        " route, model, client_key, stream, status, ok, error, prompt_tokens,"
        " completion_tokens, total_tokens, ttft_ms, latency_ms,"
        " tokens_per_sec, cost_usd, temperature, max_tokens)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument(
        "--db", default=str(REPO_ROOT / "web/backend/data/relay.db"))
    parser.add_argument("--wipe", action="store_true",
                        help="remove previously seeded rows first")
    args = parser.parse_args()

    db = Database(args.db)
    if args.wipe:
        removed = db.execute(
            "DELETE FROM requests WHERE client_key = '…seed'")
        print(f"removed {removed} previously seeded rows")
    n = seed(db, args.days)
    total = db.query_one("SELECT COUNT(*) AS n FROM requests")["n"]
    print(f"seeded {n} rows over {args.days} days into {args.db}"
          f" (table now has {total})")
    db.close()


if __name__ == "__main__":
    main()
