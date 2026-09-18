"""Récupère les annonces manquantes depuis les archives Neon etl_backfill_raw.

But :
- relire les sauvegardes brutes déjà présentes dans Neon ;
- cibler une période précise (31/08/2026 -> 13/09/2026 par défaut) ;
- dédupliquer les archives ;
- retirer ce qui existe déjà dans public.annonces (id, URL ou texte nettoyé) ;
- conserver le pipeline métier existant : regex -> OpenAI -> upsert Neon ;
- sauvegarder cache LLM + checkpoint après chaque lot pour reprendre sans perte.

Exemples :
    python scripts/recover_from_etl_backfill_raw.py --prepare-only
    python scripts/recover_from_etl_backfill_raw.py
    python scripts/recover_from_etl_backfill_raw.py --start-date 2026-08-31 --end-date 2026-09-13
    python scripts/recover_from_etl_backfill_raw.py --batch-size 50 --concurrency 10
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Charger .env avant config : DATABASE_URL est lue à l'import de config.
load_dotenv(ROOT / ".env")

import config
import processor

logger = config.configurer_logging()

BIGINT_MAX = 9_223_372_036_854_775_807
BIGINT_MIN = -9_223_372_036_854_775_808


def _parse_datetime(value: Any) -> datetime | None:
    """Accepte ISO, timestamp Unix secondes ou millisecondes."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    try:
        if re.fullmatch(r"\d{10}", text):
            return datetime.fromtimestamp(int(text), tz=timezone.utc)
        if re.fullmatch(r"\d{13}", text):
            return datetime.fromtimestamp(int(text) / 1000.0, tz=timezone.utc)

        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _date_publication_archive(
    item: dict[str, Any],
    *,
    allow_reconstructed_dates: bool,
) -> tuple[datetime | None, str | None]:
    """Retourne la date et sa provenance.

    Par défaut, seule date_publication_originale est considérée fiable pour
    reconstruire l'historique. date_publication peut avoir été reconstruite
    au moment du scraping et concentrer artificiellement beaucoup de posts sur
    le jour de collecte.
    """
    original = _parse_datetime(item.get("date_publication_originale"))
    if original is not None:
        return original, "originale"

    if allow_reconstructed_dates:
        reconstructed = _parse_datetime(item.get("date_publication"))
        if reconstructed is not None:
            return reconstructed, "reconstruite"

    return None, None


def _date_incertaine(item: dict[str, Any]) -> bool:
    value = item.get("date_incertaine")
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "oui"}


def _synthetic_id(item: dict[str, Any], source: str) -> str:
    base = "|".join(
        [
            str(item.get("url") or "").strip(),
            str(item.get("texte") or item.get("text") or "").strip(),
            str(item.get("date_publication_originale") or item.get("date_publication") or "").strip(),
            source,
        ]
    )
    return "backfill_" + hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]


def _normaliser_item(
    item: dict[str, Any],
    *,
    source: str,
    published: datetime,
    date_source: str,
) -> dict[str, Any]:
    post_id = str(item.get("id") or "").strip() or _synthetic_id(item, source)
    texte = str(item.get("texte") or item.get("text") or "").strip()
    url = str(item.get("url") or "").strip()
    groupe_nom = str(item.get("groupe_nom") or source).strip()

    # Seule une date originale est publiée comme date de publication réelle.
    # Une date reconstruite sert à cibler la reprise, mais ne doit pas fausser
    # les statistiques hebdomadaires de Hakimo.
    reliable_date = published.isoformat() if date_source == "originale" else ""

    return {
        "id": post_id,
        "groupe_nom": groupe_nom,
        "url": url,
        "date_publication": reliable_date,
        "date_publication_reconstruite": (
            published.isoformat() if date_source == "reconstruite" else ""
        ),
        "date_incertaine": (
            True if date_source == "reconstruite" else _date_incertaine(item)
        ),
        "texte": texte,
    }


