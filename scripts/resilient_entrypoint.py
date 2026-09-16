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
import subprocess
import sys
import time
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

# Régression constatée le 2026-09-16 : config.py était encore à 4 alors que
# le seuil validé pour les runs durables est 20. On l'impose ici au runtime
# sans modifier la logique de navigation elle-même.
config.MAX_PAGES_SANS_NOUVEAU_POST = max(
    config.MAX_PAGES_SANS_NOUVEAU_POST,
    20,
)

LIVE_DIR = config.RAW_DIR / "_live"
RUN_ID = os.environ.get("GITHUB_RUN_ID") or datetime.now(timezone.utc).strftime("local-%Y%m%dT%H%M%SZ")

CREATE_CHECKPOINT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS raw_posts_checkpoint (
    post_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    groupe_id TEXT,
    payload JSONB NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed BOOLEAN NOT NULL DEFAULT FALSE
)
"""

CREATE_CHECKPOINT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_raw_checkpoint_run
    ON raw_posts_checkpoint (run_id, processed)
"""

MAX_NEON_RETRIES = 3
RETRY_DELAYS_SECONDS = (2, 5)


def _ecriture_atomique_json(chemin: Path, contenu: list[dict[str, Any]]) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    temporaire = chemin.with_suffix(chemin.suffix + ".tmp")
    with temporaire.open("w", encoding="utf-8") as f:
        json.dump(contenu, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporaire, chemin)


def _reprendre_checkpoints_en_attente() -> None:
    """Reprend les RAW Neon non traités laissés par un ancien run.

    La reprise est best-effort : si OpenAI ou Neon est momentanément
    indisponible, le nouveau scraping peut tout de même démarrer et les lignes
    restent ``processed = FALSE`` pour le run suivant.
    """
    script = ROOT / "scripts" / "process_recovered_raw.py"
    if not script.exists() or not os.environ.get("DATABASE_URL", "").strip():
        return

    print("Vérification des checkpoints Neon non traités avant le scraping...")
    try:
        resultat = subprocess.run(
            [sys.executable, str(script), "--pending-only"],
            cwd=str(ROOT),
            check=False,
        )
        if resultat.returncode != 0:
            print(
                "ATTENTION: la reprise pré-run a échoué; les checkpoints restent "
                "non traités et seront retentés ultérieurement."
            )
    except Exception as exc:
        print(f"ATTENTION: reprise pré-run impossible: {exc}")


class JournalLive:
    def __init__(self) -> None:
        self.ids_deja_connus = set(scraper.charger_seen_ids().keys())
        self.posts_par_groupe: dict[str, dict[str, dict[str, Any]]] = {}
        self.conn: psycopg.Connection | None = None
        self.dsn = os.environ.get("DATABASE_URL", "").strip()

        if self.dsn:
            self._connecter_neon(initial=True)

    def _fermer_connexion(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None

    def _connecter_neon(self, *, initial: bool = False) -> bool:
        if not self.dsn:
            return False

        self._fermer_connexion()
        try:
            self.conn = psycopg.connect(
                self.dsn,
                autocommit=True,
                connect_timeout=15,
            )
            self.conn.execute(CREATE_CHECKPOINT_TABLE_SQL)
            self.conn.execute(CREATE_CHECKPOINT_INDEX_SQL)
            if initial:
                print("Checkpoint durable Neon activé pour les RAW du run.")
            else:
                print("Checkpoint Neon reconnecté avec succès.")
            return True
        except Exception as exc:
            print(f"ATTENTION: connexion checkpoint Neon impossible: {exc}")
            self._fermer_connexion()
            return False

    def _persister_neon(self, groupe_id: str, posts: list[dict[str, Any]]) -> None:
        if not posts or not self.dsn:
            return

        lignes = [
            (str(p["id"]), RUN_ID, groupe_id, Jsonb(p))
            for p in posts
            if p.get("id")
        ]
        if not lignes:
            return

        for tentative in range(1, MAX_NEON_RETRIES + 1):
            if self.conn is None or self.conn.closed:
                if not self._connecter_neon():
                    if tentative < MAX_NEON_RETRIES:
                        time.sleep(RETRY_DELAYS_SECONDS[tentative - 1])
                    continue

            try:
                assert self.conn is not None
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
                        lignes,
                    )
                if tentative > 1:
                    print(
                        "Checkpoint Neon rétabli : "
                        f"{len(lignes)} post(s) sauvegardé(s) après reconnexion."
                    )
                return
            except Exception as exc:
                print(
                    "ATTENTION: écriture checkpoint Neon échouée "
                    f"(tentative {tentative}/{MAX_NEON_RETRIES}): {exc}"
                )
                self._fermer_connexion()
                if tentative < MAX_NEON_RETRIES:
                    time.sleep(RETRY_DELAYS_SECONDS[tentative - 1])

        print(
            "ATTENTION: checkpoint Neon abandonné pour ce lot après 3 tentatives; "
            "le checkpoint JSON local reste disponible pour la récupération."
        )

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
        if not self.dsn:
            return

        for tentative in range(1, MAX_NEON_RETRIES + 1):
            if self.conn is None or self.conn.closed:
                if not self._connecter_neon():
                    if tentative < MAX_NEON_RETRIES:
                        time.sleep(RETRY_DELAYS_SECONDS[tentative - 1])
                    continue
            try:
                assert self.conn is not None
                self.conn.execute(
                    "UPDATE raw_posts_checkpoint SET processed = TRUE WHERE run_id = %s",
                    (RUN_ID,),
                )
                return
            except Exception as exc:
                print(
                    "ATTENTION: impossible de marquer les checkpoints traités "
                    f"(tentative {tentative}/{MAX_NEON_RETRIES}): {exc}"
                )
                self._fermer_connexion()
                if tentative < MAX_NEON_RETRIES:
                    time.sleep(RETRY_DELAYS_SECONDS[tentative - 1])

    def fermer(self) -> None:
        self._fermer_connexion()


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
    _reprendre_checkpoints_en_attente()
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
