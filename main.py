"""Orchestrateur CLI du pipeline ETL : scraping -> filtrage -> structuration LLM -> export.

Exemples :
    python main.py --mode daily
    python main.py --mode backfill --days-back 14 --group-limit 5 --batch-size 3
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

# BUG CORRIGÉ (trouvé en relisant le code pendant la migration PostgreSQL) :
# python-dotenv était déclaré dans requirements.txt mais `load_dotenv()`
# n'était appelé nulle part - le fichier .env local n'était donc JAMAIS lu en
# pratique. Doit s'exécuter AVANT `import config`, car config.py lit
# DATABASE_URL depuis os.environ dès l'import du module (une fois le module
# importé et mis en cache par Python, remonter load_dotenv() plus bas n'aurait
# aucun effet sur la valeur déjà figée dans config.DATABASE_URL).
from dotenv import load_dotenv

load_dotenv()

import config
import processor
import scraper

logger = config.configurer_logging()


def parser_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parseur = argparse.ArgumentParser(
        description="Pipeline ETL annonces foncières Ouagadougou (Facebook -> CSV structuré).",
    )
    parseur.add_argument(
        "--mode",
        choices=["daily", "backfill"],
        default="daily",
        help="daily = dernières 24h ; backfill = rattrapage historique paramétrable.",
    )
    parseur.add_argument(
        "--days-back",
        type=int,
        default=None,
        help=(
            "Nombre de jours à remonter. Défaut : 1 en mode daily, "
            f"{config.MAX_DAYS_BACK_BACKFILL_DEFAULT} en mode backfill."
        ),
    )
    parseur.add_argument(
        "--group-limit",
        type=int,
        default=0,
        help="Nombre max de groupes traités sur ce run (0 = tous les groupes actifs).",
    )
    parseur.add_argument(
        "--batch-size",
        type=int,
        default=config.GROUPS_BATCH_SIZE_DEFAULT,
        help="Nombre de groupes scrollés avant une pause longue inter-batch.",
    )
    parseur.add_argument(
        "--round-robin",
        action="store_true",
        help=(
            "Traite un sous-ensemble tournant des groupes actifs à chaque run "
            "(voir --groups-per-run) au lieu de tous les groupes d'un coup dans la "
            "même session. Pensé pour un cron fréquent (ex : toutes les heures) : "
            "l'espacement entre deux passages sur un même groupe vient alors de "
            "l'espacement entre les runs, pas d'une pause interne au run."
        ),
    )
    parseur.add_argument(
        "--groups-per-run",
        type=int,
        default=1,
        help="Nombre de groupes traités à ce run en mode --round-robin (défaut : 1).",
    )
    parseur.add_argument(
        "--compte",
        choices=sorted(config.COMPTES_VALIDES),
        default=None,
        help=(
            "Identifiant du compte Facebook à utiliser (\"1\".. \"5\", voir "
            "README.md section \"Multi-comptes\"). Restreint le run au secret "
            "FB_COOKIES_JSON_<compte>, à un état persistant isolé, et aux "
            "groupes de groups.csv assignés à ce compte (colonne `compte`). "
            "Omis (défaut) : comportement historique mono-compte (secret "
            "FB_COOKIES_JSON, tous les groupes actifs confondus)."
        ),
    )
    parseur.add_argument(
        "--skip-llm",
        action="store_true",
        help="Exécute uniquement le scraping + filtrage regex (Étape A), sans appeler l'API OpenAI. "
        "Utile pour tester/débugger le scraper sans consommer de crédits API.",
    )
    args = parseur.parse_args(argv)

    if args.days_back is None:
        args.days_back = (
            config.MAX_DAYS_BACK_DAILY
            if args.mode == "daily"
            else config.MAX_DAYS_BACK_BACKFILL_DEFAULT
        )
    if args.days_back <= 0:
        parseur.error("--days-back doit être strictement positif.")
    if args.batch_size <= 0:
        parseur.error("--batch-size doit être strictement positif.")
    if args.group_limit < 0:
        parseur.error("--group-limit doit être positif ou nul (0 = tous les groupes).")
    if args.groups_per_run <= 0:
        parseur.error("--groups-per-run doit être strictement positif.")

    return args


def _exposer_sortie_github_actions(cle: str, valeur: str) -> None:
    """Écrit dans $GITHUB_OUTPUT si présent (no-op en exécution locale)."""
    chemin = os.environ.get("GITHUB_OUTPUT")
    if not chemin:
        return
    try:
        with open(chemin, "a", encoding="utf-8") as f:
            f.write(f"{cle}={valeur}\n")
    except OSError as exc:
        logger.warning("Impossible d'écrire dans GITHUB_OUTPUT : %s", exc)


def _masquer_dsn(dsn: str) -> str:
    """Masque le mot de passe d'une chaîne de connexion PostgreSQL avant de
    l'écrire dans un log ou le résumé GitHub Actions (les deux peuvent finir
    dans des captures d'écran ou des exports - jamais de secret en clair là-dedans).
    """
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", dsn)


def _ecrire_resume_github_actions(resultat: processor.ResultatTraitement) -> None:
    """Écrit un résumé lisible dans $GITHUB_STEP_SUMMARY (visible directement
    dans l'onglet Actions, sans avoir à ouvrir les logs) - no-op en local.
    """
    chemin = os.environ.get("GITHUB_STEP_SUMMARY")
    if not chemin:
        return
    lignes = [
        "## Résumé du run",
        f"- Posts bruts collectés : **{resultat.nb_posts_bruts}**",
        f"- Candidats après filtrage regex : **{resultat.nb_candidats}**",
        f"- Annonces valides ajoutées/mises à jour : **{resultat.nb_valides}**",
        f"- Base maître : `{_masquer_dsn(resultat.database_url)}`",
        f"- Export Excel : `{resultat.chemin_xlsx.name}`",
    ]
    if resultat.alerte_derive:
        lignes.append(f"\n> ⚠️ **{resultat.alerte_derive}**")
    try:
        with open(chemin, "a", encoding="utf-8") as f:
            f.write("\n".join(lignes) + "\n")
    except OSError as exc:
        logger.warning("Impossible d'écrire dans GITHUB_STEP_SUMMARY : %s", exc)


async def executer(args: argparse.Namespace) -> Path | processor.ResultatTraitement:
    logger.info(
        "=== Démarrage pipeline | compte=%s mode=%s days_back=%d group_limit=%s batch_size=%d "
        "round_robin=%s groups_per_run=%d skip_llm=%s ===",
        args.compte or "unique",
        args.mode,
        args.days_back,
        args.group_limit or "tous",
        args.batch_size,
        args.round_robin,
        args.groups_per_run,
        args.skip_llm,
    )

    fichiers_bruts = await scraper.executer_scraping(
        mode=args.mode,
        days_back=args.days_back,
        group_limit=(args.groups_per_run if args.round_robin else (args.group_limit or None)),
        groups_batch_size=args.batch_size,
        round_robin=args.round_robin,
        compte=args.compte,
    )

    if not fichiers_bruts:
        logger.warning(
            "Aucun fichier brut produit par le scraping - on continue quand même le "
            "traitement (avec 0 post) pour que la détection de dérive de volume "
            "voie ce cas, le plus révélateur d'un sélecteur cassé."
        )

    if args.skip_llm:
        posts = processor.charger_posts_bruts(fichiers_bruts)
        candidats, _ = processor.filtrer_candidats(posts)
        logger.info(
            "--skip-llm actif : %d candidats identifiés, structuration LLM ignorée.",
            len(candidats),
        )
        chemin_csv = config.PROCESSED_DIR / "candidats_sans_llm.csv"
        # export brut des candidats (sans les champs LLM) pour inspection manuelle
        import csv as _csv

        with chemin_csv.open("w", encoding="utf-8-sig", newline="") as f:
            ecrivain = _csv.DictWriter(
                f,
                fieldnames=["id", "groupe_nom", "url", "texte_nettoye"],
                extrasaction="ignore",
            )
            ecrivain.writeheader()
            ecrivain.writerows(candidats)
        return chemin_csv

    return await processor.executer_traitement(fichiers_bruts, mode=args.mode)


def main(argv: list[str] | None = None) -> int:
    args = parser_arguments(argv)
    try:
        resultat = asyncio.run(executer(args))
    except scraper.CooldownActifError as exc:
        # Pas un échec : c'est le mécanisme de sécurité anti-blocage qui fait
        # exactement ce qu'on lui demande. Code 0 pour ne pas faire échouer le
        # workflow GitHub Actions tous les jours où le cooldown est actif.
        logger.warning("Run annulé par le cooldown anti-blocage : %s", exc)
        return 0
    except ValueError as exc:
        logger.error("Erreur de configuration : %s", exc)
        return 1
    except scraper.SessionExpireeError as exc:
        logger.critical("Session Facebook expirée : %s", exc)
        return 2
    except scraper.BlocageDetecteError as exc:
        logger.critical(
            "Blocage anti-bot détecté - run arrêté, cooldown de %dh activé : %s",
            config.COOLDOWN_HEURES_APRES_BLOCAGE,
            exc,
        )
        return 3
    except Exception:
        logger.exception("Erreur fatale non gérée pendant l'exécution du pipeline.")
        return 1

    if isinstance(resultat, processor.ResultatTraitement):
        logger.info(
            "=== Pipeline terminé -> DB=%s | XLSX=%s | %d annonce(s) valide(s) ce run ===",
            _masquer_dsn(resultat.database_url),
            resultat.chemin_xlsx,
            resultat.nb_valides,
        )
        _exposer_sortie_github_actions("xlsx_path", str(resultat.chemin_xlsx))
        _ecrire_resume_github_actions(resultat)
    else:
        logger.info("=== Pipeline terminé -> %s ===", resultat)
        _exposer_sortie_github_actions("csv_path", str(resultat))
    return 0


if __name__ == "__main__":
    sys.exit(main())
