#!/usr/bin/env python3
"""Recharge FB_COOKIES_JSON depuis une connexion interactive ou un export.

Contexte : quand `SessionExpireeError` est levée pendant un run, le pipeline
s'arrête, active un cooldown d'1h (config.COOLDOWN_HEURES_APRES_SESSION_EXPIREE)
et il faut régénérer le secret GitHub `FB_COOKIES_JSON` à la main. Ce script
fait le lien entre "j'ai exporté de nouveaux cookies avec Cookie-Editor" et "le
prochain run les utilise vraiment" :

1. Valide/normalise le fichier exporté (mêmes règles que `scraper.charger_cookies`
   : présence de c_user/xs, tolérance aux champs `expirationDate`/`sameSite`
   propres au format des extensions de navigateur).
2. Met à jour le secret GitHub `FB_COOKIES_JSON` (via `gh secret set`, si `gh`
   est installé et authentifié - sinon affiche la commande à lancer soi-même).
3. Purge l'état local qui bloquerait sinon le prochain run malgré les nouveaux
   cookies : le cooldown anti-blocage ET le storage_state mis en cache (ce
   dernier a PRIORITÉ sur FB_COOKIES_JSON au chargement - voir
   `scraper._charger_cookies_caches` -, donc le laisser en place ferait
   ignorer silencieusement les cookies fraîchement fournis ici. Voir aussi
   `scraper.invalider_storage_state`, qui fait la même purge côté run).
4. Optionnel (--clear-actions-cache) : supprime aussi le cache GitHub Actions
   `etat-scraper-*`, qui contient la même chose côté CI, pour ne pas attendre
   qu'il expire ou soit écrasé naturellement.

Usage :
    python scripts/maj_cookies.py --interactive --repo owner/repo --compte 1 --set-secret
    python scripts/maj_cookies.py export_cookie_editor.json
    python scripts/maj_cookies.py export_cookie_editor.json --repo owner/repo --set-secret
    python scripts/maj_cookies.py export_cookie_editor.json --repo owner/repo --set-secret --clear-actions-cache

Le fichier `export_cookie_editor.json` est l'export BRUT de l'extension
(liste de cookies avec `expirationDate`, `sameSite` en style `no_restriction`,
etc.) - PAS besoin de le convertir à la main, `scraper.charger_cookies` s'en
charge à l'exécution. C'est ce format brut (pas la version normalisée) qui est
stocké dans le secret.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import scraper  # noqa: E402


def valider_cookies_captures(cookies: list[dict]) -> str:
    """Filtre et valide les cookies Facebook capturés par Playwright.

    Le JSON retourné est compact et directement compatible avec le secret
    ``FB_COOKIES_JSON_<compte>``. Une absence de ``c_user`` ou ``xs`` signifie
    que la connexion interactive n'est pas terminée : on échoue sans jamais
    envoyer un secret incomplet à GitHub.
    """
    cookies_facebook = [
        cookie
        for cookie in cookies
        if (
            (domaine := str(cookie.get("domain", "")).lstrip(".").lower())
            == "facebook.com"
            or domaine.endswith(".facebook.com")
        )
    ]
    texte = json.dumps(cookies_facebook, ensure_ascii=False, separators=(",", ":"))
    cookies_normalises = scraper.charger_cookies(texte)
    noms = {cookie["name"] for cookie in cookies_normalises}
    manquants = {"c_user", "xs"} - noms
    if manquants:
        raise ValueError(
            "Connexion Facebook non confirmée : cookie(s) requis absent(s) : "
            + ", ".join(sorted(manquants))
        )
    return texte


def capturer_session_interactive() -> str:
    """Ouvre Chromium visiblement et capture la session après validation humaine.

    Le mot de passe et la 2FA sont saisis directement dans Facebook. Le script
    ne les lit ni ne les enregistre ; il récupère uniquement les cookies une
    fois que l'utilisateur confirme que la connexion est terminée.
    """
    from playwright.sync_api import sync_playwright

    print(
        "\nChromium va s'ouvrir. Connectez-vous vous-même au BON compte Facebook, "
        "terminez la 2FA ou toute vérification éventuelle, puis revenez ici."
    )
    with sync_playwright() as playwright:
        navigateur = playwright.chromium.launch(headless=False)
        contexte = navigateur.new_context(
            locale="fr-FR",
            timezone_id="Africa/Ouagadougou",
        )
        try:
            page = contexte.new_page()
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
            input("Appuyez sur Entrée uniquement lorsque l'accueil Facebook connecté est visible...")
            return valider_cookies_captures(contexte.cookies())
        finally:
            contexte.close()
            navigateur.close()


def valider_export(chemin: Path) -> str:
    """Lit et valide le fichier d'export brut ; lève ValueError si invalide.

    Retourne le JSON brut recompacté (PAS le JSON normalisé) : c'est ce format
    - celui produit tel quel par l'extension - que `scraper.charger_cookies`
    attend en entrée à l'exécution (voir sa docstring).
    """
    texte_brut = chemin.read_text(encoding="utf-8")
    cookies_normalises = scraper.charger_cookies(texte_brut)  # lève ValueError si invalide

    noms_presents = {c["name"] for c in cookies_normalises}
    if not {"c_user", "xs"}.issubset(noms_presents):
        print(
            "AVERTISSEMENT : cookies 'c_user'/'xs' absents de l'export - "
            "la session sera probablement considérée comme non authentifiée "
            "malgré tout (voir README.md).",
            file=sys.stderr,
        )

    return json.dumps(json.loads(texte_brut), separators=(",", ":"))


def purger_etat_local(compte: str | None = None) -> None:
    """Supprime le cooldown actif et le storage_state en cache localement,
    pour le compte concerné (ou l'état historique mono-compte si `compte`
    est None - voir config.cooldown_path/storage_state_path).

    Sans ça, soit un cooldown encore actif, soit des cookies morts déjà en
    cache, masqueraient les cookies frais qu'on vient de valider ci-dessus.
    """
    for chemin in (config.cooldown_path(compte), config.storage_state_path(compte)):
        if chemin.exists():
            chemin.unlink()
            print(f"État local purgé : {chemin}")


def maj_secret_github(
    repo: str | None,
    cookies_json_compact: str,
    appliquer: bool,
    compte: str | None = None,
) -> None:
    nom_secret = config.nom_secret_cookies(compte)
    if shutil.which("gh") is None:
        print(
            "`gh` (GitHub CLI) introuvable - mets à jour le secret manuellement :\n"
            "  Repo -> Settings -> Secrets and variables -> Actions -> "
            f"{nom_secret}\n"
            "avec le JSON ci-dessous (déjà validé) :\n"
        )
        print(cookies_json_compact)
        return

    commande = ["gh", "secret", "set", nom_secret]
    if repo:
        commande += ["--repo", repo]

    if not appliquer:
        print(
            "Commande prête (relancer avec --set-secret pour l'exécuter réellement) :\n  "
            + " ".join(commande)
        )
        return

    resultat = subprocess.run(commande, input=cookies_json_compact, text=True)
    if resultat.returncode != 0:
        print(
            "Échec de `gh secret set` - mets à jour le secret manuellement.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"Secret {nom_secret} mis à jour.")


def purger_cache_actions(repo: str | None, compte: str | None = None) -> None:
    """Supprime les entrées `actions/cache` préfixées `etat-scraper-` (mono-
    compte) ou `etat-scraper-compte-<compte>-` (voir
    .github/workflows/daily_scraper.yml) - best-effort, jamais bloquant.
    """
    prefixe = f"etat-scraper-compte-{compte}-" if compte else "etat-scraper-"
    if shutil.which("gh") is None:
        print(
            "`gh` introuvable - impossible de purger le cache GitHub Actions "
            f"automatiquement (préfixe '{prefixe}'). Depuis l'onglet Actions "
            "du dépôt : Caches -> supprimer les entrées correspondantes.",
            file=sys.stderr,
        )
        return

    commande = ["gh", "cache", "list", "--json", "id,key"]
    if repo:
        commande += ["--repo", repo]
    resultat = subprocess.run(commande, capture_output=True, text=True)
    if resultat.returncode != 0:
        print("Échec de `gh cache list` - purge du cache Actions ignorée.", file=sys.stderr)
        return

    try:
        caches = json.loads(resultat.stdout or "[]")
    except json.JSONDecodeError:
        caches = []

    for cache in caches:
        cle = str(cache.get("key", ""))
        if not cle.startswith(prefixe):
            continue
        commande_suppr = ["gh", "cache", "delete", str(cache["id"])]
        if repo:
            commande_suppr += ["--repo", repo]
        subprocess.run(commande_suppr)
        print(f"Cache Actions supprimé : {cle}")


def main(argv: list[str] | None = None) -> int:
    parseur = argparse.ArgumentParser(
        description=(
            "Recharge FB_COOKIES_JSON à partir d'un export d'extension de "
            "cookies (Cookie-Editor ou équivalent), sans laisser le pipeline "
            "bloqué à attendre."
        ),
    )
    parseur.add_argument(
        "export",
        type=Path,
        nargs="?",
        help="Fichier JSON exporté par l'extension (omis avec --interactive).",
    )
    parseur.add_argument(
        "--interactive",
        action="store_true",
        help=(
            "Ouvre Chromium visiblement, laisse l'utilisateur se connecter et "
            "capture automatiquement les cookies sans extension d'export."
        ),
    )
    parseur.add_argument(
        "--repo",
        help="owner/repo cible (défaut : dépôt courant détecté par `gh`).",
    )
    parseur.add_argument(
        "--set-secret",
        action="store_true",
        help="Applique réellement `gh secret set` (sinon affiche juste la commande/le JSON).",
    )
    parseur.add_argument(
        "--clear-actions-cache",
        action="store_true",
        help="Supprime aussi le cache GitHub Actions data/state (etat-scraper-*).",
    )
    parseur.add_argument(
        "--no-purge-etat-local",
        action="store_true",
        help="Ne supprime pas le cooldown/storage_state locaux (data/state/).",
    )
    parseur.add_argument(
        "--compte",
        choices=sorted(config.COMPTES_VALIDES),
        default=None,
        help=(
            "Compte Facebook concerné (\"1\"..\"5\", voir README.md section "
            "\"Multi-comptes\") : cible le secret FB_COOKIES_JSON_<compte> et "
            "purge uniquement l'état de CE compte (data/state/compte_<n>/, "
            "cache Actions etat-scraper-compte-<n>-*). Omis (défaut) : secret "
            "FB_COOKIES_JSON historique et état global (mono-compte)."
        ),
    )
    args = parseur.parse_args(argv)

    if args.interactive == (args.export is not None):
        parseur.error("Choisissez exactement une source : --interactive OU un fichier d'export.")

    if args.interactive:
        if not args.set_secret:
            parseur.error(
                "Le mode --interactive exige --set-secret afin de transmettre "
                "la capture sans afficher les cookies dans le terminal."
            )
        if args.set_secret and shutil.which("gh") is None:
            parseur.error(
                "GitHub CLI (`gh`) est requis avec --interactive --set-secret. "
                "Installez-le puis lancez `gh auth login`."
            )
        try:
            cookies_compacts = capturer_session_interactive()
        except ValueError as exc:
            parseur.error(str(exc))
    else:
        assert args.export is not None
        if not args.export.exists():
            parseur.error(f"Fichier introuvable : {args.export}")
        try:
            cookies_compacts = valider_export(args.export)
        except (ValueError, json.JSONDecodeError) as exc:
            parseur.error(f"Export invalide : {exc}")

    maj_secret_github(args.repo, cookies_compacts, appliquer=args.set_secret, compte=args.compte)

    if not args.no_purge_etat_local:
        purger_etat_local(args.compte)

    if args.clear_actions_cache:
        purger_cache_actions(args.repo, args.compte)

    print(
        "\nProchaine étape : relance le workflow manuellement depuis l'onglet "
        "Actions (Run workflow) si tu veux vérifier tout de suite que ça "
        "repasse, plutôt que d'attendre le prochain cron."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
