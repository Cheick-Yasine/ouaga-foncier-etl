from __future__ import annotations
import argparse, asyncio, json, os, sys
from datetime import datetime
from pathlib import Path
from typing import Any
import psycopg
from dotenv import load_dotenv
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
import config
import processor

def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))

def load_missing_posts(start: datetime, end: datetime) -> list[dict[str, Any]]:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn: raise ValueError("DATABASE_URL absente.")
    sql = ("SELECT r.post_id, r.payload FROM public.raw_posts_checkpoint r "
           "LEFT JOIN public.annonces a ON a.id = r.post_id "
           "WHERE r.captured_at >= %s AND r.captured_at <= %s AND a.id IS NULL "
           "ORDER BY r.captured_at, r.post_id")
    with psycopg.connect(dsn, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (start, end)); rows = cur.fetchall()
    posts = []
    for post_id, payload in rows:
        if isinstance(payload, dict):
            payload = dict(payload); payload.setdefault("id", str(post_id)); posts.append(payload)
    return posts

def mark_processed(post_ids: list[str]) -> int:
    if not post_ids: return 0
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn: raise ValueError("DATABASE_URL absente.")
    with psycopg.connect(dsn, autocommit=True, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE public.raw_posts_checkpoint SET processed=TRUE WHERE post_id=ANY(%s)", (post_ids,))
            return cur.rowcount

async def process_batches(posts: list[dict[str, Any]], batch_size: int) -> dict[str, int]:
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    totals = {"raw_selectionnes": len(posts), "candidats": 0, "valides": 0, "integres": 0, "rejetes_ou_echecs": 0, "checkpoints_marques": 0, "lots": 0}
    for offset in range(0, len(posts), batch_size):
        batch = posts[offset:offset + batch_size]; totals["lots"] += 1
        chemin = config.RAW_DIR / f"recovery_missing_batch_{totals['lots']:04d}.json"
        chemin.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"=== Lot {totals['lots']} : {offset+1}-{offset+len(batch)} / {len(posts)} ===")
        resultat = await processor.executer_traitement([chemin], mode="recovery_missing")
        totals["candidats"] += resultat.nb_candidats; totals["valides"] += resultat.nb_valides
        totals["integres"] += resultat.nb_valides
        totals["rejetes_ou_echecs"] += max(0, resultat.nb_candidats-resultat.nb_valides)
        ids = [str(p["id"]).strip() for p in batch if p.get("id")]
        totals["checkpoints_marques"] += mark_processed(ids)
    return totals

async def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--start", required=True); parser.add_argument("--end", required=True); parser.add_argument("--batch-size", type=int, default=250)
    args = parser.parse_args(); start, end = parse_ts(args.start), parse_ts(args.end)
    if end <= start: raise ValueError("--end doit être après --start")
    posts = load_missing_posts(start, end); print(f"RAW absents de public.annonces : {len(posts)}")
    if not posts: return 0
    totals = await process_batches(posts, args.batch_size)
    print("=== RATTRAPAGE TERMINE ==="); [print(f"{k}: {v}") for k,v in totals.items()]
    return 0

if __name__ == "__main__": raise SystemExit(asyncio.run(main()))