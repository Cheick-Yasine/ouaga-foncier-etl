"""Traite les données brutes déjà récupérées après un arrêt du scraper.

Sources de récupération :
- fichiers finaux ``data/raw/*.json`` ;
- checkpoints locaux ``data/raw/_live/*.json`` ;
- tous les checkpoints durables non traités ``raw_posts_checkpoint`` dans Neon.

Le mode ``--pending-only`` sert au début d'un nouveau run pour reprendre les
checkpoints Neon laissés par un run précédent interrompu brutalement.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Charge DATABASE_URL / OPENAI_API_KEY avant l'import de config.py afin que
# le script puisse être lancé directement en local.
load_dotenv(ROOT / ".env")

import config
import processor


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


def _charger_neon(
    par_id: dict[str, dict[str, Any]],
) -> tuple[psycopg.Connection | None, set[str]]:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    ids_chargees: set[str] = set()
    if not dsn:
        return None, ids_chargees

    try:
        conn = psycopg.connect(dsn, autocommit=True, connect_timeout=15)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT post_id, payload
                FROM raw_posts_checkpoint
                WHERE processed = FALSE
                ORDER BY captured_at
                """
            )
            for post_id, payload in cur.fetchall():
                post_id = str(post_id)
                if isinstance(payload, dict):
                    par_id[post_id] = payload
                    ids_chargees.add(post_id)
        return conn, ids_chargees
    except Exception as exc:
        print(f"ATTENTION: lecture des checkpoints Neon impossible: {exc}")
        return None, ids_chargees


def _charger_et_fusionner(
    *, pending_only: bool,
) -> tuple[list[dict[str, Any]], psycopg.Connection | None, set[str]]:
    par_id: dict[str, dict[str, Any]] = {}
    if not pending_only:
        _charger_fichiers(par_id)
    conn, ids_neon = _charger_neon(par_id)
    return list(par_id.values()), conn, ids_neon


async def _executer(*, pending_only: bool) -> int:
    posts, conn_checkpoint, ids_neon = _charger_et_fusionner(pending_only=pending_only)

    # La phase LLM peut durer longtemps. Ne pas garder ouverte pendant tout ce
    # temps la connexion utilisée pour lire les checkpoints, sinon Neon/SSL
    # peut la fermer avant le marquage final.
    if conn_checkpoint is not None:
        conn_checkpoint.close()
        conn_checkpoint = None

    if not posts:
        if pending_only:
            print("Aucun checkpoint Neon non traité à reprendre avant le run.")
        else:
            print("Aucun post brut récupérable après l'échec du scraper.")
        return 0

    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    chemin = config.RAW_DIR / (
        "recovery_pending.json" if pending_only else "recovery_merged.json"
    )
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

        # Ne marquer que les lignes Neon réellement chargées dans cette reprise.
        # Ouvre une connexion FRAÎCHE après le long traitement LLM pour éviter
        # l'erreur "SSL connection has been closed unexpectedly".
        if ids_neon:
            dsn = os.environ.get("DATABASE_URL", "").strip()
            if not dsn:
                raise ValueError(
                    "DATABASE_URL absente au moment du marquage des checkpoints."
                )

            last_exc: Exception | None = None
            for tentative in range(1, 4):
                conn_mark: psycopg.Connection | None = None
                try:
                    conn_mark = psycopg.connect(
                        dsn,
                        autocommit=True,
                        connect_timeout=15,
                    )
                    with conn_mark.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE raw_posts_checkpoint
                            SET processed = TRUE
                            WHERE processed = FALSE
                              AND post_id = ANY(%s)
                            """,
                            (list(ids_neon),),
                        )
                        print(
                            f"Checkpoints marqués traités : {cur.rowcount}"
                        )
                    last_exc = None
                    break
                except psycopg.OperationalError as exc:
                    last_exc = exc
                    print(
                        f"Marquage Neon : connexion interrompue, "
                        f"nouvel essai {tentative}/3..."
                    )
                    if tentative < 3:
                        await asyncio.sleep(3.0 * tentative)
                finally:
                    if conn_mark is not None:
                        try:
                            conn_mark.close()
                        except Exception:
                            pass

            if last_exc is not None:
                raise last_exc

        return 0
    finally:
        if conn_checkpoint is not None:
            conn_checkpoint.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pending-only",
        action="store_true",
        help="Traiter uniquement les checkpoints Neon non traités d'anciens runs.",
    )
    args = parser.parse_args()
    return asyncio.run(_executer(pending_only=args.pending_only))


if __name__ == "__main__":
    raise SystemExit(main())
