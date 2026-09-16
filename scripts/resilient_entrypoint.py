"""Point d'entrée résilient pour les runs GitHub Actions.

Double persistance pendant la collecte :
1. checkpoint JSON atomique dans ``data/raw/_live`` ;
2. checkpoint durable dans PostgreSQL/Neon après chaque lot de posts détectés.

Le second filet protège même contre une destruction brutale du runner GitHub
(timeout maximal, panne du runner), cas où les étapes ``if: always()`` peuvent
ne plus avoir l'occasion d'uploader les fichiers locaux.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
import main as pipeline_main
import scraper

LIVE_DIR = config.RAW_DIR / "_live"
RUN_ID = os.environ.get("GITHUB_RUN_ID") or datetime.now(timezone.utc).strftime("local-%Y%m%dT%H%M%SZ")

CHECKPOINT_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_posts_checkpoint (
    post_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    groupe_id TEXT,
    payload JSONB NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_raw_checkpoint_run
    ON raw_posts_checkpoint (run_id, processed);
"""


def _ecriture_atomique_json(chemin: Path, contenu: list[dict[str, Any]]) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    temporaire = chemin.with_suffix(chemin.suffix + ".tmp")
    with temporaire.open("w", encoding="utf-8") as f:
        json.dump(contenu, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporaire, chemin)


class JournalLive:
    def __init__(self) -> None:
        self.ids_deja_connus = set(scraper.charger_seen_ids().keys())
        self.posts_par_groupe: dict[str, dict[str, dict[str, Any]]] = {}
        self.conn: psycopg.Connection | None = None

        dsn = os.environ.get("DATABASE_URL", "").strip()
        if dsn:
            try:
                self.conn = psycopg.connect(dsn, autocommit=True)
                self.conn.execute(CHECKPOINT_SCHEMA)
                print("Checkpoint durable Neon activé pour les RAW du run.")
            except Exception as exc:
                # Le fichier local live reste un filet de sécurité si Neon est
                # momentanément indisponible. Le scraping ne doit pas tomber
                # uniquement à cause du mécanisme de sauvegarde secondaire.
                print(f"ATTENTION: checkpoint Neon indisponible: {exc}")
                self.conn = None

    def _persister_neon(self, groupe_id: str, posts: list[dict[str, Any]]) -> None:
        if self.conn is None or not posts:
            return
        try:
            with self.conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO raw_posts_checkpoint
                        (post_id, run_id, groupe_id, payload, captured_at, processed)
                    VALUES (%s, %s, %s, %s, NOW(), FALSE)
                    ON CONFLICT (post_id) DO UPDATE SET
                        run_id = EXCLUDED.run_id,
                        groupe_id = EXCLUDED.groupe_id,
                        payload = EXCLUDED.payload,
                        captured_at = NOW(),
                        processed = FALSE
                    """,
                    [
                        (str(p["id"]), RUN_ID, groupe_id, Jsonb(p))
                        for p in posts
                        if p.get("id")
                    ],
                )
        except Exception as exc:
            print(f"ATTENTION: écriture checkpoint Neon échouée: {exc}")

    def ajouter(self, groupe_id: str, posts: list[dict[str, Any]]) -> None:
        if not posts:
            return
        journal = self.posts_par_groupe.setdefault(groupe_id, {})
        nouveaux: list[dict[str, Any]] = []
        for post in posts:
            post_id = str(post.get("id") or "").strip()
            if not post_id or post_id in self.ids_deja_connus or post_id in journal:
                continue
            journal[post_id] = post
            nouveaux.append(post)

        if nouveaux:
            _ecriture_atomique_json(
                LIVE_DIR / f"live_{groupe_id}.json",
                list(journal.values()),
            )
            self._persister_neon(groupe_id, nouveaux)

    def marquer_run_traite(self) -> None:
        if self.conn is None:
            return
        try:
            self.conn.execute(
                "UPDATE raw_posts_checkpoint SET processed = TRUE WHERE run_id = %s",
                (RUN_ID,),
            )
        except Exception as exc:
            print(f"ATTENTION: impossible de marquer les checkpoints traités: {exc}")

    def fermer(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass


def _installer_journal_live(journal: JournalLive) -> None:
    parseur_graphql: Callable[..., list[dict[str, Any]]] = scraper.extraire_stories_depuis_json

    def parseur_graphql_journalise(
        payload: Any, groupe_id: str, groupe_nom: str
    ) -> list[dict[str, Any]]:
        posts = parseur_graphql(payload, groupe_id, groupe_nom)
        journal.ajouter(groupe_id, posts)
        return posts

    scraper.extraire_stories_depuis_json = parseur_graphql_journalise

    parseur_html = getattr(scraper, "_extraire_stories_depuis_scripts_json", None)
    if parseur_html is not None:
        def parseur_html_journalise(
            html: str, groupe_id: str, groupe_nom: str
        ) -> list[dict[str, Any]]:
            posts = parseur_html(html, groupe_id, groupe_nom)
            journal.ajouter(groupe_id, posts)
            return posts

        scraper._extraire_stories_depuis_scripts_json = parseur_html_journalise


def main() -> int:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    journal = JournalLive()
    _installer_journal_live(journal)
    try:
        code = pipeline_main.main(sys.argv[1:])
        if code == 0:
            journal.marquer_run_traite()
            shutil.rmtree(LIVE_DIR, ignore_errors=True)
        return code
    finally:
        journal.fermer()


if __name__ == "__main__":
    raise SystemExit(main())
