"""Importe en masse des exports Apify dans le pipeline ETL existant.

Le script ne filtre PAS par date et ne supprime aucun post selon son contenu.
Il :
1. lit tous les .xlsx/.xlsm/.csv/.json d'un dossier ;
2. normalise les colonnes courantes des différents Actors Apify ;
3. déduplique uniquement les doublons techniques (ID Facebook, URL, puis
   empreinte stable en dernier recours) ;
4. écrit un JSON brut compatible avec processor.py ;
5. lance le traitement existant (filtrage métier + structuration LLM + upsert
   Neon), sauf avec --prepare-only.

Exemples :
    python scripts/import_apify_recovery.py data/apify_recovery
    python scripts/import_apify_recovery.py data/apify_recovery --prepare-only
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import re
import sys
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# IMPORTANT : charger .env avant config, car config.DATABASE_URL est lu à l'import.
load_dotenv(ROOT / ".env")

import config
import processor

logger = config.configurer_logging()

EXTENSIONS_ACCEPTEES = {".xlsx", ".xlsm", ".csv", ".json"}

# Les exports Apify varient selon l'Actor. On compare des noms de colonnes
# normalisés (minuscules, sans accents ni ponctuation).
ALIASES = {
    "id": {
        "id", "postid", "postidentifier", "facebookid", "facebookpostid",
        "postfacebookid", "postpk", "postkey",
    },
    "texte": {
        "text", "texte", "message", "content", "contenu", "posttext",
        "postmessage", "body", "description", "caption",
    },
    "url": {
        "url", "posturl", "facebookurl", "permalink", "postlink",
        "link", "postpermalink",
    },
    "date_publication": {
        "time", "date", "datetime", "timestamp", "createdat", "createdtime",
        "publishedat", "publicationdate", "datepublication", "postdate",
        "posttime",
    },
    "groupe_nom": {
        "groupname", "groupe", "group", "pagename", "page", "profilename",
        "profile", "authorname", "username", "ownername", "sourcename",
    },
}


def _normaliser_nom_colonne(valeur: Any) -> str:
    texte = unicodedata.normalize("NFKD", str(valeur or ""))
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", texte.lower())


def _nettoyer_cellule(valeur: Any) -> Any:
    if isinstance(valeur, (datetime, date)):
        # Les dates Excel naïves sont conservées sans inventer de fuseau.
        return valeur.isoformat()
    if valeur is None:
        return None
    if isinstance(valeur, str):
        valeur = valeur.strip()
        return valeur or None
    return valeur


def _aplatir_dict(obj: dict[str, Any], prefixe: str = "") -> dict[str, Any]:
    """Aplatit légèrement les JSON Apify (ex. author.name -> authorname)."""
    sortie: dict[str, Any] = {}
    for cle, valeur in obj.items():
        nom = f"{prefixe}{cle}" if prefixe else str(cle)
        if isinstance(valeur, dict):
            sortie.update(_aplatir_dict(valeur, nom))
        else:
            sortie[nom] = valeur
    return sortie


def _valeur_alias(ligne: dict[str, Any], champ: str) -> Any:
    index = {
        _normaliser_nom_colonne(cle): _nettoyer_cellule(valeur)
        for cle, valeur in ligne.items()
    }
    for alias in ALIASES[champ]:
        if alias in index and index[alias] not in (None, ""):
            return index[alias]
    return None


_RE_ID_URL = [
    re.compile(r"/posts/(\d+)", re.I),
    re.compile(r"/permalink/(\d+)", re.I),
    re.compile(r"[?&]story_fbid=(\d+)", re.I),
    re.compile(r"[?&]fbid=(\d+)", re.I),
]


def _extraire_id_url(url: str | None) -> str | None:
    if not url:
        return None
    for motif in _RE_ID_URL:
        match = motif.search(str(url))
        if match:
            return match.group(1)
    return None


def _normaliser_id(valeur: Any) -> str | None:
    if valeur is None:
        return None
    if isinstance(valeur, float) and valeur.is_integer():
        return str(int(valeur))
    texte = str(valeur).strip()
    if not texte:
        return None
    # Excel peut parfois convertir un identifiant numérique en "... .0".
    if re.fullmatch(r"\d+\.0", texte):
        return texte[:-2]
    return texte


def _id_synthetique(url: str | None, texte: str | None, date_pub: str | None, source: str) -> str:
    base = "|".join([
        str(url or "").strip(),
        str(texte or "").strip(),
        str(date_pub or "").strip(),
        source,
    ])
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]
    return f"apify_{digest}"


def _normaliser_ligne(ligne: dict[str, Any], source: str) -> dict[str, Any]:
    ligne = _aplatir_dict(ligne)

    texte = _valeur_alias(ligne, "texte")
    url = _valeur_alias(ligne, "url")
    date_pub = _valeur_alias(ligne, "date_publication")
    groupe_nom = _valeur_alias(ligne, "groupe_nom") or source

    post_id = _normaliser_id(_valeur_alias(ligne, "id"))
    if not post_id:
        post_id = _extraire_id_url(str(url) if url else None)
    if not post_id:
        post_id = _id_synthetique(
            str(url) if url else None,
            str(texte) if texte else None,
            str(date_pub) if date_pub else None,
            source,
        )

    return {
        "id": post_id,
        "groupe_nom": str(groupe_nom).strip(),
        "url": str(url).strip() if url else "",
        "date_publication": str(date_pub).strip() if date_pub else "",
        "date_incertaine": not bool(date_pub),
        "texte": str(texte).strip() if texte else "",
    }


def _lire_excel(chemin: Path) -> Iterable[dict[str, Any]]:
    classeur = load_workbook(chemin, read_only=True, data_only=True)
    try:
        for feuille in classeur.worksheets:
            lignes = feuille.iter_rows(values_only=True)
            try:
                entetes_brutes = next(lignes)
            except StopIteration:
                continue

            entetes = [
                str(v).strip() if v is not None else f"colonne_{i}"
                for i, v in enumerate(entetes_brutes, start=1)
            ]
            for valeurs in lignes:
                if not any(v not in (None, "") for v in valeurs):
                    continue
                yield {
                    entetes[i]: valeurs[i] if i < len(valeurs) else None
                    for i in range(len(entetes))
                }
    finally:
        classeur.close()


def _detecter_dialecte_csv(chemin: Path) -> csv.Dialect:
    echantillon = chemin.read_text(encoding="utf-8-sig", errors="replace")[:8192]
    try:
        return csv.Sniffer().sniff(echantillon, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def _lire_csv(chemin: Path) -> Iterable[dict[str, Any]]:
    dialecte = _detecter_dialecte_csv(chemin)
    with chemin.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
        for ligne in csv.DictReader(f, dialect=dialecte):
            yield dict(ligne)


def _lire_json(chemin: Path) -> Iterable[dict[str, Any]]:
    with chemin.open("r", encoding="utf-8") as f:
        contenu = json.load(f)

    if isinstance(contenu, list):
        objets = contenu
    elif isinstance(contenu, dict):
        # Formats fréquents : {"items": [...]}, {"data": [...]}, {"results": [...]}.
        objets = None
        for cle in ("items", "data", "results", "posts"):
            valeur = contenu.get(cle)
            if isinstance(valeur, list):
                objets = valeur
                break
        if objets is None:
            objets = [contenu]
    else:
        objets = []

    for objet in objets:
        if isinstance(objet, dict):
            yield objet


def _lire_fichier(chemin: Path) -> Iterable[dict[str, Any]]:
    suffixe = chemin.suffix.lower()
    if suffixe in {".xlsx", ".xlsm"}:
        yield from _lire_excel(chemin)
    elif suffixe == ".csv":
        yield from _lire_csv(chemin)
    elif suffixe == ".json":
        yield from _lire_json(chemin)


def _cle_dedup(post: dict[str, Any]) -> str:
    post_id = str(post.get("id") or "").strip()
    if post_id and not post_id.startswith("apify_"):
        return f"id:{post_id}"

    url = str(post.get("url") or "").strip()
    if url:
        return f"url:{url}"

    return f"id:{post_id}"


def preparer_import(dossier: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    fichiers = sorted(
        p for p in dossier.rglob("*")
        if p.is_file() and p.suffix.lower() in EXTENSIONS_ACCEPTEES
    )
    if not fichiers:
        raise FileNotFoundError(
            f"Aucun fichier .xlsx/.xlsm/.csv/.json trouvé dans {dossier}"
        )

    uniques: dict[str, dict[str, Any]] = {}
    fichiers_ok = 0
    fichiers_erreur: list[tuple[str, str]] = []
    nb_lignes = 0
    nb_doublons = 0
    nb_ids_synthetiques = 0

    for chemin in fichiers:
        source = chemin.stem
        compteur_fichier = 0
        try:
            for ligne in _lire_fichier(chemin):
                nb_lignes += 1
                compteur_fichier += 1
                post = _normaliser_ligne(ligne, source=source)
                if post["id"].startswith("apify_"):
                    nb_ids_synthetiques += 1

                cle = _cle_dedup(post)
                if cle in uniques:
                    nb_doublons += 1
                # Dernière occurrence gagnante : pratique si un export plus récent
                # contient davantage de champs qu'un export précédent.
                uniques[cle] = post

            fichiers_ok += 1
            print(f"OK  {chemin.name}: {compteur_fichier} ligne(s)")
        except Exception as exc:
            fichiers_erreur.append((chemin.name, str(exc)))
            print(f"ERREUR  {chemin.name}: {exc}")

    stats = {
        "fichiers_trouves": len(fichiers),
        "fichiers_ok": fichiers_ok,
        "fichiers_erreur": fichiers_erreur,
        "lignes_brutes": nb_lignes,
        "doublons": nb_doublons,
        "uniques": len(uniques),
        "ids_synthetiques": nb_ids_synthetiques,
    }
    return list(uniques.values()), stats


async def _executer(args: argparse.Namespace) -> int:
    dossier = Path(args.dossier).expanduser().resolve()
    if not dossier.exists() or not dossier.is_dir():
        raise FileNotFoundError(f"Dossier introuvable : {dossier}")

    posts, stats = preparer_import(dossier)

    sortie = Path(args.output).expanduser()
    if not sortie.is_absolute():
        sortie = ROOT / sortie
    sortie.parent.mkdir(parents=True, exist_ok=True)
    sortie.write_text(
        json.dumps(posts, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    print("\nIMPORT APIFY - PREPARATION")
    print("-" * 42)
    print(f"Fichiers trouvés       : {stats['fichiers_trouves']}")
    print(f"Fichiers lus           : {stats['fichiers_ok']}")
    print(f"Lignes brutes          : {stats['lignes_brutes']}")
    print(f"Doublons techniques    : {stats['doublons']}")
    print(f"Posts uniques          : {stats['uniques']}")
    print(f"IDs synthétiques       : {stats['ids_synthetiques']}")
    print(f"JSON normalisé         : {sortie}")

    if stats["fichiers_erreur"]:
        print("\nFichiers ignorés à cause d'une erreur :")
        for nom, erreur in stats["fichiers_erreur"]:
            print(f"  - {nom}: {erreur}")

    if args.prepare_only:
        print("\nPréparation terminée (--prepare-only) : aucune écriture dans Neon.")
        return 0

    if not posts:
        print("\nAucun post à traiter.")
        return 0

    print("\nLancement du processor existant -> OpenAI -> Neon...")
    resultat = await processor.executer_traitement([sortie], mode="recovery")

    print("\nIMPORT APIFY - RESULTAT")
    print("-" * 42)
    print(f"Posts bruts chargés    : {resultat.nb_posts_bruts}")
    print(f"Candidats immobiliers  : {resultat.nb_candidats}")
    print(f"Annonces valides       : {resultat.nb_valides}")
    print(f"Ajouts/mises à jour DB : {resultat.nb_valides}")
    print(f"CSV du run             : {resultat.chemin_csv_run}")
    print(f"Excel maître           : {resultat.chemin_xlsx}")
    print("\nOK - import terminé dans Neon.")
    return 0


def parser_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fusionne tous les exports Apify d'un dossier et les envoie dans "
            "le pipeline ETL existant, sans filtre de date."
        )
    )
    parser.add_argument(
        "dossier",
        nargs="?",
        default=str(ROOT / "data" / "apify_recovery"),
        help="Dossier contenant les exports Apify.",
    )
    parser.add_argument(
        "--output",
        default="data/raw/apify_recovery_merged.json",
        help="JSON brut normalisé produit avant le traitement.",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help=(
            "Fusionne/normalise/déduplique les fichiers mais ne lance ni "
            "OpenAI ni l'écriture Neon."
        ),
    )
    return parser.parse_args()


def main() -> int:
    return asyncio.run(_executer(parser_arguments()))


if __name__ == "__main__":
    raise SystemExit(main())
