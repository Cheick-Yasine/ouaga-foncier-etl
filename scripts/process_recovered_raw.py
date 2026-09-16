"""Traite les données brutes déjà récupérées après un arrêt du scraper.

Sources de récupération :
- fichiers finaux ``data/raw/*.json`` ;
- checkpoints locaux ``data/raw/_live/*.json`` ;
- checkpoints durables ``raw_posts_checkpoint`` dans Neon.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
import processor

RUN_ID = os.environ.get("GITHUB_RUN_ID", "").strip()


def _charger_fichiers(par_id: dict[str, dict[str, Any]]) -> None:
    fichiers = list(config.RAW_DIR.glob("*.json"))
    fichiers += list((config.RAW_DIR / "_live").glob("*.json"))

    for chemin in sorted(fichiers):
        try:
            contenu = json.loads(chemin.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(contenu, list):
            continue
        for post in contenu:
            if not isinstance(post, dict):
                continue
            post_id = str(post.get("id") or "").strip()
            if post_id:
                par_id[post_id] = post


def _charger_neon(par_id: dict[str, dict[str, Any]]) -> psycopg.Connection | None:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn or not RUN_ID:
        return None
    try:
        conn = psycopg.connect(dsn, autocommit=True)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT post_id, payload
                FROM raw_posts_checkpoint
                WHERE run_id = %s AND processed = FALSE
                ORDER BY captured_at
                """,
                (RUN_ID,),
            )
            for post_id, payload in cur.fetchall():
                if isinstance(payload, dict):
                    par_id[str(post_id)] = payload
        return conn
    except Exception as exc:
        print(f"ATTENTION: lecture des checkpoints Neon impossible: {exc}")
        return None


def _charger_et_fusionner() -> tuple[list[dict[str, Any]], psycopg.Connection | None]:
    par_id: dict[str, dict[str, Any]] = {}
    _charger_fichiers(par_id)
    conn = _charger_neon(par_id)
    return list(par_id.values()), conn


async def _executer() -> int:
    posts, conn_checkpoint = _charger_et_fusionner()
    if not posts:
        print("Aucun post brut récupérable après l'échec du scraper.")
        if conn_checkpoint is not None:
            conn_checkpoint.close()
        return 0

    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    chemin = config.RAW_DIR / "recovery_merged.json"
    chemin.write_text(
        json.dumps(posts, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Récupération de secours : {len(posts)} post(s) brut(s) fusionné(s).")

    try:
        resultat = await processor.executer_traitement([chemin], mode="recovery")
        print(
            "Récupération terminée : "
            f"{resultat.nb_posts_bruts} bruts, "
            f"{resultat.nb_candidats} candidats, "
            f"{resultat.nb_valides} annonce(s) ajoutée(s)/mise(s) à jour."
        )
        if conn_checkpoint is not None and RUN_ID:
            conn_checkpoint.execute(
                "UPDATE raw_posts_checkpoint SET processed = TRUE WHERE run_id = %s",
                (RUN_ID,),
            )
        return 0
    finally:
        if conn_checkpoint is not None:
            conn_checkpoint.close()


def main() -> int:
    return asyncio.run(_executer())


if __name__ == "__main__":
    raise SystemExit(main())
