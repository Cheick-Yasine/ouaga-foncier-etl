"""Rattrapage fiable d'une période historique manquante.

Ce point d'entrée est volontairement séparé du quotidien. Il traite un seul
Groupe incomplet par run et considère une période comme couverte uniquement
quand le fil principal a été parcouru jusqu'en dessous de la date de début
avec plusieurs confirmations consécutives.

Exemple :
    python scripts/backfill_missing_period.py \
        --start-date 2026-08-31 --end-date 2026-09-06

Relancer la même commande : le script choisit automatiquement le prochain
groupe qui n'est pas encore marqué ``complete`` pour cette période.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
import main as pipeline_main
import scraper
from scripts.resilient_entrypoint import (
    LIVE_DIR,
    JournalLive,
    _installer_journal_live,
    _reprendre_checkpoints_en_attente,
)

COVERAGE_PATH = config.STATE_DIR / "backfill_coverage.json"
BACKFILL_MAX_EMPTY_STEPS = 20
BACKFILL_OLD_CONFIRM_STEPS = 5
BACKFILL_SESSION_CHECK_EVERY = 5

_TARGET_START: datetime | None = None
_TARGET_END_EXCLUSIVE: datetime | None = None
_PERIOD_KEY = ""


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Date invalide '{value}'. Format attendu : AAAA-MM-JJ."
        ) from exc


def _datetime_utc(jour: date) -> datetime:
    return datetime.combine(jour, dt_time.min, tzinfo=timezone.utc)


def _date_post(post: dict[str, Any]) -> datetime | None:
    brut = post.get("date_publication")
    if not brut:
        return None
    try:
        valeur = datetime.fromisoformat(str(brut).replace("Z", "+00:00"))
    except ValueError:
        return None
    if valeur.tzinfo is None:
        valeur = valeur.replace(tzinfo=timezone.utc)
    return valeur.astimezone(timezone.utc)


def _dans_periode(post: dict[str, Any]) -> bool:
    if _TARGET_START is None or _TARGET_END_EXCLUSIVE is None:
        return False
    dt = _date_post(post)
    return bool(dt and _TARGET_START <= dt < _TARGET_END_EXCLUSIVE)


def _charger_couverture() -> dict[str, Any]:
    if not COVERAGE_PATH.exists():
        return {"periods": {}}
    try:
        contenu = json.loads(COVERAGE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"periods": {}}
    if not isinstance(contenu, dict):
        return {"periods": {}}
    contenu.setdefault("periods", {})
    return contenu


def _sauvegarder_couverture(contenu: dict[str, Any]) -> None:
    COVERAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = COVERAGE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(contenu, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, COVERAGE_PATH)


def _etat_groupe(groupe_id: str) -> dict[str, Any]:
    couverture = _charger_couverture()
    periode = couverture.setdefault("periods", {}).setdefault(
        _PERIOD_KEY,
        {
            "start_date": _TARGET_START.date().isoformat() if _TARGET_START else None,
            "end_date": (
                (_TARGET_END_EXCLUSIVE - timedelta(days=1)).date().isoformat()
                if _TARGET_END_EXCLUSIVE
                else None
            ),
            "groups": {},
        },
    )
    return dict(periode.setdefault("groups", {}).get(groupe_id, {}))


def _marquer_groupe(
    groupe: config.Groupe,
    *,
    status: str,
    reason: str,
    scroll_steps: int,
    target_posts_observed: int,
    target_posts_new: int,
    oldest_seen: datetime | None,
    newest_seen: datetime | None,
) -> None:
    couverture = _charger_couverture()
    periode = couverture.setdefault("periods", {}).setdefault(
        _PERIOD_KEY,
        {
            "start_date": _TARGET_START.date().isoformat() if _TARGET_START else None,
            "end_date": (
                (_TARGET_END_EXCLUSIVE - timedelta(days=1)).date().isoformat()
                if _TARGET_END_EXCLUSIVE
                else None
            ),
            "groups": {},
        },
    )
    periode.setdefault("groups", {})[groupe.id] = {
        "group_name": groupe.nom,
        "status": status,
        "reason": reason,
        "scroll_steps": scroll_steps,
        "target_posts_observed": target_posts_observed,
        "target_posts_new": target_posts_new,
        "oldest_seen": oldest_seen.isoformat() if oldest_seen else None,
        "newest_seen": newest_seen.isoformat() if newest_seen else None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _sauvegarder_couverture(couverture)


def _choisir_prochain_groupe(
    groupes: list[config.Groupe],
    explicit_group_id: str | None = None,
) -> config.Groupe | None:
    if explicit_group_id:
        for groupe in groupes:
            if groupe.id == explicit_group_id:
                return groupe
        raise ValueError(f"Groupe {explicit_group_id} absent de groups.csv ou inactif.")

    couverture = _charger_couverture()
    groupes_etat = (
        couverture.get("periods", {})
        .get(_PERIOD_KEY, {})
        .get("groups", {})
    )
    for groupe in groupes:
        if groupes_etat.get(groupe.id, {}).get("status") != "complete":
            return groupe
    return None


class BackfillJournal(JournalLive):
    """Checkpoint Neon limité à la période visée, pour éviter de retraiter
    tout le haut du fil si le run échoue pendant le rattrapage.
    """

    def ajouter(self, groupe_id: str, posts: list[dict[str, Any]]) -> None:
        super().ajouter(groupe_id, [p for p in posts if _dans_periode(p)])


async def _scraper_groupe_backfill(
    context: Any,
    groupe: config.Groupe,
    max_days_back: int,
    seen_ids: dict[str, str],
    delai_multiplicateur: float = 1.0,
    post_repere: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Parcourt le fil principal pour une fenêtre historique précise.

    Différences avec le mode quotidien :
    - le post-repère est volontairement ignoré ;
    - les posts épinglés du HTML initial ne servent pas de signal chronologique ;
    - 20 scrolls vides sont tolérés ;
    - un vieux post isolé ne suffit jamais à arrêter le backfill ;
    - il faut 5 étapes avec contenu daté, toutes entièrement antérieures à la
      date de début, pour déclarer la période couverte.
    """
    assert _TARGET_START is not None
    assert _TARGET_END_EXCLUSIVE is not None

    page = await context.new_page()
    posts_captures: list[dict[str, Any]] = []
    taches: set[asyncio.Task[Any]] = set()
    ids_flux_vus: set[str] = set()
    ids_target_vus: set[str] = set()
    nouveaux_target: list[dict[str, Any]] = []

    compteur_reponses_vues = 0
    compteur_reponses_matchees = 0
    empty_steps = 0
    old_confirm_steps = 0
    scroll_steps = 0
    target_posts_observed = 0
    oldest_seen: datetime | None = None
    newest_seen: datetime | None = None
    status = "incomplete"
    reason = "run_interrompu"

    async def _traiter_reponse(reponse: Any) -> None:
        try:
            corps = await reponse.text()
        except Exception:
            return
        corps = corps.removeprefix("for (;;);")
        for candidat in (corps, *corps.splitlines()):
            candidat = candidat.strip()
            if not candidat:
                continue
            try:
                payload = json.loads(candidat)
            except json.JSONDecodeError:
                continue
            posts_captures.extend(
                scraper.extraire_stories_depuis_json(payload, groupe.id, groupe.nom)
            )

    def _sur_reponse(reponse: Any) -> None:
        nonlocal compteur_reponses_vues, compteur_reponses_matchees
        compteur_reponses_vues += 1
        if any(fragment in reponse.url for fragment in config.GRAPHQL_URL_FRAGMENTS):
            compteur_reponses_matchees += 1
            tache = asyncio.ensure_future(_traiter_reponse(reponse))
            taches.add(tache)
            tache.add_done_callback(taches.discard)

    page.on("response", _sur_reponse)
    url_groupe = f"{config.WEB_FACEBOOK_BASE_URL}/groups/{groupe.id}/"

    try:
        scraper.logger.info(
            "BACKFILL %s -> %s | ouverture %s (%s)",
            _TARGET_START.date(),
            (_TARGET_END_EXCLUSIVE - timedelta(days=1)).date(),
            groupe.nom,
            url_groupe,
        )
        await page.goto(url_groupe, wait_until="domcontentloaded")
        await scraper.detecter_blocage_ou_session_expiree(page)
        await asyncio.sleep(
            scraper.random.uniform(config.PAGE_DELAY_MIN_S, config.PAGE_DELAY_MAX_S)
            * delai_multiplicateur
        )

        while scroll_steps < config.MAX_PAGES_ABSOLU:
            debut_capture = len(posts_captures)
            await page.evaluate("window.scrollBy(0, window.innerHeight * 3)")
            await asyncio.sleep(
                scraper.random.uniform(config.PAGE_DELAY_MIN_S, config.PAGE_DELAY_MAX_S)
                * delai_multiplicateur
            )
            if taches:
                await asyncio.gather(*list(taches), return_exceptions=True)

            nouveaux_bruts = posts_captures[debut_capture:]
            etape: list[dict[str, Any]] = []
            for post in nouveaux_bruts:
                post_id = str(post.get("id") or "").strip()
                if not post_id or post_id in ids_flux_vus:
                    continue
                ids_flux_vus.add(post_id)
                etape.append(post)

            if etape:
                empty_steps = 0
            else:
                empty_steps += 1

            dates_etape: list[datetime] = []
            for post in etape:
                dt = _date_post(post)
                if dt is None:
                    continue
                dates_etape.append(dt)
                oldest_seen = dt if oldest_seen is None else min(oldest_seen, dt)
                newest_seen = dt if newest_seen is None else max(newest_seen, dt)

                if _TARGET_START <= dt < _TARGET_END_EXCLUSIVE:
                    target_posts_observed += 1
                    post_id = str(post.get("id") or "").strip()
                    if (
                        post_id
                        and post_id not in seen_ids
                        and post_id not in ids_target_vus
                    ):
                        ids_target_vus.add(post_id)
                        seen_ids[post_id] = post.get("scrape_le") or datetime.now(
                            timezone.utc
                        ).isoformat()
                        nouveaux_target.append(post)

            # La confirmation d'avoir dépassé la période ne repose JAMAIS sur
            # un seul vieux post. Toute l'étape doit être antérieure au début,
            # et cela doit se répéter plusieurs fois de suite.
            if dates_etape and all(dt < _TARGET_START for dt in dates_etape):
                old_confirm_steps += 1
            elif dates_etape:
                old_confirm_steps = 0

            scraper.logger.info(
                "BACKFILL %s | scroll %d | réseau=%d graphql=%d | "
                "posts flux uniques=%d | cible observés=%d nouveaux=%d | "
                "anciens confirmés=%d/%d | vides=%d/%d",
                groupe.nom,
                scroll_steps,
                compteur_reponses_vues,
                compteur_reponses_matchees,
                len(ids_flux_vus),
                target_posts_observed,
                len(nouveaux_target),
                old_confirm_steps,
                BACKFILL_OLD_CONFIRM_STEPS,
                empty_steps,
                BACKFILL_MAX_EMPTY_STEPS,
            )

            if old_confirm_steps >= BACKFILL_OLD_CONFIRM_STEPS:
                status = "complete"
                reason = "periode_depassee_confirmee"
                break

            if empty_steps >= BACKFILL_MAX_EMPTY_STEPS:
                status = "incomplete"
                reason = "20_scrolls_sans_nouveau_post"
                break

            scroll_steps += 1
            if scroll_steps % BACKFILL_SESSION_CHECK_EVERY == 0:
                await scraper.detecter_blocage_ou_session_expiree(page)
        else:
            status = "incomplete"
            reason = f"max_scrolls_{config.MAX_PAGES_ABSOLU}_atteint"

    except scraper.SessionExpireeError:
        status = "session_expired"
        reason = "session_facebook_expiree"
        raise
    except scraper.BlocageDetecteError:
        status = "blocked"
        reason = "blocage_facebook_detecte"
        raise
    except Exception:
        status = "error"
        reason = "erreur_inattendue"
        raise
    finally:
        _marquer_groupe(
            groupe,
            status=status,
            reason=reason,
            scroll_steps=scroll_steps,
            target_posts_observed=target_posts_observed,
            target_posts_new=len(nouveaux_target),
            oldest_seen=oldest_seen,
            newest_seen=newest_seen,
        )
        page.remove_listener("response", _sur_reponse)
        if taches:
            await asyncio.gather(*list(taches), return_exceptions=True)
        await page.close()

    # Ne jamais modifier le post-repère du quotidien pendant un backfill.
    return nouveaux_target, None


