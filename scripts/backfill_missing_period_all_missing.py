"""Backfill historique enrichi : récupère aussi les posts manquants hors cible.

La période demandée sert uniquement à valider la couverture historique. Tout
post rencontré pendant la descente et absent de la table finale ``annonces``
est également checkpointé et envoyé au processor.

Ce module enveloppe ``backfill_missing_period`` sans dupliquer sa logique de
navigation/couverture.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
import scraper
from scripts import backfill_missing_period as base
from scripts.resilient_entrypoint import LIVE_DIR, _ecriture_atomique_json


class BackfillJournalAllMissing(base.BackfillJournal):
    """Journalise tous les posts absents de ``annonces``, pas seulement la cible."""

    current: "BackfillJournalAllMissing | None" = None

    def __init__(self) -> None:
        super().__init__()
        BackfillJournalAllMissing.current = self

    def ajouter(self, groupe_id: str, posts: list[dict[str, Any]]) -> None:
        if not posts:
            return

        journal = self.posts_par_groupe.setdefault(groupe_id, {})
        nouveaux: list[dict[str, Any]] = []

        for post in posts:
            post_id = str(post.get("id") or "").strip()
            if not post_id or post_id in journal:
                continue

            # Quand la base finale a été chargée, elle est la source de vérité :
            # un ID vu autrefois mais jamais arrivé dans ``annonces`` doit être
            # récupéré à nouveau. Si Neon n'était pas lisible au démarrage, on
            # revient prudemment à seen_ids pour éviter les doublons massifs.
            if base._EXISTING_IDS_LOADED:
                if post_id in base._EXISTING_ANNONCE_IDS:
                    continue
            elif post_id in self.ids_deja_connus:
                continue

            journal[post_id] = post
            nouveaux.append(post)

        if not nouveaux:
            return

        # Même garantie que le pipeline résilient normal : local d'abord,
        # puis Neon. En cas de coupure, le traitement de secours peut reprendre.
        _ecriture_atomique_json(
            LIVE_DIR / f"live_{groupe_id}.json",
            list(journal.values()),
        )
        self._persister_neon(groupe_id, nouveaux)


_original_scraper_backfill = base._scraper_groupe_backfill


async def _scraper_groupe_backfill_all_missing(
    context: Any,
    groupe: config.Groupe,
    max_days_back: int,
    seen_ids: dict[str, str],
    delai_multiplicateur: float = 1.0,
    post_repere: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Retourne à la fois la cible historique et tout post manquant rencontré."""

    posts_cible, nouveau_repere = await _original_scraper_backfill(
        context,
        groupe,
        max_days_back,
        seen_ids,
        delai_multiplicateur=delai_multiplicateur,
        post_repere=post_repere,
    )

    journal = BackfillJournalAllMissing.current
    tous_manquants = (
        list(journal.posts_par_groupe.get(groupe.id, {}).values())
        if journal is not None
        else []
    )

    fusion: dict[str, dict[str, Any]] = {}
    for post in (*tous_manquants, *posts_cible):
        post_id = str(post.get("id") or "").strip()
        if post_id:
            fusion[post_id] = post

    scraper.logger.info(
        "BACKFILL %s | posts manquants rencontrés=%d | dont cible historique=%d",
        groupe.nom,
        len(fusion),
        len(posts_cible),
    )
    return list(fusion.values()), nouveau_repere


def main() -> int:
    # base.main() cherche ces noms au moment de l'exécution : on remplace donc
    # uniquement le journal et la fonction de parcours, sans modifier le reste.
    base.BackfillJournal = BackfillJournalAllMissing
    base._scraper_groupe_backfill = _scraper_groupe_backfill_all_missing
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
