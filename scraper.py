"""Scraping Facebook (Playwright async) - mode quotidien et rattrapage (backfill).

AVERTISSEMENT - À LIRE AVANT TOUTE EXÉCUTION
---------------------------------------------
Ce module automatise la navigation sur des groupes Facebook avec une session
authentifiée (cookies exportés depuis un compte réel). Cela contrevient aux
Conditions d'Utilisation de Meta, qui interdisent explicitement la collecte
automatisée de données ("scraping"). Risques concrets et non hypothétiques :
  - bannissement/désactivation du compte Facebook utilisé pour les cookies ;
  - blocage de l'IP/du fingerprint utilisé par le runner GitHub Actions ;
  - exposition légale selon la juridiction (CGU contractuelles + réglementation
    locale sur les données personnelles, puisque les posts contiennent des
    numéros de téléphone de tiers).
Ce projet étant présenté comme académique, il est de la responsabilité de
l'utilisateur de : (1) utiliser un compte dédié, pas un compte personnel
principal, (2) ne pas redistribuer les données personnelles collectées,
(3) vérifier la réglementation applicable avant tout usage en production.

CHOIX D'ARCHITECTURE : extraction JSON depuis web.facebook.com (Comet)
----------------------------------------------------------------------------
Historique complet dans config.py (juste au-dessus de `WEB_FACEBOOK_BASE_URL`)
- résumé : le plan initial ciblait mbasic.facebook.com pour son HTML léger
server-rendered, mais un test en conditions réelles le 2026-08-01 (avec un
vrai navigateur, aucune automation) a confirmé que mbasic redirige
désormais systématiquement vers web.facebook.com, qui sert l'application
React "Comet". Contrairement à ce qu'on pourrait croire, ce n'est PAS une
impasse : Comet embarque les données de chaque post en clair dans des blobs
JSON (`<script type="application/json" data-sjs>`, format Relay/GraphQL
interne à Facebook) pour l'hydratation côté client - texte, horodatage Unix
exact, id et permalien y sont tous présents. `extraire_stories_depuis_json`
parcourt ces blobs sans dépendre d'un chemin de clés fixe (structure interne
non documentée, susceptible de changer).

Deux sources de blobs JSON exploitées :
1. Le HTML initial de la page (posts "mis en avant" du groupe - peu nombreux,
   souvent anciens/épinglés).
2. Les réponses réseau GraphQL déclenchées par le scroll (le vrai fil
   principal, chargé dynamiquement - Facebook ne l'inclut plus dans la
   réponse HTML initiale).

STATUT - scroll + capture réseau CONFIRMÉ en conditions réelles (2026-08-03)
----------------------------------------------------------------------------
Le parseur JSON pur (`extraire_stories_depuis_json`) est testé et vérifié
contre plusieurs échantillons RÉELS (structure confirmée, pas une
supposition). La partie scroll + capture réseau GraphQL (`scraper_groupe`),
elle, n'avait jamais pu être testée en conditions réelles depuis mon
environnement de développement (aucun accès réseau à facebook.com) - c'est
désormais chose faite : le run CI du 2026-08-03 (`--group-limit 1`,
`--days-back 7`, avec `seen_ids` vidé pour le test) a collecté 181 posts sur
un seul groupe (60 étapes de scroll, garde-fou `MAX_PAGES_ABSOLU` atteint -
attendu vu `seen_ids` vide, un run quotidien normal s'arrêtera bien avant),
dont 26 se sont révélées être des annonces foncières valides après filtrage
regex + structuration OpenAI. Le mécanisme fonctionne donc à l'échelle, pas
seulement sur un post isolé. Reste non garanti sur la durée : la stabilité
du motif d'URL GraphQL (`config.GRAPHQL_URL_FRAGMENTS`) si Facebook fait
évoluer son API interne (non documentée publiquement, changement possible
sans préavis) - à surveiller sur les prochains runs quotidiens.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

import config

logger = logging.getLogger("ouaga_foncier_etl.scraper")


class SessionExpireeError(Exception):
    """Levée quand les cookies Facebook ne sont plus valides (mur de connexion)."""


class BlocageDetecteError(Exception):
    """Levée quand Facebook affiche un mur anti-bot (checkpoint/captcha)."""


class CooldownActifError(Exception):
    """Levée en tout début de run si un cooldown anti-blocage est encore actif.

    Ce n'est PAS une erreur au sens habituel : c'est le mécanisme qui empêche
    de relancer un scraping immédiatement après un blocage détecté, ce qui
    serait le signal comportemental le plus voyant pour Facebook.
    """


# --------------------------------------------------------------------------- #
# Sélecteurs DOM (fragiles - voir avertissement en tête de fichier)
# --------------------------------------------------------------------------- #

SELECTEURS = {
    # Indicateurs de mur de connexion / checkpoint. INCERTITUDE ASSUMÉE : ce
    # sélecteur (`input[name="pass"], #login_form`) visait à l'origine la
    # page de login mbasic - non revérifié pour la page de login de
    # web.facebook.com/Comet, qui pourrait utiliser une structure différente.
    # `checkpoint_url_fragments` reste fiable (basé sur l'URL, pas le DOM).
    "mur_connexion": 'input[name="pass"], #login_form',
    "checkpoint_url_fragments": ["checkpoint", "login.php", "recover"],
}

MOTS_CHECKPOINT_TEXTE = [
    "nous voulons juste vérifier",
    "confirmez votre identité",
    "action inhabituelle",
    "we just want to make sure",
]


# --------------------------------------------------------------------------- #
# Authentification / contexte navigateur
# --------------------------------------------------------------------------- #


# BUG CORRIGÉ (trouvé en recevant un vrai export de cookies) : la docstring
# d'origine annonçait un format "compatible Playwright" (`name`, `value`,
# `domain`, `path`) mais AUCUN outil grand public n'exporte les cookies dans
# ce format-là. Les extensions de navigateur courantes (Cookie-Editor et
# équivalents, format `chrome.cookies`) exportent `expirationDate` (epoch
# flottant) au lieu de `expires`, `sameSite` en minuscules avec des valeurs
# ("no_restriction", "unspecified") que Playwright n'accepte pas telles
# quelles, et des clés que Playwright ne reconnaît pas du tout (`hostOnly`,
# `storeId`, `session`). Sans conversion, `context.add_cookies()` aurait donc
# échoué (ou au mieux ignoré silencieusement `sameSite`) sur un export réel -
# jamais détecté avant faute d'avoir reçu un vrai export pendant le
# développement. `_normaliser_cookie` fait cette conversion.
_SAMESITE_VERS_PLAYWRIGHT = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",  # convention de l'API chrome.cookies (extensions d'export)
    # "unspecified" = cookie sans attribut SameSite explicite. Les navigateurs
    # modernes (Chrome >= 80) appliquent Lax par défaut dans ce cas - c'est
    # l'hypothèse retenue ici, à confirmer si un cookie précis pose problème.
    "unspecified": "Lax",
}


def _normaliser_cookie(cookie: dict[str, Any]) -> dict[str, Any]:
    """Convertit un cookie vers le format strict attendu par Playwright
    (`SetCookieParam` : name, value, domain, path, expires, httpOnly, secure,
    sameSite ∈ {"Strict","Lax","None"}), à partir soit du format déjà
    Playwright, soit d'un export brut d'extension de navigateur.
    """
    converti: dict[str, Any] = {
        "name": cookie["name"],
        "value": cookie["value"],
        "domain": cookie["domain"],
        "path": cookie.get("path") or "/",
    }
    if cookie.get("httpOnly") is not None:
        converti["httpOnly"] = bool(cookie["httpOnly"])
    if cookie.get("secure") is not None:
        converti["secure"] = bool(cookie["secure"])

    # "expires" (format Playwright natif) a priorité sur "expirationDate"
    # (format extension) si les deux sont présents.
    expiration = cookie.get("expires", cookie.get("expirationDate"))
    if expiration is not None and not cookie.get("session"):
        converti["expires"] = float(expiration)
    # Pas de date d'expiration (ou cookie marqué "session") : on omet
    # "expires" plutôt que d'inventer une valeur - traité comme cookie de
    # session par le navigateur, ce qui est le comportement correct ici.

    same_site_brut = cookie.get("sameSite")
    if same_site_brut:
        same_site_normalise = _SAMESITE_VERS_PLAYWRIGHT.get(str(same_site_brut).lower())
        if same_site_normalise:
            converti["sameSite"] = same_site_normalise
        else:
            logger.warning(
                "Cookie '%s' : valeur sameSite '%s' non reconnue, ignorée.",
                cookie.get("name"),
                same_site_brut,
            )
        # Valeur non reconnue : on omet plutôt que d'envoyer à Playwright une
        # valeur hors de l'enum Strict/Lax/None qu'il rejetterait.

    return converti


def charger_cookies(cookies_json: str) -> list[dict[str, Any]]:
    """Parse, valide et normalise le contenu de la variable d'environnement
    FB_COOKIES_JSON.

    Accepte deux formats en entrée : le format Playwright natif
    (`{"name", "value", "domain", "path", ...}`) et le format brut exporté par
    les extensions de navigateur usuelles (`{"expirationDate", "hostOnly",
    "sameSite": "no_restriction", ...}`) - voir `_normaliser_cookie`. Le
    résultat retourné est toujours au format Playwright.

    Raises:
        ValueError: JSON invalide ou structure inattendue.
    """
    try:
        cookies_bruts = json.loads(cookies_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"FB_COOKIES_JSON n'est pas un JSON valide : {exc}") from exc

    if not isinstance(cookies_bruts, list) or not cookies_bruts:
        raise ValueError("FB_COOKIES_JSON doit être une liste non vide de cookies.")

    champs_requis = {"name", "value", "domain"}
    for i, cookie in enumerate(cookies_bruts):
        if not isinstance(cookie, dict) or not champs_requis.issubset(cookie):
            raise ValueError(
                f"Cookie #{i} invalide : champs requis {champs_requis} manquants."
            )

    cookies = [_normaliser_cookie(c) for c in cookies_bruts]

    noms_presents = {c["name"] for c in cookies}
    if "c_user" not in noms_presents or "xs" not in noms_presents:
        logger.warning(
            "Cookies 'c_user'/'xs' absents - la session sera probablement "
            "considérée comme non authentifiée par Facebook."
        )

    return cookies


def _charger_origins_sauvegardees(compte: str | None = None) -> list[dict[str, Any]]:
    """Récupère le localStorage sauvegardé d'un run précédent (voir
    `sauvegarder_storage_state`), pour que le navigateur ressemble à un appareil
    qui revient plutôt qu'à un navigateur vierge à chaque exécution.

    `compte` isole ce cache par compte Facebook - voir config.storage_state_path.
    """
    chemin = config.storage_state_path(compte)
    if not chemin.exists():
        return []
    try:
        with chemin.open(encoding="utf-8") as f:
            return json.load(f).get("origins", [])
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "storage_state.json illisible (%s) - repart sans localStorage sauvegardé.",
            exc,
        )
        return []


def _charger_cookies_caches(compte: str | None = None) -> list[dict[str, Any]] | None:
    """Cookies sauvegardés à la fin du run précédent (voir
    `sauvegarder_storage_state`), à réutiliser en PRIORITÉ sur les cookies
    statiques du secret FB_COOKIES_JSON.

    RAISON D'ÊTRE (correction du 2026-08-16) : jusqu'ici, le code rechargeait
    TOUJOURS les cookies figés de FB_COOKIES_JSON à chaque run, même après un
    run réussi qui avait potentiellement obtenu des cookies renouvelés par
    Facebook en cours de session (comportement standard : un navigateur qui
    reste connecté voit ses cookies de session tourner/se rafraîchir au fil de
    la navigation). Ignorer ce renouvellement en réappliquant systématiquement
    l'ancien secret revenait à reculer volontairement d'une session à chaque
    run, ce qui RAPPROCHE l'expiration au lieu de l'éloigner - un vrai
    navigateur ne redemande jamais les identifiants tant que la session en
    cours est valide, on reproduit ce comportement ici.

    Retourne None (pas une liste vide) si le cache est absent, illisible, ou
    manifestement invalide (cookies d'authentification absents ou expirés) -
    l'appelant doit alors se rabattre sur FB_COOKIES_JSON. Ce repli garantit
    qu'une régénération manuelle du secret (après une vraie expiration) est
    bien prise en compte au run suivant plutôt que masquée par un vieux cache.
    """
    chemin = config.storage_state_path(compte)
    if not chemin.exists():
        return None
    try:
        with chemin.open(encoding="utf-8") as f:
            cookies = json.load(f).get("cookies", [])
    except (OSError, json.JSONDecodeError) as exc:
        logger.info(
            "storage_state.json illisible (%s) - repli sur FB_COOKIES_JSON.", exc
        )
        return None

    if not cookies:
        return None

    noms_presents = {c.get("name") for c in cookies}
    if "c_user" not in noms_presents or "xs" not in noms_presents:
        logger.info(
            "Cookies sauvegardés incomplets (c_user/xs absents) - "
            "repli sur FB_COOKIES_JSON."
        )
        return None

    # Format Playwright natif pour storage_state() : "expires" en epoch
    # secondes, ou -1 pour un cookie de session (pas de date fixe - à ne PAS
    # traiter comme "expiré", contrairement à une date passée classique).
    maintenant = datetime.now(timezone.utc).timestamp()
    for c in cookies:
        expiration = c.get("expires")
        if expiration is not None and expiration != -1 and expiration < maintenant:
            logger.info(
                "Cookie '%s' du cache expiré (%.0f < maintenant) - "
                "repli sur FB_COOKIES_JSON.",
                c.get("name"),
                expiration,
            )
            return None

    logger.info(
        "Réutilisation de %d cookie(s) sauvegardés du run précédent (session "
        "potentiellement déjà renouvelée par Facebook depuis FB_COOKIES_JSON).",
        len(cookies),
    )
    return cookies


async def sauvegarder_storage_state(
    contexte: BrowserContext, compte: str | None = None
) -> None:
    """Sauvegarde cookies + localStorage en fin de run pour la prochaine exécution.

    Note CI : ce fichier vit dans data/state/, qui n'est PAS versionné (voir
    .gitignore) - en GitHub Actions, sa persistance entre deux runs dépend d'un
    cache explicite (voir .github/workflows/daily_scraper.yml). Sans ce cache,
    chaque run repart d'un navigateur "neuf" et cette fonction ne sert à rien.
    `compte` isole ce fichier par compte Facebook (voir config.storage_state_path).
    """
    try:
        chemin = config.storage_state_path(compte)
        chemin.parent.mkdir(parents=True, exist_ok=True)
        await contexte.storage_state(path=str(chemin))
    except Exception:
        logger.exception("Échec de sauvegarde du storage_state (non bloquant).")


def invalider_storage_state(compte: str | None = None) -> None:
    """Supprime le storage_state mis en cache (cookies + localStorage) - à
    appeler quand on SAIT que les cookies qu'il contient sont morts (session
    expirée détectée sur ce run).

    BUG CORRIGÉ : sans ça, sauvegarder_storage_state réenregistrait les
    cookies morts après une SessionExpireeError, et _charger_cookies_caches
    donne PRIORITÉ à ce cache sur FB_COOKIES_JSON au run suivant - un cookie
    invalidé côté serveur n'a pas forcément de date dépassée, donc le test de
    fraîcheur ne le détecte pas. Résultat : régénérer FB_COOKIES_JSON à la
    main (nouvel export Cookie-Editor) après une expiration ne servait à
    rien, le run suivant rechargeait le cache mort en boucle. En supprimant
    ce cache dès qu'une expiration est détectée, le run suivant retombe sur
    FB_COOKIES_JSON, donc sur les cookies fraîchement fournis.
    """
    try:
        config.storage_state_path(compte).unlink(missing_ok=True)
    except Exception:
        logger.exception("Échec de suppression du storage_state invalide (non bloquant).")


async def creer_navigateur(
    playwright,
    cookies: list[dict[str, Any]],
    compte: str | None = None,
    proxy: dict[str, str] | None = None,
) -> tuple[Browser, BrowserContext]:
    """Lance Chromium headless et prépare une session aussi cohérente que possible
    d'un run à l'autre (cookies + localStorage réutilisé si disponible).

    `compte` sélectionne le localStorage sauvegardé DU BON compte (voir
    _charger_origins_sauvegardees) - sans ça, un run pour le compte 2
    réutiliserait par erreur le fingerprint/localStorage du compte 1.

    `proxy` (voir config.proxy_playwright) fait sortir TOUTE la session par ce
    proxy plutôt que par l'IP du runner - None (par défaut) préserve le
    comportement historique (pas de proxy).

    Limite assumée : aucune de ces mesures ne compense une mauvaise réputation
    d'IP/ASN (voir README.md) - c'est un plafond bas, pas une garantie. Un
    proxy résidentiel/mobile (voir `proxy` ci-dessus) réduit ce risque
    précis, sans non plus le supprimer entièrement (le proxy lui-même a sa
    propre réputation, pas forcément parfaite).
    """
    navigateur = await playwright.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
        proxy=proxy,
    )
    contexte = await navigateur.new_context(
    # Viewport desktop : cohérent avec le User-Agent Chrome/Windows utilisé
    # ci-dessous (config.MBASIC_USER_AGENT, malgré son nom historique, est
    # en réalité un UA Chrome desktop standard depuis le passage à Comet -
    # voir historique dans config.py). Un viewport mobile (360x640) combiné
    # à un UA desktop était un signal incohérent facilement détectable par
    # Facebook, corrigé le 2026-08-06.
    viewport={"width": 1366, "height": 900},
    locale="fr-FR",
    timezone_id="Africa/Ouagadougou",
    user_agent=config.MBASIC_USER_AGENT,
    storage_state={"cookies": [], "origins": _charger_origins_sauvegardees(compte)},
)
    # Masque le flag standard qui trahit un navigateur piloté par automation.
    # Patch minimal et documenté publiquement (pas une suite de contournement) -
    # voir README.md pour ce qui n'est délibérément PAS fait au-delà de ça.
    await contexte.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )
    await contexte.add_cookies(cookies)
    contexte.set_default_navigation_timeout(config.NAVIGATION_TIMEOUT_MS)
    return navigateur, contexte


async def echauffement(contexte: BrowserContext) -> None:
    """Navigation de "mise en jambe" avant d'attaquer les groupes : ouvre le fil
    d'actualité général plutôt que de foncer droit sur une URL de groupe dès la
    première requête de la session. Best-effort : un échec ici ne doit jamais
    arrêter le run (c'est une amélioration comportementale, pas une étape
    critique).
    """
    page = await contexte.new_page()
    try:
        await page.goto(f"{config.WEB_FACEBOOK_BASE_URL}/", wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(3.0, 7.0))
    except Exception as exc:
        logger.debug("Échauffement ignoré (non bloquant) : %s", exc)
    finally:
        await page.close()


# --------------------------------------------------------------------------- #
# Détection anti-bot / session expirée
# --------------------------------------------------------------------------- #


async def detecter_blocage_ou_session_expiree(page: Page) -> None:
    """Vérifie l'URL et le contenu visible pour détecter un mur anti-bot ou une
    session expirée. Lève une exception dédiée dans les deux cas, pour que
    l'appelant puisse arrêter proprement plutôt que de scraper une page d'erreur.
    """
    url = page.url.lower()
    if any(fragment in url for fragment in SELECTEURS["checkpoint_url_fragments"]):
        raise BlocageDetecteError(f"URL de checkpoint/connexion détectée : {page.url}")

    try:
        contenu = (await page.content()).lower()
    except Exception:  # page déjà fermée, navigation en cours, etc.
        return

    if any(mot in contenu for mot in MOTS_CHECKPOINT_TEXTE):
        raise BlocageDetecteError("Texte de vérification anti-bot détecté sur la page.")

    # BUG RÉEL rencontré le 2026-08-01 (run 30715788089) : aucun des checks
    # ci-dessus ne détecte une session Comet simplement déconnectée (pas de
    # redirection d'URL, pas de texte de vérification, pas de formulaire
    # `#login_form`/`input[name="pass"]` - ce sélecteur visait mbasic et ne
    # matche pas la page "déconnectée" de web.facebook.com/Comet). Résultat :
    # 5 groupes revenus à 0 post sans qu'aucune erreur ne soit levée, cooldown
    # anti-blocage jamais déclenché. Confirmé en analysant le dump HTML réel :
    # la page contenait `"USER_ID":"0"` et `"actorID":"0"` (identifiants
    # Facebook internes de l'utilisateur anonyme/déconnecté - un vrai user
    # connecté a un identifiant numérique réel), ainsi que de vrais liens
    # "Se connecter" vers `/login/`. Marqueur fiable et spécifique (peu de
    # risque de faux positif contrairement à un mot générique).
    if '"user_id":"0"' in contenu or '"actorid":"0"' in contenu:
        raise SessionExpireeError(
            "Session Comet déconnectée détectée (USER_ID/actorID à 0) - "
            "cookies probablement invalidés côté serveur Facebook."
        )

    if await page.locator(SELECTEURS["mur_connexion"]).count() > 0:
        raise SessionExpireeError(
            "Mur de connexion détecté - cookies probablement expirés."
        )


# --------------------------------------------------------------------------- #
# Extraction des posts depuis les blobs JSON Comet (voir avertissement en
# tête de module + historique dans config.py). Fonctions PURES et testables
# sans navigateur - contrairement à la navigation/au scroll qui les entoure.
# --------------------------------------------------------------------------- #

# Clés observées sur DEUX échantillons réels distincts (2026-08-01) - la
# structure diffère selon la provenance du post, pas seulement selon la
# version de Comet :
#   1. Post "mis en avant" (`highlight_units.edges[i].node`) : id + url +
#      comet_sections co-existent directement sur le même objet.
#   2. Post du fil normal (`group_feed.edges[i].node`, capturé via une
#      réponse GraphQL de scroll) : l'objet "node" porte id + creation_time,
#      mais le texte/comet_sections réel vit un niveau plus bas, dans un
#      sous-objet `attached_story` (post partagé/attaché) qui a SA PROPRE
#      clé `id` et `comet_sections` - mais PAS de `url` à ce niveau, celle-ci
#      étant encore plus profondément imbriquée (`comet_sections.
#      context_layout.story.comet_sections.metadata[i].story.url`).
# Conclusion : exiger `url` sur le MÊME objet que `id`+`comet_sections` (comme
# avant) rate systématiquement le cas 2, qui est pourtant le cas le plus
# fréquent en usage réel (fil normal vs. les quelques posts épinglés). On ne
# garde donc que `id` + `comet_sections` comme signature d'identification, et
# `url` est recherché séparément par motif (`_extraire_url_story`), comme le
# texte et la date - le risque de faux positif reste faible car un post n'est
# retenu que si un texte réel est aussi trouvé (voir `extraire_stories_depuis_json`).
_CLES_STORY_REQUISES = ("id", "comet_sections")


def _est_noeud_story(obj: Any) -> bool:
    """Heuristique de détection d'un objet "story" (post) dans un payload
    JSON Comet - structure interne non documentée publiquement et non
    garantie stable, donc détectée par la PRÉSENCE d'un jeu de clés typique
    plutôt que par un chemin de clés fixe (voir `_CLES_STORY_REQUISES`).
    """
    return (
        isinstance(obj, dict)
        and isinstance(obj.get("id"), str)
        and all(cle in obj for cle in _CLES_STORY_REQUISES)
    )


def _chercher_valeur_imbriquee(
    obj: Any, predicat: Callable[[Any], bool], profondeur_max: int = config.JSON_PROFONDEUR_MAX
) -> Any | None:
    """Parcours en largeur (BFS) d'un JSON imbriqué à la recherche de la
    première valeur satisfaisant `predicat`, borné par `profondeur_max`
    (protection contre un coût CPU excessif sur un payload volumineux).
    """
    file_attente: deque[tuple[Any, int]] = deque([(obj, 0)])
    while file_attente:
        courant, profondeur = file_attente.popleft()
        if profondeur > profondeur_max:
            continue
        if predicat(courant):
            return courant
        if isinstance(courant, dict):
            for v in courant.values():
                file_attente.append((v, profondeur + 1))
        elif isinstance(courant, list):
            for v in courant:
                file_attente.append((v, profondeur + 1))
    return None


def _extraire_texte_story(story: dict[str, Any]) -> str | None:
    """Cherche le texte du post à l'intérieur d'un nœud story (voir
    `_est_noeud_story`). Sur l'échantillon réel observé, ce texte vit sous
    `comet_sections.content.story.comet_sections.message_container.story.
    message.text` - chemin non codé en dur ici car probablement instable,
    d'où la recherche par motif (`{"message": {"text": "..."}}`) plutôt que
    par chemin exact.
    """
    def _est_message_texte(v: Any) -> bool:
        return (
            isinstance(v, dict)
            and isinstance(v.get("message"), dict)
            and isinstance(v["message"].get("text"), str)
            and bool(v["message"]["text"].strip())
        )

    trouve = _chercher_valeur_imbriquee(story, _est_message_texte)
    return trouve["message"]["text"] if trouve else None


def _extraire_url_story(story: dict[str, Any]) -> str | None:
    """Cherche le permalien du post à l'intérieur d'un nœud story. Sur
    l'échantillon "fil normal" observé, ce champ vit sous
    `attached_story.comet_sections.context_layout.story.comet_sections.
    metadata[i].story.url` - trop profond et instable pour un chemin figé,
    d'où la recherche par motif comme pour le texte et la date.

    On ne se contente pas de vérifier `isinstance(v, str)` : un objet story
    contient aussi des URLs de profil d'auteur (`"url":
    "https://www.facebook.com/<pseudo>"`), rencontrées AVANT le vrai
    permalien lors d'un parcours en largeur - les retenir par erreur
    casserait le dédoublonnage par id. Un vrai permalien de post Facebook
    contient toujours `/posts/` ou `/permalink/` (confirmé sur les deux
    échantillons réels analysés), ce qui les distingue de façon fiable.
    """
    def _est_url_post(v: Any) -> bool:
        return (
            isinstance(v, dict)
            and isinstance(v.get("url"), str)
            and ("/posts/" in v["url"] or "/permalink/" in v["url"])
        )

    trouve = _chercher_valeur_imbriquee(story, _est_url_post)
    return trouve["url"] if trouve else None


def _extraire_creation_time_story(story: dict[str, Any]) -> datetime | None:
    """Cherche l'horodatage Unix du post à l'intérieur d'un nœud story. Sur
    l'échantillon réel observé, ce champ vit sous `comet_sections.
    context_layout.story.comet_sections.metadata[0].story.creation_time` -
    recherche par motif pour la même raison que `_extraire_texte_story`.
    Contrairement à l'horodatage textuel relatif de mbasic
    (`_parser_horodatage_relatif`), c'est un timestamp Unix exact - aucune
    ambiguïté à résoudre, mais toujours pas de valeur inventée si absent.
    """
    def _est_creation_time(v: Any) -> bool:
        return (
            isinstance(v, dict)
            and isinstance(v.get("creation_time"), (int, float))
            and not isinstance(v.get("creation_time"), bool)
            and v["creation_time"] > 1_000_000_000  # grandeur d'un timestamp Unix plausible
        )

    trouve = _chercher_valeur_imbriquee(story, _est_creation_time)
    if not trouve:
        return None
    try:
        return datetime.fromtimestamp(trouve["creation_time"], tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def extraire_stories_depuis_json(
    payload: Any, groupe_id: str, groupe_nom: str
) -> list[dict[str, Any]]:
    """Parcourt un payload JSON Comet (blob SSR initial ou réponse GraphQL
    capturée pendant le scroll) et en extrait tous les posts identifiables.

    Une fois qu'un nœud story est identifié et qu'un texte a pu en être
    extrait, on ne descend PAS plus profondément dans son sous-arbre - évite
    de compter deux fois le même post si un nœud story imbriqué à
    l'intérieur (ex: dans `comet_sections.content.story`) matche lui aussi
    l'heuristique `_est_noeud_story`.

    Best-effort par construction : un post au format inattendu (texte
    introuvable, JSON malformé localement) est silencieusement ignoré plutôt
    que de faire échouer tout le parcours - cohérent avec le reste du
    pipeline (aucun post individuel ne doit pouvoir bloquer un run entier).
    """
    posts: list[dict[str, Any]] = []
    vus: set[str] = set()
    maintenant = datetime.now(timezone.utc)

    def _parcourir(obj: Any) -> None:
        if isinstance(obj, dict):
            if _est_noeud_story(obj) and obj["id"] not in vus:
                texte = _extraire_texte_story(obj)
                if texte:
                    vus.add(obj["id"])
                    horodatage = _extraire_creation_time_story(obj)
                    posts.append(
                        {
                            "id": obj["id"],
                            "groupe_id": groupe_id,
                            "groupe_nom": groupe_nom,
                            "url": _extraire_url_story(obj),
                            "texte": texte,
                            "date_publication": horodatage.isoformat() if horodatage else None,
                            "date_incertaine": horodatage is None,
                            "scrape_le": maintenant.isoformat(),
                        }
                    )
                    return  # pas de descente dans le sous-arbre déjà capturé
            for v in obj.values():
                _parcourir(v)
        elif isinstance(obj, list):
            for v in obj:
                _parcourir(v)

    try:
        _parcourir(payload)
    except RecursionError:
        logger.warning(
            "Profondeur de récursion dépassée en parcourant un blob JSON du groupe %s "
            "- extraction partielle conservée (%d post(s) trouvé(s) avant l'échec).",
            groupe_id, len(posts),
        )

    return posts


def _extraire_stories_depuis_scripts_json(
    html: str, groupe_id: str, groupe_nom: str
) -> list[dict[str, Any]]:
    """Parcourt tous les blocs `<script type="application/json">` d'une page
    HTML Comet et en extrait les posts (voir `extraire_stories_depuis_json`).

    Chaque blob est indépendant et peut échouer sans affecter les autres -
    la page contient couramment 300+ balises script, la plupart sans rapport
    avec des posts (config, tracking, autres widgets).
    """
    posts: list[dict[str, Any]] = []
    vus: set[str] = set()
    for blob in re.findall(
        r'<script type="application/json"[^>]*>(.*?)</script>', html, re.DOTALL
    ):
        try:
            payload = json.loads(blob)
        except json.JSONDecodeError:
            continue
        for post in extraire_stories_depuis_json(payload, groupe_id, groupe_nom):
            if post["id"] not in vus:
                vus.add(post["id"])
                posts.append(post)
    return posts


# --------------------------------------------------------------------------- #
# CODE HÉRITÉ, PLUS UTILISÉ PAR LE CHEMIN ACTIF (voir historique en tête de
# fichier) : parsing des horodatages textuels relatifs de mbasic ("il y a 3
# h", "Hier à 14:30"). Devenu inutile depuis le passage à l'extraction JSON
# Comet, qui fournit un timestamp Unix exact (`_extraire_creation_time_story`)
# - plus besoin de deviner une date approximative depuis du texte affiché.
# Conservé (fonction pure, 15 tests existants, aucun coût de maintenance)
# uniquement au cas où un retour à du HTML léger redeviendrait pertinent.
# --------------------------------------------------------------------------- #

_MOIS_FR = {
    "janvier": 1,
    "février": 2,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "août": 8,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
    "decembre": 12,
}
_MOTIF_MOIS_FR = "|".join(_MOIS_FR)


def _parser_horodatage_relatif(
    texte: str | None, maintenant: datetime
) -> datetime | None:
    """Convertit le texte d'horodatage affiché par mbasic (français, locale du
    contexte navigateur fixée à fr-FR) en `datetime` absolu.

    Fonction PURE et entièrement testable sans navigateur, contrairement à
    l'ancienne extraction qui dépendait d'un DOM live jamais accessible en
    test. Formats couverts (best-effort, à valider en conditions réelles -
    voir avertissement en tête de fichier) : "X min", "X h", "X j",
    "Hier [à HH:MM]", "Aujourd'hui [à HH:MM]", "D mois [AAAA] [à HH:MM]".
    Tout format non reconnu retourne None plutôt que de deviner - le post est
    alors conservé avec `date_incertaine=True` (jamais supprimé silencieusement).
    """
    if not texte:
        return None
    t = texte.strip().lower()

    if t in ("à l'instant", "a l'instant", "just now", "il y a quelques secondes"):
        return maintenant

    m = re.fullmatch(r"(\d+)\s*min", t)
    if m:
        return maintenant - timedelta(minutes=int(m.group(1)))

    m = re.fullmatch(r"(\d+)\s*h", t)
    if m:
        return maintenant - timedelta(hours=int(m.group(1)))

    m = re.fullmatch(r"(\d+)\s*j", t)
    if m:
        return maintenant - timedelta(days=int(m.group(1)))

    m = re.fullmatch(r"hier(?:\s+[àa]\s+(\d{1,2})[:h](\d{2}))?", t)
    if m:
        heure = int(m.group(1)) if m.group(1) else 0
        minute = int(m.group(2)) if m.group(2) else 0
        veille = maintenant - timedelta(days=1)
        return veille.replace(hour=heure, minute=minute, second=0, microsecond=0)

    m = re.fullmatch(r"aujourd'?hui(?:\s+[àa]\s+(\d{1,2})[:h](\d{2}))?", t)
    if m:
        heure = int(m.group(1)) if m.group(1) else 0
        minute = int(m.group(2)) if m.group(2) else 0
        return maintenant.replace(hour=heure, minute=minute, second=0, microsecond=0)

    m = re.fullmatch(
        r"(\d{1,2})\s+("
        + _MOTIF_MOIS_FR
        + r")(?:\s+(\d{4}))?(?:\s+[àa]\s+(\d{1,2})[:h](\d{2}))?",
        t,
    )
    if m:
        jour_n = int(m.group(1))
        mois_n = _MOIS_FR[m.group(2)]
        annee = int(m.group(3)) if m.group(3) else maintenant.year
        # Heure inconnue -> midi par convention (précision réduite au jour près,
        # suffisant pour un filtre `days_back`, jamais pour un tri fin).
        heure = int(m.group(4)) if m.group(4) else 12
        minute = int(m.group(5)) if m.group(5) else 0
        try:
            candidat = maintenant.replace(
                year=annee,
                month=mois_n,
                day=jour_n,
                hour=heure,
                minute=minute,
                second=0,
                microsecond=0,
            )
        except ValueError:
            return None  # date invalide (ex: 31 février) - on ne devine pas
        # Sans année explicite, une date qui tombe dans le futur signifie
        # presque certainement l'année précédente ("1 août" affiché en janvier).
        if not m.group(3) and candidat > maintenant:
            candidat = candidat.replace(year=annee - 1)
        return candidat

    return None


def _sauvegarder_echantillons_graphql_debug(
    echantillons: list[dict[str, Any]], groupe_id: str
) -> Path | None:
    """Sauvegarde quelques réponses GraphQL brutes (URL + corps tronqué) quand
    un groupe a reçu des réponses matchant `GRAPHQL_URL_FRAGMENTS` pendant le
    scroll mais que `extraire_stories_depuis_json` n'y a trouvé AUCUN post.

    Sert à trancher entre les deux causes possibles sans avoir à deviner :
    soit ce sont bien de vraies réponses "fil du groupe" mais dont la
    structure interne a changé (le parseur ne les reconnaît plus), soit ce
    sont des réponses GraphQL d'un autre type (notifications, chat,
    suggestions...) qui matchent le même fragment d'URL par coïncidence sans
    jamais avoir contenu de post. Fichier écrit dans data/logs/ (jamais
    commité, inclus dans l'artefact du run CI - voir daily_scraper.yml).

    Best-effort : une erreur d'écriture ne doit jamais faire échouer le run.
    """
    try:
        horodatage = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        chemin = config.LOG_DIR / f"debug_scroll_vide_{groupe_id}_{horodatage}.json"
        chemin.parent.mkdir(parents=True, exist_ok=True)
        with chemin.open("w", encoding="utf-8") as f:
            json.dump(echantillons, f, ensure_ascii=False, indent=2)
        logger.warning(
            "Groupe %s : scroll terminé sans AUCUN post trouvé alors que %d réponse(s) "
            "GraphQL ont matché - %d échantillon(s) brut(s) sauvegardé(s) -> %s. "
            "À inspecter : soit ce sont de vraies réponses \"fil du groupe\" dont la "
            "structure JSON a changé, soit ce sont des réponses GraphQL d'un autre type "
            "(notifications, chat...) qui matchent le même fragment d'URL par coïncidence.",
            groupe_id, len(echantillons), len(echantillons), chemin,
        )
        return chemin
    except Exception as exc:  # ne doit jamais interrompre le scraping
        logger.debug("Échec sauvegarde des échantillons GraphQL de debug : %s", exc)
        return None


async def _sauvegarder_html_debug(page: Page, groupe_id: str) -> Path | None:
    """Sauvegarde le HTML brut de la page courante quand aucun post "mis en
    avant" n'est trouvé dans le HTML initial d'un groupe.

    Sert à diagnostiquer un échec d'extraction JSON sans session live pour
    inspecter manuellement (voir `extraire_stories_depuis_json`). Fichier
    écrit dans data/logs/ (jamais commité - voir .gitignore ; inclus dans
    l'artefact du run CI - voir daily_scraper.yml, `data/logs/debug_page_vide_*.html`).

    INCERTITUDE LEVÉE le 2026-08-03 : ce cas n'implique PAS systématiquement
    un problème. Sur un run à 14 groupes, 3 groupes ont déclenché ce
    diagnostic (0 post "mis en avant") mais ont ensuite trouvé 78 à 179
    posts via le scroll - `highlight_units` peut légitimement être vide pour
    un groupe qui n'a simplement aucun post épinglé. Seul un groupe
    réellement vrai échec (0 post après le scroll aussi) mérite une
    inspection du HTML.

    Best-effort : une erreur d'écriture ne doit jamais faire échouer le run.
    """
    try:
        horodatage = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        chemin = config.LOG_DIR / f"debug_page_vide_{groupe_id}_{horodatage}.html"
        chemin.parent.mkdir(parents=True, exist_ok=True)
        contenu = await page.content()
        chemin.write_text(contenu, encoding="utf-8")
        logger.info(
            "0 post \"mis en avant\" pour le groupe %s (HTML sauvegardé -> %s, "
            "au cas où - voir data/logs/). Peut être normal si le groupe n'a "
            "aucun post épinglé : le scroll va quand même chercher le fil "
            "normal. Si le run se termine ENCORE à 0 post pour ce groupe "
            "après le scroll, inspectez le fichier (bloc <script "
            "type=\"application/json\"> contenant un texte de post connu, "
            "pour vérifier si la structure JSON Comet a changé).",
            groupe_id, chemin,
        )
        return chemin
    except Exception as exc:  # ne doit jamais interrompre le scraping
        logger.debug("Échec sauvegarde HTML de debug : %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Persistance incrémentale / déduplication
# --------------------------------------------------------------------------- #


def charger_seen_ids() -> dict[str, str]:
    """Charge {post_id: date_iso_vu} pour dédupliquer entre exécutions."""
    if not config.SEEN_IDS_PATH.exists():
        return {}
    try:
        with config.SEEN_IDS_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Impossible de lire %s (%s) - repart d'un état vide.",
            config.SEEN_IDS_PATH,
            exc,
        )
        return {}


def sauvegarder_seen_ids(seen: dict[str, str], retention_jours: int = 90) -> None:
    """Sauvegarde l'état de déduplication, en purgeant les entrées trop anciennes
    pour éviter une croissance illimitée du fichier au fil des runs quotidiens.
    """
    seuil = datetime.now(timezone.utc) - timedelta(days=retention_jours)
    purge: dict[str, str] = {}
    for post_id, date_str in seen.items():
        try:
            if datetime.fromisoformat(date_str) >= seuil:
                purge[post_id] = date_str
        except ValueError:
            purge[post_id] = date_str  # date illisible : on garde par précaution

    with config.SEEN_IDS_PATH.open("w", encoding="utf-8") as f:
        json.dump(purge, f, ensure_ascii=False, indent=2)


def charger_dernier_post_connu(compte: str | None = None) -> dict[str, str]:
    """Charge {groupe_id: post_id} du post le plus récent connu pour chaque
    groupe, tel qu'établi à la fin du run précédent - voir
    `config.dernier_post_connu_path`. Sert de repère d'arrêt du scroll dans
    `scraper_groupe` : dès qu'on le retrouve en scrollant depuis le haut, on
    sait qu'on a rattrapé tout ce qui a été publié depuis la dernière visite.
    `compte` isole ce repère par compte (chaque compte ne traite de toute
    façon que ses propres groupes, voir config.charger_groupes(compte=...),
    mais l'isolation évite tout mélange si un groupe changeait un jour
    d'attribution entre deux comptes).

    Fichier absent/corrompu -> dict vide (pas de repère connu), traité comme
    un premier run sur chaque groupe concerné - jamais bloquant.
    """
    chemin = config.dernier_post_connu_path(compte)
    if not chemin.exists():
        return {}
    try:
        with chemin.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Fichier de repère illisible (%s) - repart sans repère de reprise "
            "pour ce run (comportement équivalent à un premier passage).",
            exc,
        )
        return {}


def sauvegarder_dernier_post_connu(
    reperes: dict[str, str], compte: str | None = None
) -> None:
    chemin = config.dernier_post_connu_path(compte)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    with chemin.open("w", encoding="utf-8") as f:
        json.dump(reperes, f, ensure_ascii=False, indent=2)


def verifier_cooldown(compte: str | None = None) -> datetime | None:
    """Retourne la date de fin de cooldown si un cooldown est encore actif, sinon None.

    Fichier corrompu/absent -> pas de cooldown (on ne bloque pas un run à cause
    d'un état illisible, mais on log un avertissement pour investigation).
    `compte` isole ce cooldown par compte Facebook : un blocage détecté sur le
    compte 3 ne doit JAMAIS geler les runs des comptes 1, 2, 4 et 5, qui
    n'ont rien à voir avec ce qui a déclenché le blocage.
    """
    chemin = config.cooldown_path(compte)
    if not chemin.exists():
        return None
    try:
        with chemin.open(encoding="utf-8") as f:
            contenu = json.load(f)
        fin = datetime.fromisoformat(contenu["jusqu_a"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("Fichier de cooldown illisible (%s) - ignoré.", exc)
        return None

    if fin > datetime.now(timezone.utc):
        return fin
    return None


def activer_cooldown(heures: float, raison: str, compte: str | None = None) -> None:
    """Enregistre un cooldown : aucun run (sur CE compte) ne devrait scraper
    avant `heures` heures."""
    fin = datetime.now(timezone.utc) + timedelta(hours=heures)
    chemin = config.cooldown_path(compte)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    with chemin.open("w", encoding="utf-8") as f:
        json.dump(
            {"jusqu_a": fin.isoformat(), "raison": raison},
            f,
            ensure_ascii=False,
            indent=2,
        )
    logger.critical("Cooldown activé jusqu'à %s (raison : %s)", fin.isoformat(), raison)


# --------------------------------------------------------------------------- #
# Throttle adaptatif (AIMD) : ralentit/réduit le volume automatiquement au
# moindre signal de suspicion, ré-accélère lentement après des runs propres.
# Fonctions pures (état en entrée -> nouvel état en sortie), testables sans
# navigateur ni mock complexe - voir README.md pour la logique complète.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AjustementsSession:
    """Réglages effectifs pour le run courant, dérivés du niveau de confiance."""

    delai_multiplicateur: float  # >1.0 = délais rallongés (confiance basse)
    ratio_groupes: (
        float  # 0.0-1.0 = fraction des groupes normalement prévus réellement traités
    )


def charger_sante(compte: str | None = None) -> dict[str, Any]:
    """Charge l'état de santé persistant, ou un état initial "confiance maximale"
    si aucun historique n'existe encore (premier run, ou fichier corrompu).
    `compte` isole ce score de confiance par compte Facebook : un compte qui
    se fait repérer ne doit pas faire chuter la confiance (et donc ralentir/
    réduire le volume) des 4 autres comptes, qui n'ont rien à voir avec ça.
    """
    etat_initial = {
        "niveau_confiance": config.NIVEAU_CONFIANCE_INITIAL,
        "runs_propres_consecutifs": 0,
        "cooldown_multiplicateur": 1,
    }
    chemin = config.sante_path(compte)
    if not chemin.exists():
        return etat_initial
    try:
        with chemin.open(encoding="utf-8") as f:
            etat = json.load(f)
        etat_initial.update(etat)
        return etat_initial
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "État de santé illisible (%s) - repart de la confiance maximale.", exc
        )
        return etat_initial


def sauvegarder_sante(etat: dict[str, Any], compte: str | None = None) -> None:
    chemin = config.sante_path(compte)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    with chemin.open("w", encoding="utf-8") as f:
        json.dump(etat, f, ensure_ascii=False, indent=2)


def calculer_ajustements(etat: dict[str, Any]) -> AjustementsSession:
    """Traduit le niveau de confiance actuel en réglages concrets pour le run.

    Confiance basse -> délais plus longs (jusqu'à x5 au plancher) ET moins de
    groupes traités ce run (les groupes non traités seront repris aux runs
    suivants, une fois la confiance remontée).
    """
    confiance = etat.get("niveau_confiance", config.NIVEAU_CONFIANCE_INITIAL)
    confiance = max(
        config.NIVEAU_CONFIANCE_MIN, min(config.NIVEAU_CONFIANCE_MAX, confiance)
    )
    return AjustementsSession(
        delai_multiplicateur=round(1.0 / confiance, 3),
        ratio_groupes=confiance,
    )


def mettre_a_jour_apres_run(
    etat: dict[str, Any],
    anomalies: int,
    total_groupes: int,
    bloque: bool = False,
    session_expiree: bool = False,
) -> dict[str, Any]:
    """Calcule le nouvel état de santé à partir de l'issue du run qui vient de
    se terminer. Logique AIMD : diminution multiplicative rapide au moindre
    signal négatif, augmentation additive lente seulement après plusieurs
    runs propres consécutifs.

    Args:
        etat: état de santé chargé en début de run (voir `charger_sante`).
        anomalies: nombre de groupes ayant levé une exception inattendue
            (hors blocage/session expirée, déjà gérés à part).
        total_groupes: nombre de groupes réellement tentés ce run.
        bloque: un `BlocageDetecteError` a stoppé le run.
        session_expiree: un `SessionExpireeError` a stoppé le run.
    """
    nouvel_etat = dict(etat)
    confiance = etat.get("niveau_confiance", config.NIVEAU_CONFIANCE_INITIAL)
    multiplicateur_cooldown = etat.get("cooldown_multiplicateur", 1)

    if bloque:
        nouvel_etat["niveau_confiance"] = config.NIVEAU_CONFIANCE_MIN
        nouvel_etat["runs_propres_consecutifs"] = 0
        nouvel_etat["cooldown_multiplicateur"] = min(
            config.COOLDOWN_MULTIPLICATEUR_MAX,
            multiplicateur_cooldown * 2,
        )
    elif session_expiree:
        # Signal plus faible qu'un blocage actif (probablement juste des
        # cookies à renouveler) - on reste prudent sans punir aussi fort.
        nouvel_etat["niveau_confiance"] = max(
            config.NIVEAU_CONFIANCE_MIN, confiance * 0.7
        )
        nouvel_etat["runs_propres_consecutifs"] = 0
    else:
        ratio_anomalies = (anomalies / total_groupes) if total_groupes else 0.0
        if ratio_anomalies > config.RATIO_ANOMALIES_SUSPICION:
            nouvel_etat["niveau_confiance"] = max(
                config.NIVEAU_CONFIANCE_MIN,
                confiance * config.NIVEAU_CONFIANCE_PALIER_SUSPICION,
            )
            nouvel_etat["runs_propres_consecutifs"] = 0
        else:
            propres = etat.get("runs_propres_consecutifs", 0) + 1
            if propres >= config.RUNS_PROPRES_POUR_RAMPUP:
                nouvel_etat["niveau_confiance"] = min(
                    config.NIVEAU_CONFIANCE_MAX,
                    confiance + config.RAMPUP_INCREMENT,
                )
                nouvel_etat["runs_propres_consecutifs"] = 0
                nouvel_etat["cooldown_multiplicateur"] = (
                    1  # reset après un vrai streak propre
                )
            else:
                nouvel_etat["runs_propres_consecutifs"] = propres

    nouvel_etat["derniere_maj"] = datetime.now(timezone.utc).isoformat()
    return nouvel_etat


def sauvegarder_posts_groupe(posts: list[dict[str, Any]], groupe_id: str) -> Path:
    """Sauvegarde incrémentale : un fichier par groupe traité, horodaté.

    Objectif explicite du cahier des charges : ne pas perdre les données déjà
    scrapées en cas de coupure sur un groupe suivant.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    chemin = config.RAW_DIR / f"{timestamp}_{groupe_id}.json"
    with chemin.open("w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)
    logger.info("Groupe %s : %d posts sauvegardés -> %s", groupe_id, len(posts), chemin)
    return chemin


# --------------------------------------------------------------------------- #
# Scraping d'un groupe (web.facebook.com/Comet : scroll + capture GraphQL)
# --------------------------------------------------------------------------- #


async def scraper_groupe(
    context: BrowserContext,
    groupe: "config.Groupe",
    max_days_back: int,
    seen_ids: dict[str, str],
    delai_multiplicateur: float = 1.0,
    post_repere: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Parcourt un groupe Facebook (web.facebook.com, scroll simulé + capture
    réseau GraphQL) et retourne (les nouveaux posts non vus, le nouveau
    post-repère à retenir pour le prochain passage sur ce groupe).

    MÉCANISME D'ARRÊT PRINCIPAL - "post-repère" : `post_repere` est l'id du
    post le plus récent connu lors du run précédent sur CE groupe (voir
    `charger_dernier_post_connu`). On scrolle depuis le haut du fil (le plus
    récent) et on s'arrête dès qu'on retrouve ce post précis dans une réponse
    brute capturée - à ce moment-là, on a la CERTITUDE d'avoir collecté tout
    ce qui a été publié depuis la dernière visite, contrairement à l'ancienne
    heuristique ("4 scrolls sans nouveau post") qui pouvait s'arrêter trop tôt
    sur un groupe très actif où de vrais posts inédits existent encore plus
    bas dans le fil. `MAX_PAGES_ABSOLU` (voir config.py) reste un filet de
    sécurité pour les cas où ce repère est introuvable (post supprimé entre-
    temps, ou tout premier run sur ce groupe).

    Le nouveau repère retourné est le premier post inédit rencontré durant ce
    run (le plus proche du "haut" du fil, donc le plus récent) - si aucun
    post inédit n'a été trouvé ce run (groupe déjà entièrement à jour),
    `None` est retourné et l'appelant doit conserver l'ancien repère plutôt
    que de l'effacer (voir `executer_scraping`).

    INCERTITUDE ASSUMÉE (voir avertissement en tête de fichier) : cette
    fonction n'a pas pu être testée en conditions réelles (aucun accès réseau
    à facebook.com depuis l'environnement de développement). Testez
    impérativement avec `--group-limit 1` avant tout run de production, et
    surveillez les logs pour vérifier qu'au moins quelques posts sont trouvés.

    Deux sources de posts, fusionnées et dédoublonnées par id :
    1. Le HTML initial (posts "mis en avant", peu nombreux) - extraction
       immédiate via `_extraire_stories_depuis_scripts_json`.
    2. Les réponses réseau GraphQL déclenchées par le scroll (le fil
       principal) - interceptées via `page.on("response", ...)` et parsées
       avec le même parseur générique `extraire_stories_depuis_json`.

    Stratégie d'arrêt du scroll : on arrête quand `MAX_PAGES_SANS_NOUVEAU_POST`
    étapes consécutives n'apportent aucun post inédit, après `MAX_PAGES_ABSOLU`
    étapes (garde-fou dur), ou si tous les posts inédits d'une étape de scroll
    sont plus vieux que `max_days_back` ET que leur date est connue (les posts
    à date incertaine ne sont jamais utilisés comme critère d'arrêt, pour
    éviter de couper la collecte à tort). Les posts "mis en avant" (source 1
    ci-dessus) ne participent PAS à ce calcul et sont toujours conservés quel
    que soit leur âge - voir le commentaire détaillé au-dessus de leur bloc
    d'extraction pour la justification (posts épinglés = pas d'ordre
    chronologique fiable, donc inutilisables comme signal de fraîcheur ; une
    fois vus ils entrent dans `seen_ids` et ne sont plus jamais réévalués).

    Args:
        delai_multiplicateur: facteur appliqué aux délais entre étapes de
            scroll (voir `calculer_ajustements` - >1.0 quand le throttle
            adaptatif a perdu confiance suite à des runs récents suspects).
    """
    date_limite = datetime.now(timezone.utc) - timedelta(days=max_days_back)
    page = await context.new_page()
    nouveaux_posts: list[dict[str, Any]] = []
    posts_captures: list[dict[str, Any]] = []
    taches_en_cours: set[asyncio.Task] = set()
    nb_posts_trouves_via_scroll = 0  # exclut les posts "mis en avant" du HTML initial
    # Défini tôt (avant le try) pour être toujours retournable, y compris si
    # une exception interrompt le scraping avant la fin normale de la fonction.
    nouveau_repere: str | None = None
    repere_trouve = False
    compteur_reponses_vues = 0
    compteur_reponses_matchees = 0
    # Échantillons bruts (URL + corps tronqué) des réponses GraphQL matchées,
    # gardés même si aucun post n'y est trouvé - sert de diagnostic best-effort
    # (voir `_sauvegarder_echantillons_graphql_debug`) sans jamais peser sur la
    # mémoire grâce au plafond `NB_ECHANTILLONS_DEBUG_GRAPHQL`.
    echantillons_graphql_bruts: list[dict[str, Any]] = []

    async def _traiter_reponse_graphql(reponse: Any) -> None:
        """Parse une réponse réseau GraphQL et accumule les posts trouvés dans
        `posts_captures`. Best-effort : toute erreur est avalée pour ne
        jamais interrompre le scraping sur une réponse mal formée.
        """
        try:
            corps = await reponse.text()
        except Exception:
            return
        # Ancien préfixe anti-JSON-hijacking parfois toujours présent.
        corps = corps.removeprefix("for (;;);")
        if len(echantillons_graphql_bruts) < config.NB_ECHANTILLONS_DEBUG_GRAPHQL:
            echantillons_graphql_bruts.append(
                {"url": reponse.url, "corps_tronque": corps[:5000]}
            )
        # Une réponse GraphQL Facebook peut contenir plusieurs objets JSON
        # concaténés ligne par ligne ("multipart") - on tente le corps entier
        # puis chaque ligne individuellement.
        for candidat in (corps, *corps.splitlines()):
            candidat = candidat.strip()
            if not candidat:
                continue
            try:
                payload = json.loads(candidat)
            except json.JSONDecodeError:
                continue
            posts_captures.extend(
                extraire_stories_depuis_json(payload, groupe.id, groupe.nom)
            )

    def _sur_reponse(reponse: Any) -> None:
        # Callback SYNCHRONE (API d'événements Playwright) : on ne fait que
        # planifier le traitement async (reponse.text() est une coroutine).
        nonlocal compteur_reponses_vues, compteur_reponses_matchees
        compteur_reponses_vues += 1
        if any(fragment in reponse.url for fragment in config.GRAPHQL_URL_FRAGMENTS):
            compteur_reponses_matchees += 1
            tache = asyncio.ensure_future(_traiter_reponse_graphql(reponse))
            taches_en_cours.add(tache)
            tache.add_done_callback(taches_en_cours.discard)

    page.on("response", _sur_reponse)
    # Navigue vers `groupe.url` tel que renseigné dans groups.csv, plutôt que
    # de reconstruire systématiquement une URL `/groups/<id>/` à partir de
    # `groupe.id` (comportement d'origine, valable uniquement pour un vrai
    # groupe). Ce changement généralise `scraper_groupe` aux Pages Facebook
    # (URL du type `/NomDePage/`, sans id de groupe numérique) - voir
    # README.md, section "Pages Facebook" : NON VALIDÉ en conditions réelles
    # sur une vraie Page (structure JSON Comet potentiellement différente
    # d'un groupe) - à surveiller sur le premier run réel, voir
    # `_sauvegarder_html_debug`/`_sauvegarder_echantillons_graphql_debug` en
    # cas de 0 post trouvé.
    url_groupe = groupe.url

    try:
        logger.info("Ouverture du groupe %s (%s)", groupe.nom, url_groupe)
        await page.goto(url_groupe, wait_until="domcontentloaded")
        await detecter_blocage_ou_session_expiree(page)

        # Posts "mis en avant" présents dès le chargement initial.
        #
        # DÉCISION ASSUMÉE (confirmée en conditions réelles le 2026-08-01,
        # voir README.md, section "Limites connues") : ces posts sont ajoutés
        # SANS filtre sur `max_days_back`, et leur date n'intervient JAMAIS
        # dans la décision de lancer ou non le scroll ci-dessous. Deux
        # raisons, pas un oubli :
        #   1. "Mis en avant" par Facebook/l'admin du groupe ne veut pas dire
        #      "récent" - ce sont des posts épinglés, potentiellement anciens
        #      par nature (annonce phare gardée en tête). Les utiliser comme
        #      signal de fraîcheur pour décider d'arrêter le scroll AVANT
        #      MÊME de l'avoir commencé couperait l'accès au vrai fil (le
        #      seul chronologique) sur la base d'une donnée non pertinente.
        #   2. Cohérent avec le reste de cette fonction : aucune étape (page
        #      mbasic historiquement, étape de scroll ici) ne filtre les
        #      posts un par un par date - la date ne sert qu'à décider de
        #      continuer ou non la collecte, jamais à exclure un post déjà
        #      trouvé. Une fois vu, un post "mis en avant" entre dans
        #      `seen_ids` et ne sera plus jamais réévalué (impact réel
        #      observé : une seule fois par groupe, pas une pollution
        #      quotidienne récurrente du jeu de données).
        html_initial = await page.content()
        posts_initiaux = _extraire_stories_depuis_scripts_json(
            html_initial, groupe.id, groupe.nom
        )
        if not posts_initiaux:
            await _sauvegarder_html_debug(page, groupe.id)
        posts_inedits_initiaux = [
            p for p in posts_initiaux if p["id"] not in seen_ids
        ]
        for p in posts_inedits_initiaux:
            seen_ids[p["id"]] = p["scrape_le"]
        nouveaux_posts.extend(posts_inedits_initiaux)
        # Note : les posts "mis en avant" ne sont volontairement PAS utilisés
        # comme candidat pour `nouveau_repere` (pas de garantie chronologique,
        # voir le commentaire détaillé plus haut sur ce même sujet) ni comme
        # repère de recherche `repere_trouve` - seul le fil scrollé fait foi.

        await asyncio.sleep(
            random.uniform(config.PAGE_DELAY_MIN_S, config.PAGE_DELAY_MAX_S)
            * delai_multiplicateur
        )

        etapes_sans_nouveau = 0
        etapes_scroll = 0

        while etapes_scroll < config.MAX_PAGES_ABSOLU:
            debut_capture = len(posts_captures)
            # Scroll page-niveau (window), plus fiable en headless que
            # page.mouse.wheel dont l'effet dépend de la position du curseur.
            await page.evaluate("window.scrollBy(0, window.innerHeight * 3)")
            await asyncio.sleep(
                random.uniform(config.PAGE_DELAY_MIN_S, config.PAGE_DELAY_MAX_S)
                * delai_multiplicateur
            )
            # Laisse le temps aux tâches de capture réseau déclenchées par ce
            # scroll de se terminer avant d'évaluer ce qui a été trouvé.
            if taches_en_cours:
                await asyncio.gather(*list(taches_en_cours), return_exceptions=True)

            nouveaux_bruts = posts_captures[debut_capture:]

            # Détection du post-repère AVANT filtrage par seen_ids : le
            # repère provient forcément d'un run précédent, donc il est déjà
            # dans seen_ids et n'apparaîtrait jamais dans `posts_inedits` -
            # il faut le chercher dans les posts bruts capturés à cette étape.
            if post_repere and not repere_trouve:
                if any(p["id"] == post_repere for p in nouveaux_bruts):
                    repere_trouve = True
                    logger.info(
                        "Groupe %s : post-repère du run précédent retrouvé - "
                        "rattrapage complet, arrêt du scroll pour ce groupe.",
                        groupe.nom,
                    )

            vus_cette_etape: set[str] = set()
            posts_inedits: list[dict[str, Any]] = []
            for p in nouveaux_bruts:
                if p["id"] not in seen_ids and p["id"] not in vus_cette_etape:
                    vus_cette_etape.add(p["id"])
                    posts_inedits.append(p)

            if posts_inedits:
                etapes_sans_nouveau = 0
                for p in posts_inedits:
                    seen_ids[p["id"]] = p["scrape_le"]
                nouveaux_posts.extend(posts_inedits)
                nb_posts_trouves_via_scroll += len(posts_inedits)
                # Le premier post inédit rencontré est le plus proche du haut
                # du fil sur ce run, donc le candidat le plus fiable comme
                # "post le plus récent" - servira de repère au prochain passage.
                if nouveau_repere is None:
                    nouveau_repere = posts_inedits[0]["id"]
            else:
                etapes_sans_nouveau += 1

            logger.info(
                "Groupe %s | étape scroll %d | réponses réseau vues=%d matchées_graphql=%d "
                "| posts capturés cumulés=%d",
                groupe.nom, etapes_scroll, compteur_reponses_vues,
                compteur_reponses_matchees, len(posts_captures),
            )

            # Critère d'arrêt PRINCIPAL : le post-repère du run précédent a été
            # retrouvé -> on a la certitude d'avoir tout rattrapé sur ce groupe.
            if repere_trouve:
                break

            # Critère d'arrêt "hors fenêtre temporelle" : uniquement sur posts datés.
            posts_dates_connues = [p for p in posts_inedits if p["date_publication"]]
            if posts_dates_connues:
                plus_ancien = min(
                    datetime.fromisoformat(p["date_publication"])
                    for p in posts_dates_connues
                )
                if plus_ancien < date_limite:
                    logger.info(
                        "Groupe %s : posts hors fenêtre de %d jour(s) atteints, arrêt du scroll.",
                        groupe.nom,
                        max_days_back,
                    )
                    break

            if etapes_sans_nouveau >= config.MAX_PAGES_SANS_NOUVEAU_POST:
                logger.info(
                    "Groupe %s : %d étape(s) de scroll sans nouveau post, arrêt.",
                    groupe.nom,
                    etapes_sans_nouveau,
                )
                break

            etapes_scroll += 1

        if etapes_scroll >= config.MAX_PAGES_ABSOLU and not repere_trouve:
            logger.warning(
                "Groupe %s : garde-fou MAX_PAGES_ABSOLU=%d atteint SANS avoir "
                "retrouvé le post-repère du run précédent (id=%s) - soit ce post "
                "a été supprimé/déplacé entre-temps, soit le groupe est "
                "anormalement actif. Le rattrapage est donc potentiellement "
                "incomplet pour ce groupe sur ce run ; il continuera au run "
                "suivant à partir du nouveau repère.",
                groupe.nom,
                config.MAX_PAGES_ABSOLU,
                post_repere,
            )

        if nb_posts_trouves_via_scroll == 0 and echantillons_graphql_bruts:
            _sauvegarder_echantillons_graphql_debug(echantillons_graphql_bruts, groupe.id)

    except PlaywrightTimeoutError as exc:
        logger.error("Timeout navigation sur le groupe %s : %s", groupe.nom, exc)
    finally:
        page.remove_listener("response", _sur_reponse)
        if taches_en_cours:
            await asyncio.gather(*list(taches_en_cours), return_exceptions=True)
        await page.close()

    return nouveaux_posts, nouveau_repere


# --------------------------------------------------------------------------- #
# Orchestration : batches de groupes + pauses inter-batch
# --------------------------------------------------------------------------- #


async def executer_scraping(
    mode: str,
    days_back: int,
    group_limit: int | None,
    groups_batch_size: int,
    round_robin: bool = False,
    compte: str | None = None,
) -> list[Path]:
    """Point d'entrée principal du module, appelé par main.py.

    Args:
        mode: "daily" ou "backfill" (influence uniquement le logging - le
            comportement de pagination est identique, seul `days_back` change).
        days_back: fenêtre temporelle en jours (1 pour le quotidien, jusqu'à
            90 pour un rattrapage complet - à répartir en plusieurs runs
            paramétrables via `days_back`/`group_limit` côté CLI/CI plutôt que
            de tout tenter en un seul run, pour rester sous les limites mémoire
            du navigateur headless et réduire le risque de détection).
        group_limit: en mode normal, nombre max de groupes traités sur ce run
            (None = tous). En mode `round_robin`, nombre de groupes traités à
            CE run parmi la rotation (voir ci-dessous) - None équivaut à 1.
        groups_batch_size: taille des lots de groupes entre deux pauses longues.
        round_robin: si True, ne traite qu'un sous-ensemble tournant des
            groupes actifs à chaque run (au lieu de tous les groupes d'un
            coup) - l'état de rotation persiste entre les runs via
            `config.index_prochain_groupe_path`. Pensé pour être appelé
            fréquemment (ex : cron horaire) avec un `group_limit` petit (1-2),
            afin que l'espacement entre deux passages sur un MÊME groupe soit
            obtenu naturellement par l'espacement des runs eux-mêmes, plutôt
            que de scroller tous les groupes à la suite dans une seule session
            (ce qui semble accélérer un frein de Facebook en cours de run,
            voir README.md).
        compte: identifiant du compte Facebook à utiliser ("1".."5", voir
            README.md section "Multi-comptes"). None (défaut) = comportement
            historique mono-compte : secret `FB_COOKIES_JSON`, état global
            (data/state/*.json), TOUS les groupes actifs de groups.csv quel
            que soit leur colonne `compte`. Une valeur "1".."5" restreint le
            run au secret `FB_COOKIES_JSON_<compte>`, à un état isolé
            (data/state/compte_<compte>/) et UNIQUEMENT aux groupes assignés
            à ce compte dans groups.csv - c'est ce qui permet de lancer les 5
            comptes en parallèle (ou séquentiellement) sans qu'ils ne se
            marchent dessus (mêmes cookies rechargés deux fois, cooldown ou
            score de confiance partagé à tort, etc.).

    Returns:
        Liste des chemins des fichiers JSON bruts sauvegardés (un par groupe).

    Raises:
        ValueError: secret de cookies absent/invalide, ou aucun groupe configuré
            pour ce compte.
        CooldownActifError: un cooldown anti-blocage est encore actif (voir
            `verifier_cooldown`) - le run s'arrête avant même d'ouvrir un navigateur.
        SessionExpireeError: propagée si détectée sur un groupe (signal fort
            que les cookies sont morts - inutile de continuer sur les autres).
        BlocageDetecteError: propagée si un mur anti-bot est détecté - le run
            s'arrête entièrement (voir stratégie anti-blocage dans README.md),
            il ne continue PAS sur les groupes restants.
    """
    import os

    cooldown_actif = verifier_cooldown(compte)
    if cooldown_actif:
        raise CooldownActifError(
            f"Cooldown anti-blocage actif jusqu'à {cooldown_actif.isoformat()} - run annulé."
        )

    nom_secret = config.nom_secret_cookies(compte)
    cookies_json = os.environ.get(nom_secret)
    if not cookies_json:
        raise ValueError(f"Variable d'environnement {nom_secret} absente.")
    cookies_secret = charger_cookies(cookies_json)

    # Priorité aux cookies "vivants" du run précédent (potentiellement déjà
    # renouvelés par Facebook) sur le secret statique - voir
    # `_charger_cookies_caches` pour la justification complète. Repli
    # automatique et silencieux sur le secret si le cache est absent/invalide,
    # ce qui couvre aussi bien le premier run que le run juste après une
    # régénération manuelle du secret suite à une vraie expiration.
    cookies_caches = _charger_cookies_caches(compte)
    cookies = cookies_caches if cookies_caches is not None else cookies_secret

    if round_robin:
        tous_les_groupes_actifs = config.charger_groupes(limite=None, compte=compte)
        nb_a_traiter = group_limit or 1
        total = len(tous_les_groupes_actifs)
        index_depart = config.charger_index_prochain_groupe(compte) % total
        nb_a_traiter = min(nb_a_traiter, total)
        groupes = [
            tous_les_groupes_actifs[(index_depart + i) % total]
            for i in range(nb_a_traiter)
        ]
        nouvel_index = (index_depart + nb_a_traiter) % total
        config.sauvegarder_index_prochain_groupe(nouvel_index, compte)
        logger.info(
            "Round-robin : %d/%d groupe(s) traité(s) ce run (index %d -> %d) : %s",
            nb_a_traiter, total, index_depart, nouvel_index,
            ", ".join(g.nom for g in groupes),
        )
    else:
        groupes = config.charger_groupes(limite=group_limit, compte=compte)

    etat_sante = charger_sante(compte)
    ajustements = calculer_ajustements(etat_sante)
    if ajustements.ratio_groupes < 1.0:
        nb_avant = len(groupes)
        groupes = groupes[: max(1, round(nb_avant * ajustements.ratio_groupes))]
        logger.warning(
            "Throttle adaptatif actif (confiance=%.2f) : %d/%d groupe(s) traités ce run, "
            "délais x%.2f. Les groupes restants seront repris aux prochains runs.",
            etat_sante.get("niveau_confiance", 1.0),
            len(groupes),
            nb_avant,
            ajustements.delai_multiplicateur,
        )

    logger.info(
        "Compte=%s | Mode=%s | %d groupe(s) à traiter | days_back=%d | batch=%d",
        compte or "unique",
        mode,
        len(groupes),
        days_back,
        groups_batch_size,
    )

    proxy = config.proxy_playwright(compte)
    if proxy is not None:
        logger.info(
            "Proxy configuré pour ce run (%s) : sortie via %s.",
            config.nom_secret_proxy(compte),
            proxy["server"],
        )

    seen_ids = charger_seen_ids()
    reperes_dernier_post = charger_dernier_post_connu(compte)
    fichiers_sauvegardes: list[Path] = []
    debut_session = datetime.now(timezone.utc)
    budget_depasse = False
    anomalies = 0
    bloque = False
    session_expiree = False

    async with async_playwright() as playwright:
        navigateur, contexte = await creer_navigateur(playwright, cookies, compte, proxy)
        try:
            await echauffement(contexte)

            for debut_batch in range(0, len(groupes), groups_batch_size):
                if budget_depasse:
                    break

                lot = groupes[debut_batch : debut_batch + groups_batch_size]
                logger.info(
                    "--- Batch %d groupe(s) : %s ---",
                    len(lot),
                    ", ".join(g.nom for g in lot),
                )

                for i, groupe in enumerate(lot):
                    ecoulees_min = (
                        datetime.now(timezone.utc) - debut_session
                    ).total_seconds() / 60
                    if ecoulees_min >= config.SESSION_DUREE_MAX_MINUTES:
                        logger.warning(
                            "Budget de session atteint (%.1f min) - arrêt propre, "
                            "%d groupe(s) restant(s) traités au prochain run.",
                            ecoulees_min,
                            len(groupes) - (debut_batch + i),
                        )
                        budget_depasse = True
                        break

                    try:
                        posts, nouveau_repere = await scraper_groupe(
                            contexte,
                            groupe,
                            days_back,
                            seen_ids,
                            delai_multiplicateur=ajustements.delai_multiplicateur,
                            post_repere=reperes_dernier_post.get(groupe.id),
                        )
                        # Ne remplace le repère existant QUE si un nouveau post
                        # inédit a réellement été trouvé ce run - sinon (groupe
                        # déjà à jour, ou run interrompu tôt) on garde l'ancien
                        # repère plutôt que de le perdre avec une valeur `None`.
                        if nouveau_repere:
                            reperes_dernier_post[groupe.id] = nouveau_repere
                    except SessionExpireeError as exc:
                        logger.critical(
                            "Session Facebook expirée sur le groupe %s. Arrêt du run - "
                            "il faut régénérer FB_COOKIES_JSON.",
                            groupe.nom,
                        )
                        session_expiree = True
                        activer_cooldown(
                            config.COOLDOWN_HEURES_APRES_SESSION_EXPIREE,
                            f"session expirée sur {groupe.nom}: {exc}",
                            compte,
                        )
                        raise
                    except BlocageDetecteError as exc:
                        logger.critical(
                            "Blocage anti-bot détecté sur %s (%s) - arrêt COMPLET du run "
                            "(les autres groupes ne sont pas tentés).",
                            groupe.nom,
                            exc,
                        )
                        bloque = True
                        multiplicateur_cooldown = etat_sante.get(
                            "cooldown_multiplicateur", 1
                        )
                        activer_cooldown(
                            config.COOLDOWN_HEURES_APRES_BLOCAGE
                            * multiplicateur_cooldown,
                            f"blocage détecté sur {groupe.nom}: {exc}",
                            compte,
                        )
                        raise
                    except Exception:
                        logger.exception(
                            "Erreur inattendue sur le groupe %s - groupe ignoré.",
                            groupe.nom,
                        )
                        anomalies += 1
                        continue

                    if posts:
                        fichiers_sauvegardes.append(
                            sauvegarder_posts_groupe(posts, groupe.id)
                        )
                    sauvegarder_seen_ids(
                        seen_ids
                    )  # sauvegarde après CHAQUE groupe (résilience coupure)
                    sauvegarder_dernier_post_connu(
                        reperes_dernier_post, compte
                    )  # idem : le repère de reprise ne doit jamais se perdre

                    # Pause entre deux groupes du même lot - 10-15 minutes
                    # (voir config.PAUSE_ENTRE_GROUPES_MIN_S/MAX_S), distincte
                    # du délai entre étapes de scroll (config.PAGE_DELAY_*,
                    # en secondes, utilisé PENDANT le scroll d'un même groupe).
                    if i < len(lot) - 1:
                        await asyncio.sleep(
                            random.uniform(
                                config.PAUSE_ENTRE_GROUPES_MIN_S,
                                config.PAUSE_ENTRE_GROUPES_MAX_S,
                            )
                            * ajustements.delai_multiplicateur
                        )

                # Pause plus longue entre deux batches de groupes.
                if not budget_depasse and debut_batch + groups_batch_size < len(
                    groupes
                ):
                    pause = (
                        random.uniform(
                            config.PAUSE_ENTRE_BATCHES_MIN_S,
                            config.PAUSE_ENTRE_BATCHES_MAX_S,
                        )
                        * ajustements.delai_multiplicateur
                    )
                    logger.info("Pause inter-batch de %.1fs", pause)
                    await asyncio.sleep(pause)
        finally:
            if session_expiree:
                # Cookies connus morts sur CE run - ne pas les mettre en cache
                # (voir invalider_storage_state), sinon ils masqueraient tout
                # renouvellement du secret de cookies au run suivant.
                invalider_storage_state(compte)
            else:
                await sauvegarder_storage_state(contexte, compte)
            await contexte.close()
            await navigateur.close()
            nouvel_etat_sante = mettre_a_jour_apres_run(
                etat_sante,
                anomalies=anomalies,
                total_groupes=len(groupes),
                bloque=bloque,
                session_expiree=session_expiree,
            )
            sauvegarder_sante(nouvel_etat_sante, compte)
            if nouvel_etat_sante.get("niveau_confiance") != etat_sante.get(
                "niveau_confiance"
            ):
                logger.info(
                    "Confiance du throttle adaptatif : %.2f -> %.2f",
                    etat_sante.get("niveau_confiance", 1.0),
                    nouvel_etat_sante.get("niveau_confiance", 1.0),
                )

    return fichiers_sauvegardes


if __name__ == "__main__":
    # Exécution ad hoc pour test manuel local (main.py reste le point d'entrée CLI officiel).
    logging.basicConfig(level=logging.INFO)
    asyncio.run(
        executer_scraping(mode="daily", days_back=1, group_limit=1, groups_batch_size=1)
    )
