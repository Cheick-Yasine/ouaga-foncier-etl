"""Traite les données brutes déjà récupérées après un arrêt du scraper.

Cette étape est appelée par GitHub Actions uniquement si le pipeline principal
échoue. Elle fusionne les fichiers finaux ``data/raw/*.json`` et les
checkpoints du groupe interrompu ``data/raw/_live/*.json``, puis exécute le
processor normal (filtrage, structuration, upsert Neon, exports).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
import processor


def _charger_et_fusionner() -> list[dict[str, Any]]:
    fichiers = list(config.RAW_DIR.glob("*.json"))
    fichiers += list((config.RAW_DIR / "_live").glob("*.json"))

    par_id: dict[str, dict[str, Any]] = {}
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
    return list(par_id.values())


async def _executer() -> int:
    posts = _charger_et_fusionner()
    if not posts:
        print("Aucun post brut récupérable après l'échec du scraper.")
        return 0

    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    chemin = config.RAW_DIR / "recovery_merged.json"
    chemin.write_text(
        json.dumps(posts, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Récupération de secours : {len(posts)} post(s) brut(s) fusionné(s).")

    resultat = await processor.executer_traitement([chemin], mode="recovery")
    print(
        "Récupération terminée : "
        f"{resultat.nb_posts_bruts} bruts, "
        f"{resultat.nb_candidats} candidats, "
        f"{resultat.nb_valides} annonce(s) ajoutée(s)/mise(s) à jour."
    )
    return 0


def main() -> int:
    return asyncio.run(_executer())


if __name__ == "__main__":
    raise SystemExit(main())