def _parser_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rattrape une période historique manquante, un groupe à la fois."
    )
    parser.add_argument("--start-date", required=True, type=_parse_iso_date)
    parser.add_argument("--end-date", required=True, type=_parse_iso_date)
    parser.add_argument(
        "--group-id",
        default=None,
        help="Optionnel : force un groupe précis au lieu du prochain groupe incomplet.",
    )
    parser.add_argument("--skip-llm", action="store_true")
    args = parser.parse_args()
    if args.end_date < args.start_date:
        parser.error("--end-date doit être >= --start-date.")
    return args


def main() -> int:
    global _TARGET_START, _TARGET_END_EXCLUSIVE, _PERIOD_KEY

    args = _parser_args()
    _TARGET_START = _datetime_utc(args.start_date)
    _TARGET_END_EXCLUSIVE = _datetime_utc(args.end_date + timedelta(days=1))
    _PERIOD_KEY = f"{args.start_date.isoformat()}__{args.end_date.isoformat()}"

    chargeur_original: Callable[..., list[config.Groupe]] = config.charger_groupes
    groupes = chargeur_original(limite=None)
    groupe = _choisir_prochain_groupe(groupes, args.group_id)
    if groupe is None:
        print(
            f"Backfill {_PERIOD_KEY} : tous les groupes actifs sont déjà marqués complete."
        )
        return 0

    print(
        f"Backfill {_PERIOD_KEY} : prochain groupe = {groupe.nom} ({groupe.id})."
    )

    def _charger_selection(*_args: Any, **_kwargs: Any) -> list[config.Groupe]:
        return [groupe]

    # Le pipeline existant garde la gestion des cookies, du throttle, des RAW,
    # du processor et de Neon. On remplace uniquement la stratégie de parcours.
    config.charger_groupes = _charger_selection
    scraper.scraper_groupe = _scraper_groupe_backfill

    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    _reprendre_checkpoints_en_attente()
    journal = BackfillJournal()
    _installer_journal_live(journal)

    days_back = max(1, (datetime.now(timezone.utc).date() - args.start_date).days + 1)
    cli_args = [
        "--mode",
        "backfill",
        "--days-back",
        str(days_back),
        "--group-limit",
        "1",
        "--batch-size",
        "1",
    ]
    if args.skip_llm:
        cli_args.append("--skip-llm")

    try:
        code = pipeline_main.main(cli_args)
        if code == 0:
            journal.marquer_run_traite()
            shutil.rmtree(LIVE_DIR, ignore_errors=True)
        return code
    finally:
        config.charger_groupes = chargeur_original
        journal.fermer()


if __name__ == "__main__":
    raise SystemExit(main())
