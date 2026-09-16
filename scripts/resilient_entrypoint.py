"""Point d'entrée résilient pour les runs GitHub Actions.

Ce wrapper ne change pas la logique de scraping. Il journalise en revanche les
posts détectés au fil des réponses Facebook dans ``data/raw/_live``. Ainsi, si
le scraper s'arrête avant la fin d'un groupe (session expirée, blocage, timeout
contrôlé), les posts déjà vus dans ce groupe existent encore sur disque et
peuvent être uploadés/traités par l'étape de secours du workflow.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
import main as pipeline_main
import scraper

LIVE_DIR = config.RAW_DIR / "_live"


def _ecriture_atomique_json(chemin: Path, contenu: list[dict[str, Any]]) -> None:
    """Écrit un checkpoint sans laisser un JSON partiel en cas d'interruption."""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    temporaire = chemin.with_suffix(chemin.suffix + ".tmp")
    with temporaire.open("w", encoding="utf-8") as f:
        json.dump(contenu, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporaire, chemin)


class JournalLive:
    def __init__(self) -> None:
        # Snapshot pris AVANT le run : on ne journalise pas les IDs déjà connus
        # d'un run précédent. Les doublons rencontrés plusieurs fois pendant le
        # run courant sont ensuite éliminés par le dictionnaire par ID.
        self.ids_deja_connus = set(scraper.charger_seen_ids().keys())
        self.posts_par_groupe: dict[str, dict[str, dict[str, Any]]] = {}

    def ajouter(self, groupe_id: str, posts: list[dict[str, Any]]) -> None:
        if not posts:
            return
        journal = self.posts_par_groupe.setdefault(groupe_id, {})
        modifie = False
        for post in posts:
            post_id = str(post.get("id") or "").strip()
            if not post_id or post_id in self.ids_deja_connus or post_id in journal:
                continue
            journal[post_id] = post
            modifie = True
        if modifie:
            _ecriture_atomique_json(
                LIVE_DIR / f"live_{groupe_id}.json",
                list(journal.values()),
            )


def _installer_journal_live(journal: JournalLive) -> None:
    """Entoure les deux parseurs existants sans modifier leur comportement."""
    parseur_graphql: Callable[..., list[dict[str, Any]]] = scraper.extraire_stories_depuis_json

    def parseur_graphql_journalise(
        payload: Any, groupe_id: str, groupe_nom: str
    ) -> list[dict[str, Any]]:
        posts = parseur_graphql(payload, groupe_id, groupe_nom)
        journal.ajouter(groupe_id, posts)
        return posts

    scraper.extraire_stories_depuis_json = parseur_graphql_journalise

    # Le HTML initial utilise un parseur distinct. On le journalise aussi pour
    # couvrir une interruption très tôt dans le groupe.
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

    code = pipeline_main.main(sys.argv[1:])

    # Si tout s'est bien terminé, les fichiers finaux data/raw horodatés ont
    # déjà été produits et traités par main.py : les checkpoints live ne sont
    # plus utiles. En cas d'échec on les conserve pour l'étape de secours.
    if code == 0:
        shutil.rmtree(LIVE_DIR, ignore_errors=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
