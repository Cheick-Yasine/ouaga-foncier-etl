"""Importe rapidement le JSON Apify normalisé dans Neon, sans LLM ni filtrage.

Usage :
    python scripts/import_apify_raw_neon.py
    python scripts/import_apify_raw_neon.py data/raw/apify_recovery_merged.json

Tous les posts sont conservés dans la table raw_facebook_posts. La table
annonces reste réservée aux annonces structurées par le pipeline métier.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import psycopg
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

import config

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS raw_facebook_posts (
    id TEXT PRIMARY KEY,
    groupe_nom TEXT,
    url TEXT,
    date_publication TEXT,
    date_incertaine BOOLEAN NOT NULL DEFAULT FALSE,
    texte TEXT,
    source TEXT NOT NULL DEFAULT 'apify',
    payload JSONB,
    premiere_collecte TIMESTAMPTZ NOT NULL,
    derniere_maj TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_facebook_posts_date
    ON raw_facebook_posts (date_publication);
CREATE INDEX IF NOT EXISTS idx_raw_facebook_posts_source
    ON raw_facebook_posts (source);
"""


def charger_posts(chemin: Path) -> list[dict[str, Any]]:
    with chemin.open("r", encoding="utf-8") as f:
        contenu = json.load(f)
    if not isinstance(contenu, list):
        raise ValueError("Le JSON normalisé doit contenir une liste de posts.")

    uniques: dict[str, dict[str, Any]] = {}
    for post in contenu:
        if not isinstance(post, dict):
            continue
        post_id = str(post.get("id") or "").strip()
        if not post_id:
            continue
        uniques[post_id] = post
    return list(uniques.values())


def importer(posts: list[dict[str, Any]], dsn: str) -> tuple[int, int, int]:
    if not posts:
        return 0, 0, 0

    maintenant = datetime.now(timezone.utc)
    ids = [str(p["id"]) for p in posts]

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)

            cur.execute(
                "SELECT id FROM raw_facebook_posts WHERE id = ANY(%s)",
                (ids,),
            )
            deja_presents = {row[0] for row in cur.fetchall()}

            lignes = []
            for p in posts:
                lignes.append(
                    (
                        str(p["id"]),
                        p.get("groupe_nom"),
                        p.get("url"),
                        p.get("date_publication"),
                        bool(p.get("date_incertaine")),
                        p.get("texte"),
                        "apify",
                        Jsonb(p),
                        maintenant,
                        maintenant,
                    )
                )

            cur.executemany(
                """
                INSERT INTO raw_facebook_posts (
                    id, groupe_nom, url, date_publication, date_incertaine,
                    texte, source, payload, premiere_collecte, derniere_maj
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    groupe_nom = EXCLUDED.groupe_nom,
                    url = EXCLUDED.url,
                    date_publication = EXCLUDED.date_publication,
                    date_incertaine = EXCLUDED.date_incertaine,
                    texte = EXCLUDED.texte,
                    source = EXCLUDED.source,
                    payload = EXCLUDED.payload,
                    derniere_maj = EXCLUDED.derniere_maj
                """,
                lignes,
            )
        conn.commit()

    nb_updates = len(deja_presents)
    nb_nouveaux = len(posts) - nb_updates
    return len(posts), nb_nouveaux, nb_updates


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Charge tous les posts Apify normalisés dans Neon sans OpenAI."
    )
    parser.add_argument(
        "fichier",
        nargs="?",
        default=str(ROOT / "data" / "raw" / "apify_recovery_merged.json"),
        help="JSON produit par import_apify_recovery.py --prepare-only.",
    )
    args = parser.parse_args()

    chemin = Path(args.fichier).expanduser().resolve()
    if not chemin.exists():
        raise FileNotFoundError(f"Fichier introuvable : {chemin}")
    if not config.DATABASE_URL:
        raise ValueError(
            "DATABASE_URL absente. Vérifiez votre fichier .env avant l'import."
        )

    posts = charger_posts(chemin)
    print(f"Posts uniques à charger : {len(posts)}")
    print("Import Neon brut en cours (aucun appel OpenAI)...")

    total, nouveaux, updates = importer(posts, config.DATABASE_URL)

    print("\nIMPORT BRUT NEON")
    print("-" * 40)
    print(f"Posts upsertés          : {total}")
    print(f"Nouveaux posts          : {nouveaux}")
    print(f"Posts déjà présents     : {updates}")
    print("Table                   : raw_facebook_posts")
    print("\nOK - tous les posts sont conservés dans Neon.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