def _cle_dedup(post: dict[str, Any]) -> str:
    post_id = str(post.get("id") or "").strip()
    if post_id and not post_id.startswith("backfill_"):
        return "id:" + post_id

    url = str(post.get("url") or "").strip()
    if url:
        return "url:" + url

    texte = processor.nettoyer_texte(str(post.get("texte") or ""))
    date_pub = str(post.get("date_publication") or "")
    return "text:" + hashlib.sha256(
        (texte + "|" + date_pub).encode("utf-8")
    ).hexdigest()


def _charger_archives(
    *,
    start: datetime,
    end_exclusive: datetime,
    include_uncertain_dates: bool,
    allow_reconstructed_dates: bool,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not config.DATABASE_URL:
        raise ValueError("DATABASE_URL absente. Vérifiez le fichier .env.")

    uniques: dict[str, dict[str, Any]] = {}
    stats = Counter()

    with psycopg.connect(config.DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT digest, compte, fichier, contenu
                FROM etl_backfill_raw
                ORDER BY archive_le, digest
                """
            )
            rows = cur.fetchall()

    for digest, compte, fichier, contenu in rows:
        stats["archives"] += 1
        if not isinstance(contenu, list):
            stats["contenus_non_liste"] += 1
            continue

        source = str(fichier or digest or f"compte_{compte}")
        for raw in contenu:
            stats["items_bruts"] += 1
            if not isinstance(raw, dict):
                stats["items_invalides"] += 1
                continue

            published, date_source = _date_publication_archive(
                raw,
                allow_reconstructed_dates=allow_reconstructed_dates,
            )
            if published is None:
                if _parse_datetime(raw.get("date_publication")) is not None:
                    stats["date_reconstruite_ignoree"] += 1
                else:
                    stats["sans_date"] += 1
                continue
            stats[f"date_{date_source}"] += 1
            if not (start <= published < end_exclusive):
                stats["hors_periode"] += 1
                continue
            if _date_incertaine(raw) and not include_uncertain_dates:
                stats["date_incertaine_ignoree"] += 1
                continue

            post = _normaliser_item(
                raw,
                source=source,
                published=published,
                date_source=str(date_source),
            )
            cle = _cle_dedup(post)
            if cle in uniques:
                stats["doublons_archives"] += 1
            uniques[cle] = post

    stats["uniques_periode"] = len(uniques)
    return list(uniques.values()), dict(stats)


def _charger_index_annonces() -> tuple[set[str], set[str], set[str]]:
    """Indexe la base maître sans la modifier."""
    ids: set[str] = set()
    urls: set[str] = set()
    textes: set[str] = set()

    with psycopg.connect(config.DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, url, texte_nettoye FROM annonces")
            for post_id, url, texte in cur.fetchall():
                if post_id:
                    ids.add(str(post_id).strip())
                if url:
                    urls.add(str(url).strip())
                nettoye = processor.nettoyer_texte(texte)
                if nettoye:
                    textes.add(nettoye)

    return ids, urls, textes


def _retirer_deja_presents(
    posts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    ids, urls, textes = _charger_index_annonces()
    manquants: list[dict[str, Any]] = []
    stats = Counter()

    for post in posts:
        post_id = str(post.get("id") or "").strip()
        url = str(post.get("url") or "").strip()
        texte = processor.nettoyer_texte(post.get("texte"))

        if post_id and post_id in ids:
            stats["deja_par_id"] += 1
            continue
        if url and url in urls:
            stats["deja_par_url"] += 1
            continue
        if texte and texte in textes:
            stats["deja_par_texte"] += 1
            continue

        manquants.append(post)

    stats["manquants"] = len(manquants)
    return manquants, dict(stats)


def _par_jour(posts: list[dict[str, Any]]) -> list[tuple[str, int]]:
    compteur: Counter[str] = Counter()
    for post in posts:
        parsed = _parse_datetime(post.get("date_publication"))
        if parsed is None:
            parsed = _parse_datetime(post.get("date_publication_reconstruite"))
        if parsed is not None:
            compteur[parsed.date().isoformat()] += 1
    return sorted(compteur.items())


def _empreinte_candidats(candidats: list[dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for post in sorted(candidats, key=lambda p: str(p.get("id") or "")):
        h.update(str(post.get("id") or "").encode("utf-8"))
        h.update(b"\0")
        h.update(str(post.get("texte_nettoye") or "").encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _ecrire_json_atomique(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temp.replace(path)


def _state_paths(start_date: date, end_date: date) -> tuple[Path, Path]:
    slug = f"{start_date:%Y%m%d}_{end_date:%Y%m%d}"
    return (
        config.STATE_DIR / f"etl_backfill_recovery_{slug}_llm_cache.json",
        config.STATE_DIR / f"etl_backfill_recovery_{slug}_checkpoint.json",
    )


def _charger_etat(
    empreinte: str,
    cache_path: Path,
    checkpoint_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    cache: dict[str, Any] = {"empreinte": empreinte, "results": {}}
    checkpoint: dict[str, Any] = {"empreinte": empreinte, "db_done_ids": []}

    for path, target_name in (
        (cache_path, "cache"),
        (checkpoint_path, "checkpoint"),
    ):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("empreinte") != empreinte:
            continue
        if target_name == "cache":
            cache = data
        else:
            checkpoint = data

    return cache, checkpoint


def _sauvegarder_etat(
    cache: dict[str, Any],
    checkpoint: dict[str, Any],
    cache_path: Path,
    checkpoint_path: Path,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    cache["updated_at"] = now
    checkpoint["updated_at"] = now
    _ecrire_json_atomique(cache_path, cache)
    _ecrire_json_atomique(checkpoint_path, checkpoint)


def _preparer_schema_neon() -> None:
    with psycopg.connect(config.DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(processor.SCHEMA_SQL)
            cur.execute(
                "ALTER TABLE annonces "
                "ALTER COLUMN prix_fcfa TYPE BIGINT USING prix_fcfa::BIGINT"
            )
            cur.execute(
                "ALTER TABLE annonces "
                "ALTER COLUMN superficie_m2 TYPE BIGINT USING superficie_m2::BIGINT"
            )
        conn.commit()


def _securiser_grands_entiers(records: list[dict[str, Any]]) -> None:
    for record in records:
        for field in ("prix_fcfa", "superficie_m2"):
            value = record.get(field)
            if isinstance(value, int) and not (BIGINT_MIN <= value <= BIGINT_MAX):
                logger.warning(
                    "%s hors plage BIGINT pour %s -> null",
                    field,
                    record.get("id"),
                )
                record[field] = None


def _lots(elements: list[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(elements), size):
        yield elements[index:index + size]


async def _traiter(
    posts: list[dict[str, Any]],
    *,
    batch_size: int,
    concurrency: int,
    cache_path: Path,
    checkpoint_path: Path,
) -> tuple[int, int, Path, Path]:
    candidats, rejetes_niveau1 = processor.filtrer_candidats(posts)
    empreinte = _empreinte_candidats(candidats)
    cache, checkpoint = _charger_etat(
        empreinte,
        cache_path,
        checkpoint_path,
    )

    results: dict[str, Any] = cache.setdefault("results", {})
    db_done = set(str(v) for v in checkpoint.setdefault("db_done_ids", []))

    # Résultats LLM déjà payés mais pas encore écrits dans Neon.
    pending_db = [
        entry["record"]
        for post_id, entry in results.items()
        if entry.get("status") == "valid"
        and post_id not in db_done
        and isinstance(entry.get("record"), dict)
    ]
    if pending_db:
        print(
            f"Reprise : {len(pending_db)} résultat(s) LLM déjà sauvegardé(s) "
            "à écrire dans Neon sans nouvel appel OpenAI."
        )
        for lot in _lots(pending_db, batch_size):
            _securiser_grands_entiers(lot)
            processor.upsert_annonces(lot)
            db_done.update(str(row["id"]) for row in lot)
            checkpoint["db_done_ids"] = sorted(db_done)
            _sauvegarder_etat(cache, checkpoint, cache_path, checkpoint_path)

    a_traiter = [
        post
        for post in candidats
        if str(post["id"]) not in results
        or results[str(post["id"])].get("status") == "failed"
    ]
    print(
        f"Checkpoint : {len(candidats) - len(a_traiter)}/{len(candidats)} "
        f"candidat(s) déjà structuré(s), {len(a_traiter)} restant(s)."
    )

    config.LLM_MAX_CONCURRENCE = max(1, concurrency)
    total_lots = math.ceil(len(a_traiter) / batch_size) if a_traiter else 0
    start_time = time.monotonic()

    for index, lot in enumerate(_lots(a_traiter, batch_size), start=1):
        lot_start = time.monotonic()
        print(
            f"\nLot {index}/{total_lots} : {len(lot)} candidat(s) -> OpenAI "
            f"(concurrence={config.LLM_MAX_CONCURRENCE})"
        )

        valides, non_valides = await processor.structurer_lot(lot)
        _securiser_grands_entiers(valides)

        # Cache LLM avant la DB : une panne Neon ne fait pas repayer le lot.
        for record in valides:
            results[str(record["id"])] = {
                "status": "valid",
                "record": record,
            }
        for record in non_valides:
            post_id = str(record.get("id") or "")
            status = (
                "failed"
                if record.get("motif_rejet") == "echec_api_ou_validation"
                else "rejected"
            )
            results[post_id] = {
                "status": status,
                "record": record,
            }

        _sauvegarder_etat(cache, checkpoint, cache_path, checkpoint_path)

        if valides:
            processor.upsert_annonces(valides)
            db_done.update(str(row["id"]) for row in valides)
            checkpoint["db_done_ids"] = sorted(db_done)
            _sauvegarder_etat(cache, checkpoint, cache_path, checkpoint_path)

        elapsed = time.monotonic() - lot_start
        processed = min(index * batch_size, len(a_traiter))
        remaining = max(0, len(a_traiter) - processed)
        speed = processed / max(time.monotonic() - start_time, 0.001)
        eta = remaining / speed if speed > 0 else 0.0
        print(
            f"Lot {index}/{total_lots} terminé : "
            f"{len(valides)} valide(s), {len(non_valides)} rejetée(s)/échouée(s), "
            f"Neon OK | {elapsed:.1f}s | ETA ~ {eta / 60:.1f} min"
        )

    valides_totales = [
        entry["record"]
        for entry in results.values()
        if entry.get("status") == "valid"
        and isinstance(entry.get("record"), dict)
    ]
    rejetes_niveau2 = [
        entry["record"]
        for entry in results.values()
        if entry.get("status") in {"rejected", "failed"}
        and isinstance(entry.get("record"), dict)
    ]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    csv_path = config.PROCESSED_DIR / f"backfill_recupere_{stamp}.csv"
    rejected_path = config.PROCESSED_DIR / f"backfill_rejetes_{stamp}.json"

    processor.enregistrer_run(
        "backfill_recovery",
        len(posts),
        len(candidats),
        len(valides_totales),
    )
    processor.exporter_csv(valides_totales, csv_path)
    processor.exporter_json_audit(
        rejetes_niveau1 + rejetes_niveau2,
        rejected_path,
    )

    return len(candidats), len(valides_totales), csv_path, rejected_path


async def _executer(args: argparse.Namespace) -> int:
    start_date = date.fromisoformat(args.start_date)
    end_date = date.fromisoformat(args.end_date)
    if end_date < start_date:
        raise ValueError("--end-date doit être >= --start-date")

    start = datetime.combine(start_date, dt_time.min, tzinfo=timezone.utc)
    end_exclusive = datetime.combine(
        end_date + timedelta(days=1),
        dt_time.min,
        tzinfo=timezone.utc,
    )

    archives, archive_stats = _charger_archives(
        start=start,
        end_exclusive=end_exclusive,
        include_uncertain_dates=args.include_uncertain_dates,
        allow_reconstructed_dates=args.include_reconstructed_dates,
    )
    manquants, present_stats = _retirer_deja_presents(archives)

    raw_path = config.RAW_DIR / (
        f"etl_backfill_recovery_{start_date:%Y%m%d}_{end_date:%Y%m%d}.json"
    )
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        json.dumps(manquants, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    print("\nRECUPERATION ETL_BACKFILL_RAW - PREPARATION")
    print("-" * 56)
    print(f"Période                 : {start_date} -> {end_date}")
    print(f"Archives Neon           : {archive_stats.get('archives', 0)}")
    print(f"Items bruts archivés    : {archive_stats.get('items_bruts', 0)}")
    print(f"Dates originales lues   : {archive_stats.get('date_originale', 0)}")
    print(
        f"Dates reconstr. ignorées: "
        f"{archive_stats.get('date_reconstruite_ignoree', 0)}"
    )
    print(f"Sans date exploitable   : {archive_stats.get('sans_date', 0)}")
    print(f"Doublons archives       : {archive_stats.get('doublons_archives', 0)}")
    print(f"Uniques dans période    : {archive_stats.get('uniques_periode', 0)}")
    print(f"Déjà présents par ID    : {present_stats.get('deja_par_id', 0)}")
    print(f"Déjà présents par URL   : {present_stats.get('deja_par_url', 0)}")
    print(f"Déjà présents par texte : {present_stats.get('deja_par_texte', 0)}")
    print(f"Posts bruts à examiner  : {len(manquants)}")
    print(f"Snapshot JSON           : {raw_path}")

    print("\nRépartition des posts bruts manquants par jour :")
    for jour, count in _par_jour(manquants):
        print(f"  {jour} : {count}")

    if args.prepare_only:
        print(
            "\nPréparation terminée (--prepare-only) : "
            "aucun appel OpenAI et aucune écriture dans annonces."
        )
        return 0

    if not manquants:
        print("\nAucun post manquant à retraiter.")
        return 0

    _preparer_schema_neon()
    cache_path, checkpoint_path = _state_paths(start_date, end_date)

    print(
        "\nLancement : filtrage regex -> OpenAI par lots -> "
        "cache/checkpoint -> Neon."
    )
    nb_candidats, nb_valides, csv_path, rejected_path = await _traiter(
        manquants,
        batch_size=max(1, args.batch_size),
        concurrency=max(1, args.concurrency),
        cache_path=cache_path,
        checkpoint_path=checkpoint_path,
    )

    print("\nRECUPERATION ETL_BACKFILL_RAW - RESULTAT")
    print("-" * 56)
    print(f"Posts bruts manquants   : {len(manquants)}")
    print(f"Candidats immobiliers   : {nb_candidats}")
    print(f"Annonces valides        : {nb_valides}")
    print(f"CSV récupéré            : {csv_path}")
    print(f"Audit rejets            : {rejected_path}")
    print(f"Cache LLM               : {cache_path}")
    print(f"Checkpoint Neon         : {checkpoint_path}")
    print(
        "\nOK - en cas de coupure, relancez la même commande : "
        "les résultats déjà structurés seront repris."
    )
    return 0


def parser_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Retraite les éléments archivés dans etl_backfill_raw qui sont "
            "absents de la table annonces, avec reprise sûre."
        )
    )
    parser.add_argument(
        "--start-date",
        default="2026-08-31",
        help="Premier jour inclus (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--end-date",
        default="2026-09-13",
        help="Dernier jour inclus (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Analyse/déduplique seulement, sans OpenAI ni écriture Neon.",
    )
    parser.add_argument(
        "--include-uncertain-dates",
        action="store_true",
        help="Inclut aussi les éléments dont date_incertaine=true.",
    )
    parser.add_argument(
        "--include-reconstructed-dates",
        action="store_true",
        help=(
            "Inclut aussi date_publication quand date_publication_originale "
            "est absente. À utiliser séparément : ces dates peuvent refléter "
            "le jour du scraping plutôt que le vrai jour de publication."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Nombre de candidats par lot (défaut : 50).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Appels OpenAI simultanés pour ce rattrapage (défaut : 10).",
    )
    return parser.parse_args()


def main() -> int:
    return asyncio.run(_executer(parser_arguments()))


if __name__ == "__main__":
    raise SystemExit(main())
