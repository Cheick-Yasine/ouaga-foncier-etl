"""Traite progressivement les RAW Neon déjà capturés par le backfill curseur.

Le but est d'alimenter public.annonces sans attendre la fin complète du backfill,
tout en gardant une reprise sûre :
- lit uniquement raw_posts_checkpoint.processed = FALSE ;
- peut se limiter à un groupe et une fenêtre de dates ;
- saute sans coût LLM les IDs déjà présents dans annonces ;
- traite les RAW par petits lots via le pipeline existant regex -> OpenAI -> Neon ;
- marque un lot processed=TRUE seulement si le pipeline du lot se termine avec succès.

Les nouveaux RAW capturés pendant l'exécution restent disponibles pour les lots
suivants ou pour une relance ultérieure.

Exemple :
    python scripts/process_pending_raw_batches.py \
      --group-id 364054065107714 \
      --start-date 2026-08-31 \
      --end-date 2026-09-13 \
      --batch-size 500
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

import config
import processor


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Date invalide '{value}'. Format attendu : YYYY-MM-DD."
        ) from exc


def _parse_publication(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.isdigit() and len(text) == 10:
            return datetime.fromtimestamp(int(text), tz=timezone.utc)
        if text.isdigit() and len(text) == 13:
            return datetime.fromtimestamp(int(text) / 1000.0, tz=timezone.utc)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _in_window(
    payload: dict[str, Any],
    start_dt: datetime | None,
    end_exclusive: datetime | None,
) -> bool:
    if start_dt is None and end_exclusive is None:
        return True
    published = _parse_publication(payload.get("date_publication"))
    if published is None:
        return False
    if start_dt is not None and published < start_dt:
        return False
    if end_exclusive is not None and published >= end_exclusive:
        return False
    return True


def _fetch_pending(
    conn: psycopg.Connection,
    *,
    group_id: str | None,
    start_dt: datetime | None,
    end_exclusive: datetime | None,
    scan_limit: int,
    batch_size: int,
) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    """Retourne (à traiter, déjà présents en annonces à marquer processed)."""
    conditions = ["r.processed = FALSE"]
    params: list[Any] = []

    if group_id:
        conditions.append("r.groupe_id = %s")
        params.append(group_id)

    sql = f"""
        SELECT r.post_id, r.payload,
               EXISTS (
                   SELECT 1 FROM annonces a WHERE a.id = r.post_id
               ) AS already_in_annonces
        FROM raw_posts_checkpoint r
        WHERE {' AND '.join(conditions)}
        ORDER BY r.captured_at, r.post_id
        LIMIT %s
    """
    params.append(scan_limit)

    rows = conn.execute(sql, params).fetchall()

    selected: list[tuple[str, dict[str, Any]]] = []
    already: list[str] = []

    for post_id, payload, already_in_annonces in rows:
        if not isinstance(payload, dict):
            continue
        if not _in_window(payload, start_dt, end_exclusive):
            continue
        if already_in_annonces:
            already.append(str(post_id))
        else:
            selected.append((str(post_id), payload))
        if len(selected) >= batch_size:
            break

    return selected, already


def _mark_processed(conn: psycopg.Connection, ids: list[str]) -> None:
    if not ids:
        return
    conn.execute(
        """
        UPDATE raw_posts_checkpoint
        SET processed = TRUE
        WHERE processed = FALSE
          AND post_id = ANY(%s)
        """,
        (ids,),
    )


def _count_pending(
    conn: psycopg.Connection,
    *,
    group_id: str | None,
) -> int:
    if group_id:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM raw_posts_checkpoint
            WHERE processed = FALSE AND groupe_id = %s
            """,
            (group_id,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM raw_posts_checkpoint
            WHERE processed = FALSE
            """
        ).fetchone()
    return int(row[0]) if row else 0


async def run(args: argparse.Namespace) -> int:
    if not config.DATABASE_URL:
        raise ValueError("DATABASE_URL absente dans .env.")

    start_dt = (
        datetime.combine(args.start_date, dt_time.min, tzinfo=timezone.utc)
        if args.start_date
        else None
    )
    end_exclusive = (
        datetime.combine(
            args.end_date + timedelta(days=1),
            dt_time.min,
            tzinfo=timezone.utc,
        )
        if args.end_date
        else None
    )

    temp_dir = config.RAW_DIR / "_processing"
    temp_dir.mkdir(parents=True, exist_ok=True)

    total_bruts = 0
    total_candidats = 0
    total_valides = 0
    batch_number = 0

    with psycopg.connect(
        config.DATABASE_URL,
        autocommit=True,
        connect_timeout=15,
    ) as conn:
        pending_initial = _count_pending(conn, group_id=args.group_id)
        print(f"Checkpoints pending au départ : {pending_initial}")

        while True:
            if args.max_batches and batch_number >= args.max_batches:
                break

            # On scanne plus large que batch_size car certaines lignes peuvent
            # être hors fenêtre ou déjà présentes dans annonces.
            scan_limit = max(args.batch_size * 5, args.batch_size + 100)
            selected, already = _fetch_pending(
                conn,
                group_id=args.group_id,
                start_dt=start_dt,
                end_exclusive=end_exclusive,
                scan_limit=scan_limit,
                batch_size=args.batch_size,
            )

            if already:
                _mark_processed(conn, already)
                print(
                    f"{len(already)} RAW déjà présents dans annonces -> "
                    "marqués processed sans appel OpenAI."
                )

            if not selected:
                print("Aucun autre RAW correspondant à traiter.")
                break

            batch_number += 1
            ids = [post_id for post_id, _ in selected]
            posts = [payload for _, payload in selected]

            batch_path = temp_dir / f"pending_batch_{batch_number:04d}.json"
            batch_path.write_text(
                json.dumps(posts, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            print(
                f"\nLOT {batch_number} : {len(posts)} RAW -> "
                "regex -> OpenAI -> Neon"
            )

            try:
                result = await processor.executer_traitement(
                    [batch_path],
                    mode="cursor_recovery",
                )
            except Exception:
                print(
                    f"LOT {batch_number} interrompu : aucun RAW de ce lot "
                    "n'est marqué processed. Relancer la même commande "
                    "reprendra ces lignes."
                )
                raise

            # Seulement après succès complet du pipeline du lot.
            _mark_processed(conn, ids)

            total_bruts += result.nb_posts_bruts
            total_candidats += result.nb_candidats
            total_valides += result.nb_valides

            pending_now = _count_pending(conn, group_id=args.group_id)
            print(
                f"LOT {batch_number} OK : {result.nb_posts_bruts} bruts, "
                f"{result.nb_candidats} candidats, "
                f"{result.nb_valides} valides | pending groupe={pending_now}"
            )

            try:
                batch_path.unlink()
            except OSError:
                pass

        pending_final = _count_pending(conn, group_id=args.group_id)

    print("\nTRAITEMENT DES RAW CAPTURÉS - RÉSUMÉ")
    print("-" * 56)
    print(f"Lots terminés            : {batch_number}")
    print(f"RAW traités              : {total_bruts}")
    print(f"Candidats LLM            : {total_candidats}")
    print(f"Annonces valides/upsert  : {total_valides}")
    print(f"Pending restant groupe   : {pending_final}")
    print(
        "\nLes nouveaux RAW capturés après ce traitement pourront être "
        "introduits avec exactement la même commande."
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--group-id")
    parser.add_argument("--start-date", type=_parse_date)
    parser.add_argument("--end-date", type=_parse_date)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Nombre maximal de RAW par lot (défaut : 500).",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=0,
        help="0 = tous les lots disponibles ; sinon limite le nombre de lots.",
    )
    args = parser.parse_args()

    if args.batch_size < 1:
        parser.error("--batch-size doit être >= 1")
    if args.start_date and args.end_date and args.end_date < args.start_date:
        parser.error("--end-date doit être >= --start-date")

    return args


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
