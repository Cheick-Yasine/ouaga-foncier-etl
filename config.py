"""Configuration centrale du pipeline ETL (mots-clés, quartiers, délais, chemins).

Toute constante "métier" (regex, quartiers, délais anti-bot) vit ici pour que
scraper.py / processor.py / main.py restent des modules de logique pure, sans
valeur hardcodée dispersée.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

_logger = logging.getLogger("ouaga_foncier_etl.config")

# --------------------------------------------------------------------------- #
# Arborescence
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"  # posts bruts scrapés, sauvegarde incrémentale
PROCESSED_DIR = DATA_DIR / "processed"  # sorties CSV/JSON structurées
STATE_DIR = DATA_DIR / "state"  # ids déjà vus (déduplication inter-runs)
LOG_DIR = DATA_DIR / "logs"

for _dir in (RAW_DIR, PROCESSED_DIR, STATE_DIR, LOG_DIR):
    _dir.mkdir(parents=True, exist_ok=True)

GROUPS_CSV_PATH = BASE_DIR / "groups.csv"
SEEN_IDS_PATH = STATE_DIR / "seen_post_ids.json"
# {groupe_id: post_id} du post le plus récent connu pour chaque groupe, au
# moment où le run précédent a terminé son scroll. Sert de repère d'arrêt :
# au prochain passage sur ce groupe, on scrolle depuis le haut du fil jusqu'à
# retrouver ce post précis, puis on s'arrête - on sait alors avec certitude
# qu'on a rattrapé TOUT ce qui a été publié depuis la dernière visite, sans
# dépendre d'un plafond de scrolls arbitraire (voir MAX_PAGES_ABSOLU ci-dessous,
# qui reste un filet de sécurité, plus le mécanisme d'arrêt principal).
DERNIER_POST_CONNU_PATH = STATE_DIR / "dernier_post_connu.json"
COOLDOWN_PATH = STATE_DIR / "cooldown_until.json"
STORAGE_STATE_PATH = STATE_DIR / "storage_state.json"
SANTE_PATH = STATE_DIR / "sante_scraper.json"
INDEX_PROCHAIN_GROUPE_PATH = STATE_DIR / "prochain_groupe_index.json"

# --------------------------------------------------------------------------- #
# Multi-comptes : un état persistant ISOLÉ par compte Facebook (2026-08-26,
# passage à 5 comptes se partageant les groupes de groups.csv, voir colonne
# `compte` ci-dessous et le README, section "Multi-comptes").
#
# Chaque compte a SA PROPRE session (cookies/localStorage), SON PROPRE
# cooldown, SON PROPRE historique de confiance (throttle adaptatif) : un
# blocage ou une session expirée sur le compte 3 ne doit ni geler les 4
# autres comptes (cooldown partagé), ni fausser leur score de confiance
# (mettre_a_jour_apres_run partagé). Les constantes globales ci-dessus
# (SEEN_IDS_PATH, STORAGE_STATE_PATH, etc.) restent inchangées et servent de
# repli pour compte=None : c'est ce qui permet aux tests existants et à tout
# appel historique sans paramètre `compte` de continuer à fonctionner à
# l'identique (rétrocompatibilité - voir tests/test_scraper_helpers.py qui
# écrit directement dans ces chemins).
#
# `seen_post_ids.json` est également isolé par compte. Le workflow matriciel
# ne restaure que `data/state/compte_<n>/` : conserver ce fichier à la racine
# faisait perdre la déduplication entre deux runs et pouvait réexporter les
# posts épinglés. L'isolation évite aussi les écritures concurrentes des cinq
# jobs sur un même fichier.
COMPTES_VALIDES = {"1", "2", "3", "4", "5"}


def _repertoire_compte(compte: str) -> Path:
    """Sous-répertoire d'état dédié à un compte (créé si absent)."""
    if compte not in COMPTES_VALIDES:
        raise ValueError(
            f"Compte '{compte}' inconnu (valeurs valides : {sorted(COMPTES_VALIDES)})."
        )
    repertoire = STATE_DIR / f"compte_{compte}"
    repertoire.mkdir(parents=True, exist_ok=True)
    return repertoire


def seen_ids_path(compte: str | None = None) -> Path:
    """Chemin de déduplication persistant, isolé par compte en mode matriciel."""
    return SEEN_IDS_PATH if compte is None else _repertoire_compte(compte) / "seen_post_ids.json"


def dernier_post_connu_path(compte: str | None = None) -> Path:
    return DERNIER_POST_CONNU_PATH if compte is None else _repertoire_compte(compte) / "dernier_post_connu.json"


def cooldown_path(compte: str | None = None) -> Path:
    return COOLDOWN_PATH if compte is None else _repertoire_compte(compte) / "cooldown_until.json"


def storage_state_path(compte: str | None = None) -> Path:
    return STORAGE_STATE_PATH if compte is None else _repertoire_compte(compte) / "storage_state.json"


def sante_path(compte: str | None = None) -> Path:
    return SANTE_PATH if compte is None else _repertoire_compte(compte) / "sante_scraper.json"


def index_prochain_groupe_path(compte: str | None = None) -> Path:
    return INDEX_PROCHAIN_GROUPE_PATH if compte is None else _repertoire_compte(compte) / "prochain_groupe_index.json"


def nom_secret_cookies(compte: str | None = None) -> str:
    """Nom de la variable d'environnement contenant les cookies FB à utiliser.

    compte=None -> secret historique unique `FB_COOKIES_JSON` (rétrocompatibilité,
    utilisé par défaut si le run ne précise pas de compte).
    compte="1".."5" -> un secret dédié par compte : `FB_COOKIES_JSON_1` ...
    `FB_COOKIES_JSON_5`, à créer dans Settings -> Secrets and variables ->
    Actions pour chacun des 5 comptes (voir README.md, section "Multi-comptes").
    """
    return ENV_FB_COOKIES if compte is None else f"{ENV_FB_COOKIES}_{compte}"

def nom_secret_proxy(compte: str | None = None) -> str:
    """Nom de la variable d'environnement contenant l'URL du proxy à utiliser
    pour ce compte (même convention que `nom_secret_cookies` ci-dessus).

    compte=None -> secret partagé `PROXY_URL` (repli historique, utilisé si le
    run ne précise pas de compte, ou si un seul proxy sert les 5 comptes).
    compte="1".."5" -> un secret dédié par compte `PROXY_URL_1` ... `PROXY_URL_5`
    (à créer dans Settings -> Secrets and variables -> Actions), pour que
    chaque compte sorte par une IP distincte plutôt que de partager la même -
    voir README.md, section "Proxy (réputation IP/ASN)".
    """
    return ENV_PROXY_URL if compte is None else f"{ENV_PROXY_URL}_{compte}"

def proxy_playwright(
    compte: str | None = None,
    *,
    obligatoire: bool | None = None,
) -> dict[str, str] | None:
    """Lit et parse l'URL de proxy pour ce compte (voir `nom_secret_proxy`),
    au format attendu par Playwright (`BrowserType.launch(proxy=...)`) :
    `{"server": "schéma://hôte:port", "username"?: str, "password"?: str}`.

    POURQUOI UN PROXY (voir README.md, section "Stratégie anti-blocage") : le
    facteur qui pèse le plus sur le risque de blocage Facebook n'est pas le
    comportement du scraper (délais, user-agent, etc.) mais la réputation de
    l'IP/ASN d'où partent les requêtes. Une IP de datacenter GitHub Actions,
    jamais associée au compte auparavant, peut faire invalider la session
    immédiatement côté serveur Facebook (SessionExpireeError, signature
    USER_ID/actorID à 0 - voir `detecter_blocage_ou_session_expiree` dans
    scraper.py) même avec des cookies fraîchement régénérés. Un proxy
    résidentiel/mobile donne une IP à réputation plus proche d'un usage
    humain réel. Ce n'est PAS une garantie (même réserve assumée que pour les
    autres mesures anti-blocage - voir `creer_navigateur`), seulement une
    réduction de risque supplémentaire.

    Format attendu de la variable d'environnement (ex. PROXY_URL_1) :
        http://utilisateur:motdepasse@hote:port
        http://hote:port                          (proxy sans authentification)
        socks5://utilisateur:motdepasse@hote:port  (Playwright supporte aussi SOCKS5)

    En mode multi-comptes, le proxy reste obligatoire par défaut. Une
    connexion directe doit être autorisée explicitement avec
    ALLOW_DIRECT_CONNECTION=true. Le workflow auto-hébergé utilise ce drapeau
    uniquement en mode réseau « direct », afin qu'une omission accidentelle de
    secret proxy ne fasse pas basculer silencieusement un runner hébergé.
    """
    if obligatoire is None:
        connexion_directe = os.environ.get(
            "ALLOW_DIRECT_CONNECTION", ""
        ).strip().lower() in {"1", "true", "vrai", "yes", "oui"}
        obligatoire = compte is not None and not connexion_directe

    nom_variable = nom_secret_proxy(compte)
    valeur = os.environ.get(nom_variable, "").strip()
    if not valeur:
        if obligatoire:
            raise ValueError(
                f"{nom_variable} est absent ou vide : exécution refusée pour "
                "éviter une sortie réseau accidentelle sans proxy."
            )
        return None

    try:
        analyse = urllib.parse.urlsplit(valeur)
        port = analyse.port
    except ValueError as exc:
        raise ValueError(f"{nom_variable} contient un port invalide.") from exc

    schemas_acceptes = {"http", "https", "socks5"}
    if analyse.scheme.lower() not in schemas_acceptes or not analyse.hostname:
        message = (
            f"{nom_variable} est invalide : format attendu "
            "http(s)://[utilisateur:motdepasse@]hôte:port ou socks5://..."
        )
        if obligatoire:
            raise ValueError(message)
        _logger.warning("%s Proxy ignoré.", message)
        return None

    suffixe_port = f":{port}" if port else ""
    proxy: dict[str, str] = {
        "server": f"{analyse.scheme.lower()}://{analyse.hostname}{suffixe_port}"
    }
    if analyse.username:
        proxy["username"] = urllib.parse.unquote(analyse.username)
    if analyse.password:
        proxy["password"] = urllib.parse.unquote(analyse.password)
    return proxy


@dataclass(frozen=True)
class ParametresRegionaux:
    pays: str
    locale: str
    fuseau_horaire: str


def _variable_par_compte(nom: str, compte: str | None, defaut: str) -> str:
    if compte is not None:
        valeur_compte = os.environ.get(f"{nom}_{compte}", "").strip()
        if valeur_compte:
            return valeur_compte
    return os.environ.get(nom, "").strip() or defaut


def parametres_regionaux(compte: str | None = None) -> ParametresRegionaux:
    """Configuration déclarée du proxy et du navigateur pour un compte."""
    if compte is not None and compte not in COMPTES_VALIDES:
        raise ValueError(f"Compte '{compte}' inconnu.")
    pays = _variable_par_compte("PROXY_COUNTRY", compte, "BF").upper()
    if not re.fullmatch(r"[A-Z]{2}", pays):
        raise ValueError("PROXY_COUNTRY doit être un code ISO de deux lettres (ex. BF).")
    return ParametresRegionaux(
        pays=pays,
        locale=_variable_par_compte("BROWSER_LOCALE", compte, "fr-FR"),
        fuseau_horaire=_variable_par_compte(
            "BROWSER_TIMEZONE", compte, "Africa/Ouagadougou"
        ),
    )


# Vue Excel régénérée à chaque run à partir de la base maître PostgreSQL - UN
# SEUL fichier, toujours à jour, plutôt qu'un CSV différent par run (voir
# processor.py). La base maître elle-même n'est plus un fichier local depuis
# la migration SQLite -> PostgreSQL : voir DATABASE_URL ci-dessous.
MASTER_XLSX_PATH = PROCESSED_DIR / "annonces.xlsx"

# --------------------------------------------------------------------------- #
# Variables d'environnement (secrets)
# --------------------------------------------------------------------------- #

ENV_FB_COOKIES = "FB_COOKIES_JSON"
ENV_OPENAI_KEY = "OPENAI_API_KEY"
ENV_DATABASE_URL = "DATABASE_URL"
# Proxy optionnel (voir `proxy_playwright` plus haut) - absent par défaut, le
# pipeline fonctionne sans exactement comme avant l'ajout de cette variable.
ENV_PROXY_URL = "PROXY_URL"

# Base de données maître PostgreSQL (source de vérité, upsert par id de post).
# Lue depuis l'environnement (secret GitHub Actions en CI, .env en local via
# python-dotenv - voir main.py). Pas de valeur par défaut "pratique" du type
# localhost:5432 : une absence de DATABASE_URL doit échouer bruyamment plutôt
# que de pointer silencieusement vers une base qui n'existe pas chez l'utilisateur.
# `.strip()` : un secret GitHub Actions collé avec un retour à la ligne final
# (piège courant - un simple copier-coller depuis un fichier/dashboard suffit)
# produit une chaîne du type "...sslmode=require\n", que libpq/psycopg refuse
# purement et simplement (`invalid sslmode value`) - confirmé en conditions
# réelles le 2026-08-01 sur le premier run du workflow GitHub Actions.
DATABASE_URL = os.environ.get(ENV_DATABASE_URL, "").strip()

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def configurer_logging(niveau: int = logging.INFO) -> logging.Logger:
    """Configure un logger unique pour tout le pipeline (console + fichier)."""
    logger = logging.getLogger("ouaga_foncier_etl")
    if logger.handlers:  # évite les handlers dupliqués si appelé plusieurs fois
        return logger
    logger.setLevel(niveau)

    formatteur = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler_console = logging.StreamHandler()
    handler_console.setFormatter(formatteur)
    logger.addHandler(handler_console)

    handler_fichier = logging.FileHandler(LOG_DIR / "pipeline.log", encoding="utf-8")
    handler_fichier.setFormatter(formatteur)
    logger.addHandler(handler_fichier)

    return logger


# --------------------------------------------------------------------------- #
# Groupes Facebook à scraper
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Groupe:
    """Représente un groupe Facebook cible."""

    id: str
    nom: str
    url: str
    actif: bool = True
    # Compte Facebook assigné pour scraper CE groupe ("1".."5"). Colonne
    # OPTIONNELLE dans groups.csv (voir charger_groupes) : absente ou vide ->
    # "1" par défaut, pour rester compatible avec un groups.csv historique
    # (mono-compte, avant le passage multi-comptes du 2026-08-26 - voir
    # README.md, section "Multi-comptes").
    compte: str = "1"


def charger_groupes(
    chemin: Path = GROUPS_CSV_PATH,
    limite: int | None = None,
    compte: str | None = None,
) -> list[Groupe]:
    """Charge la liste des groupes depuis groups.csv (source unique de vérité).

    IMPORTANT : cette liste n'est PAS hardcodée dans le code. Le fichier
    Groupe.xlsx fourni à la racine de "F:\\Scraping Facebook" n'a pas pu être
    lu automatiquement lors de la génération de ce projet (accès shell
    indisponible dans mon environnement à ce moment-là). `groups.csv` contient
    donc des lignes TODO à compléter/valider manuellement - voir README.md.

    Args:
        chemin: chemin vers le fichier CSV des groupes.
        limite: si fourni, ne retourne que les N premiers groupes actifs
            (après filtrage par `compte` le cas échéant) - utilisé par
            `--group-limit` en CLI pour les tests/rattrapages.
        compte: si fourni ("1".."5"), ne retourne que les groupes actifs
            assignés à ce compte (colonne `compte` de groups.csv) - c'est ce
            qui permet à un run donné de ne traiter QUE les groupes de son
            compte Facebook, jamais ceux des 4 autres (voir README.md,
            section "Multi-comptes"). None (défaut) = tous les comptes
            confondus, comportement historique inchangé.

    Raises:
        FileNotFoundError: si groups.csv est absent.
        ValueError: si le CSV est vide ou mal formé, ou si `compte` n'est
            pas une valeur reconnue (voir COMPTES_VALIDES).
    """
    if compte is not None and compte not in COMPTES_VALIDES:
        raise ValueError(
            f"Compte '{compte}' inconnu (valeurs valides : {sorted(COMPTES_VALIDES)})."
        )

    if not chemin.exists():
        raise FileNotFoundError(
            f"Fichier de groupes introuvable : {chemin}. "
            "Créez-le à partir de Groupe.xlsx (voir README.md)."
        )

    groupes: list[Groupe] = []
    with chemin.open(encoding="utf-8") as f:
        lecteur = csv.DictReader(f)
        colonnes_attendues = {"id", "nom", "url", "actif"}
        if lecteur.fieldnames is None or not colonnes_attendues.issubset(set(lecteur.fieldnames)):
            raise ValueError(
                f"En-têtes CSV invalides dans {chemin} : attendu {colonnes_attendues}, "
                f"trouvé {lecteur.fieldnames}"
            )
        # "compte" reste une colonne OPTIONNELLE (rétrocompatibilité avec un
        # groups.csv mono-compte antérieur) : absente du CSV -> "1" pour
        # toutes les lignes.
        colonne_compte_presente = "compte" in (lecteur.fieldnames or [])
        for ligne in lecteur:
            if ligne["id"].strip().upper().startswith("TODO"):
                continue  # ligne placeholder non complétée : on l'ignore silencieusement
            valeur_compte = (ligne.get("compte") or "").strip() if colonne_compte_presente else ""
            valeur_compte = valeur_compte or "1"
            if valeur_compte not in COMPTES_VALIDES:
                raise ValueError(
                    f"Compte '{valeur_compte}' invalide pour le groupe '{ligne['id'].strip()}' "
                    f"dans {chemin} (valeurs valides : {sorted(COMPTES_VALIDES)})."
                )
            # Normalise l'URL vers m.facebook.com (mode mobile) si elle pointe
            # encore vers www/web - cohérent avec le fingerprint mobile.
            url_brute = ligne["url"].strip()
            url_mobile = re.sub(
                r"https?://(www\.|web\.)?facebook\.com",
                "https://m.facebook.com",
                url_brute,
            )
            groupes.append(
                Groupe(
                    id=ligne["id"].strip(),
                    nom=ligne["nom"].strip(),
                    url=url_mobile,
                    actif=ligne["actif"].strip().lower() in ("1", "true", "vrai", "oui"),
                    compte=valeur_compte,
                )
            )

    groupes_actifs = [g for g in groupes if g.actif]
    if compte is not None:
        groupes_actifs = [g for g in groupes_actifs if g.compte == compte]
    if not groupes_actifs:
        raise ValueError(
            f"Aucun groupe actif trouvé dans {chemin}"
            + (f" pour le compte '{compte}'" if compte is not None else "")
            + ". Vérifiez que les lignes TODO ont bien été remplacées et que "
            "la colonne 'compte' assigne bien des groupes à chaque compte."
        )

    if limite is not None and limite > 0:
        groupes_actifs = groupes_actifs[:limite]

    return groupes_actifs


# --------------------------------------------------------------------------- #
# Rotation "round-robin" des groupes entre plusieurs runs (voir --round-robin
# dans main.py). Permet de traiter 1 (ou quelques) groupe(s) par run plutôt
# que tous les groupes d'un coup dans la même session - l'espacement entre
# deux groupes est alors obtenu naturellement en espaçant les runs eux-mêmes
# (via la planification cron), pas en faisant "dormir" un job.
# --------------------------------------------------------------------------- #


def charger_index_prochain_groupe(compte: str | None = None) -> int:
    """Index (dans la liste des groupes actifs DU COMPTE concerné) du prochain
    groupe à traiter.

    Fichier absent/corrompu -> on repart de 0 (premier groupe) plutôt que de
    bloquer le run pour un problème d'état non critique. `compte` isole cet
    index par compte Facebook (voir index_prochain_groupe_path) - sinon les 5
    comptes partageraient la même rotation et tourneraient sur les groupes
    des autres.
    """
    chemin = index_prochain_groupe_path(compte)
    if not chemin.exists():
        return 0
    try:
        with chemin.open(encoding="utf-8") as f:
            return int(json.load(f).get("index", 0))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0


def sauvegarder_index_prochain_groupe(index: int, compte: str | None = None) -> None:
    chemin = index_prochain_groupe_path(compte)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    with chemin.open("w", encoding="utf-8") as f:
        json.dump({"index": index}, f)


# --------------------------------------------------------------------------- #
# Filtrage regex niveau 1 (marché foncier de Ouagadougou)
# --------------------------------------------------------------------------- #

# Mots-clés d'inclusion : présence d'AU MOINS UN de ces motifs = candidat potentiel.
# Regroupés par thème pour faciliter la maintenance.
_MOTS_FONCIER = [
    r"parcelle", r"terrain", r"lotissement", r"non\s+loti", r"zone\s+lotie",
    r"cession", r"hectares?", r"superficie",
]
_MOTS_DOCUMENT = [
    r"attestation", r"titre\s+foncier", r"\btf\b", r"puh", r"permis\s+d[' ]habiter",
    r"apfr", r"acte\s+de\s+cession", r"papier\s+en\s+r[eè]gle",
]
_MOTS_TRANSACTION = [
    r"\b[àa]\s+vendre\b", r"\bvente\b", r"\bvendre\b", r"\bc[ée]der\b", r"\bprix\b",
    r"n[ée]gociable",
]

MOTIF_FONCIER = re.compile(
    r"\b(" + "|".join(_MOTS_FONCIER + _MOTS_DOCUMENT + _MOTS_TRANSACTION) + r")\b",
    re.IGNORECASE,
)

# Unités de superficie ("ha", "m2", "m²") gérées à part avec une ancre sur le
# chiffre qui précède plutôt qu'un \b classique. Raison : dans l'écriture
# courante ("600m2", "5ha", sans espace), le chiffre et la lettre sont tous les
# deux des caractères "mot" pour le moteur regex -> \b ne trouve AUCUNE
# frontière entre eux et ne matche jamais. Idem pour \b après "²", qui n'est
# pas considéré comme un caractère "mot" par Python (donc jamais suivi d'une
# frontière valide devant un espace, lui aussi non-mot). Bug identifié et
# corrigé en relecture - non testé en conditions réelles (sandbox indisponible
# au moment de la génération), à confirmer sur un échantillon réel d'annonces.
MOTIF_SUPERFICIE_NUMERIQUE = re.compile(r"\d\s*(m2|m²|ha)(?=\D|$)", re.IGNORECASE)

# Recherches d'achat ("je cherche/recherche un terrain...") : à exclure de l'envoi au
# LLM car ce ne sont PAS des annonces de vente. Heuristique volontairement prudente :
# si le texte contient un verbe de recherche ET ne contient PAS de signal de vente
# explicite (souvent une recherche republie une annonce trouvée ailleurs), on exclut.
MOTIF_RECHERCHE_ACHAT = re.compile(
    r"\b(je\s+recherche|recherche\s+un[e]?|cherche\s+un[e]?|besoin\s+d[' ]un[e]?|"
    r"suis\s+preneur|qui\s+a\s+un[e]?\s+(terrain|parcelle)\s+[àa]\s+(vendre|proposer))\b",
    re.IGNORECASE,
)
MOTIF_SIGNAL_VENTE = re.compile(
    r"\b([àa]\s+vendre|vends|disponible\s+[àa]\s+la\s+vente)\b|prix\s*:?\s*\d+",
    re.IGNORECASE,
)

# Locations (à ne pas rejeter, mais à taguer - le foncier "vente" reste la cible
# métier principale ; laissé au LLM de trancher via `type_bien`/`resume_court`).
MOTIF_LOCATION = re.compile(r"\b(location|louer|loyer|bail)\b", re.IGNORECASE)

# Spam grossier détectable sans LLM (économie de coûts) : arnaques, contenus hors-sujet
# manifestes. Volontairement restreint pour limiter les faux positifs - un pattern trop
# large rejetterait de vraies annonces. À enrichir avec des cas réels observés.
#
# BUG CORRIGÉ (trouvé en exécutant réellement la suite de tests) : la version
# précédente incluait `whatsapp\s*:?\s*\+?\d{8,}.{0,5}$` pour détecter les posts
# qui ne sont QU'un numéro de téléphone (spam de contact). En pratique, la quasi-
# totalité des vraies annonces immobilières se terminent aussi par un numéro
# WhatsApp ("...Contact WhatsApp 70123456.") - ce motif rejetait donc la majorité
# des annonces légitimes (faux négatif massif, découvert par le test
# `test_separe_correctement_candidats_et_rejetes`). Supprimé plutôt que rafistolé :
# la présence d'un numéro en fin de texte n'est PAS un signal fiable de spam dans
# ce domaine métier précis.
MOTIF_SPAM = re.compile(
    r"(cliquez\s+ici|gagnez\s+\d|投资|forex\s+trading|crypto\s*(monnaie)?\s+gratuit)",
    re.IGNORECASE,
)


def est_candidat_foncier(texte: str) -> bool:
    """Étape A du filtrage : décide si un post mérite d'être envoyé au LLM.

    Règle : (mot-clé foncier présent) ET (pas de spam évident) ET
    (pas une recherche d'achat pure, sauf si un signal de vente cohabite -
    cas fréquent d'un post republié ambigu, laissé au LLM pour trancher).

    Limite connue : détection d'intention (achat vs vente) par regex est
    approximative. Des faux négatifs (annonces rejetées à tort) sont possibles
    sur des tournures inhabituelles. Pas de faux positifs coûteux en revanche,
    car l'étape B (LLM) revalide `est_une_annonce_valide`.
    """
    if not texte or not texte.strip():
        return False
    if MOTIF_SPAM.search(texte):
        return False
    if not (MOTIF_FONCIER.search(texte) or MOTIF_SUPERFICIE_NUMERIQUE.search(texte)):
        return False
    if MOTIF_RECHERCHE_ACHAT.search(texte) and not MOTIF_SIGNAL_VENTE.search(texte):
        return False
    return True


# --------------------------------------------------------------------------- #
# Quartiers / zones de Ouagadougou (normalisation)
# --------------------------------------------------------------------------- #

# Liste non exhaustive des quartiers/secteurs/communes couramment cités dans les
# annonces foncières à Ouagadougou. À COMPLÉTER au fil de l'eau : quand le LLM
# renvoie un `quartier_zone` absent de cette liste, il est conservé tel quel
# (voir processor.py) plutôt que forcé/déformé - on ne veut pas perdre
# d'information par excès de normalisation.
QUARTIERS_OUAGA = [
    "Ouaga 2000", "Karpala", "Pissy", "Saaba", "Komsilga", "Cissin", "Tanghin",
    "Gounghin", "Kossodo", "Nioko", "Bassinko", "Yagma", "Tampouy", "Zagtouli",
    "Kamboinsé", "Nongr-Massom", "Sig-Noghin", "Baskuy", "Bogodogo", "Boulmiougou",
    "Tanghin-Dassouri", "Koubri", "Loumbila", "Pabré", "Dapoya", "Zone du Bois",
    "Patte d'Oie", "Ouidi", "Kilwin", "Rimkiéta", "Yamtenga",
]

_QUARTIERS_NORMALISES = {q.lower(): q for q in QUARTIERS_OUAGA}


def normaliser_quartier(valeur: str | None) -> str | None:
    """Tente de faire correspondre un quartier libre à la liste normalisée.

    Correspondance stricte (insensible à la casse) uniquement - volontairement
    pas de fuzzy-matching (Levenshtein, etc.) pour éviter de fusionner à tort
    deux quartiers distincts. Si aucune correspondance, retourne la valeur
    d'origine nettoyée (pas de perte de donnée), à trier manuellement plus tard.
    """
    if not valeur:
        return None
    nettoye = valeur.strip()
    return _QUARTIERS_NORMALISES.get(nettoye.lower(), nettoye)


# --------------------------------------------------------------------------- #
# Statut du document foncier (normalisation)
# --------------------------------------------------------------------------- #

# Liste initiale constituée le 2026-08-03 à partir des valeurs RÉELLEMENT
# renvoyées par le LLM sur 224 annonces (pas une liste théorique/inventée) -
# à COMPLÉTER au fil de l'eau, même logique que QUARTIERS_OUAGA ci-dessus.
# "Attestation" et "Attestation d'attribution" sont volontairement gardées
# SÉPARÉES : rien ne garantit qu'un vendeur écrivant juste "attestation" veut
# dire "attestation d'attribution" plutôt qu'un autre type - les fusionner
# serait une supposition non vérifiée, pas une normalisation de casse/forme.
STATUTS_DOCUMENT = [
    "Titre foncier",
    "APFR",
    "PUH",
    "Attestation d'attribution",
    "Fiche d'attribution",
    "Attestation",
]

_STATUTS_DOCUMENT_NORMALISES = {s.lower(): s for s in STATUTS_DOCUMENT}


def normaliser_statut_document(valeur: str | None) -> str | None:
    """Tente de faire correspondre un statut de document libre à la liste
    normalisée - même logique et mêmes garanties que `normaliser_quartier`
    (correspondance stricte insensible à la casse, aucune perte de donnée
    sur une valeur non reconnue, pas de fuzzy-matching).
    """
    if not valeur:
        return None
    nettoye = valeur.strip()
    return _STATUTS_DOCUMENT_NORMALISES.get(nettoye.lower(), nettoye)


# --------------------------------------------------------------------------- #
# Paramètres de scraping / anti-détection (MODE MOBILE - 2026-08-29)
# --------------------------------------------------------------------------- #

# Ces valeurs peuvent être surchargées via les arguments CLI de main.py.
MAX_DAYS_BACK_DAILY = 1
MAX_DAYS_BACK_BACKFILL_DEFAULT = 7
GROUPS_BATCH_SIZE_DEFAULT = 5

# --------------------------------------------------------------------------- #
# MODE MOBILE (2026-08-29) : m.facebook.com + UA mobile + viewport mobile.
# Les comptes "mobile" ont une meilleure réputation pour le scraping que
# l'interface Comet desktop (web.facebook.com). On passe entièrement en mobile.
# --------------------------------------------------------------------------- #

WEB_FACEBOOK_BASE_URL = "https://m.facebook.com"
MOBILE_FACEBOOK_BASE_URL = "https://m.facebook.com"

# Conservé pour référence/historique uniquement.
MBASIC_BASE_URL = "https://mbasic.facebook.com"

# Playwright 1.47 embarque Chromium 129. Les cinq profils restent donc Android
# Chrome 129 et sont associés à un viewport précis. Un compte conserve le même
# profil entre ses groupes et ses runs ; aucune combinaison indépendante ne
# peut produire un appareil incohérent.
MOBILE_PROFILES = [
    {
        "nom": "Galaxy S23",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36"
        ),
        "viewport": {"width": 360, "height": 780},
    },
    {
        "nom": "Pixel 8",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 915},
    },
    {
        "nom": "Galaxy A53",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 13; SM-A536B) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36"
        ),
        "viewport": {"width": 360, "height": 800},
    },
    {
        "nom": "Redmi Note 11",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 12; Redmi Note 11) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36"
        ),
        "viewport": {"width": 393, "height": 873},
    },
    {
        "nom": "Galaxy S21",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; SM-G991B) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36"
        ),
        "viewport": {"width": 384, "height": 854},
    },
]

MOBILE_USER_AGENTS = [profil["user_agent"] for profil in MOBILE_PROFILES]
MOBILE_VIEWPORTS = [profil["viewport"] for profil in MOBILE_PROFILES]
MBASIC_USER_AGENT = MOBILE_PROFILES[0]["user_agent"]


def choisir_fingerprint_mobile(
    compte: str | None = None,
) -> tuple[str, dict[str, int]]:
    """Retourne le profil Android stable associé au compte."""
    if compte is None:
        index = 0
    else:
        if compte not in COMPTES_VALIDES:
            raise ValueError(f"Compte '{compte}' inconnu.")
        index = int(compte) - 1
    profil = MOBILE_PROFILES[index]
    return str(profil["user_agent"]), dict(profil["viewport"])


PROXY_GEO_CHECK_URL = "https://ip.decodo.com/json"
PROXY_GEO_CHECK_TIMEOUT_MS = 20_000


# Délais entre étapes de scroll (secondes) - plus variables / humains.
PAGE_DELAY_MIN_S = 4.0
PAGE_DELAY_MAX_S = 18.0

# Micro-pauses pendant un scroll multi-étapes (secondes).
SCROLL_MICRO_PAUSE_MIN_S = 0.3
SCROLL_MICRO_PAUSE_MAX_S = 1.8

# Temps de "lecture / réflexion" après chargement d'un groupe ou d'un batch de posts.
TEMPS_LECTURE_MIN_S = 8.0
TEMPS_LECTURE_MAX_S = 35.0

# Pause entre deux groupes consécutifs du même lot (batch).
PAUSE_ENTRE_GROUPES_MIN_S = 600.0  # 10 minutes
PAUSE_ENTRE_GROUPES_MAX_S = 900.0  # 15 minutes

# Pause entre deux LOTS (batches) de groupes.
PAUSE_ENTRE_BATCHES_MIN_S = 1200.0  # 20 minutes
PAUSE_ENTRE_BATCHES_MAX_S = 1800.0  # 30 minutes

MAX_PAGES_SANS_NOUVEAU_POST = 4  # arrêt du scroll si N étapes consécutives sans post inédit

# FILET DE SÉCURITÉ uniquement depuis l'introduction du repère de reprise
# (DERNIER_POST_CONNU_PATH, voir plus haut) : l'arrêt "normal" du scroll se
# fait maintenant en retrouvant le dernier post connu du run précédent, pas
# en comptant les scrolls. Ce plafond ne sert donc qu'à éviter un scroll
# infini dans deux cas limites : (1) le post-repère a été supprimé entre-temps
# par son auteur et ne sera donc jamais retrouvé, (2) tout premier run sur un
# groupe (aucun repère encore connu). Fixé à 100 (valeur choisie par
# l'utilisateur le 2026-08-15).
MAX_PAGES_ABSOLU = 100
NAVIGATION_TIMEOUT_MS = 30_000

# Fragments d'URL identifiant une requête GraphQL Facebook (pour intercepter
# les réponses réseau déclenchées par le scroll et y chercher des posts).
GRAPHQL_URL_FRAGMENTS = ["/api/graphql/"]

# Profondeur maximale de parcours récursif d'un blob JSON à la recherche de
# posts - protection contre un coût CPU excessif sur un payload très large
# et profondément imbriqué (observé : blobs de 170 Ko+, des centaines par page).
JSON_PROFONDEUR_MAX = 12

# Nombre d'échantillons bruts de réponses GraphQL matchées à conserver pour
# diagnostic quand un groupe termine son scroll sans avoir trouvé aucun post.
NB_ECHANTILLONS_DEBUG_GRAPHQL = 3

# --------------------------------------------------------------------------- #
# Stratégie anti-blocage : circuit breaker + budget de session
# --------------------------------------------------------------------------- #
#
# Le facteur qui a le plus d'impact réel sur le risque de blocage n'est PAS le
# code (délais, user-agent, etc.) mais l'infrastructure (réputation de l'IP/ASN
# du runner) et la confiance du compte utilisé - voir README.md, section
# "Stratégie anti-blocage". Ce qui suit ne compense pas ces facteurs, ça réduit
# seulement le risque évitable côté comportement.

# En cas de blocage détecté (checkpoint, mur anti-bot), on arrête TOUT le run
# immédiatement (pas seulement le groupe en cours) et on impose un délai de
# repos avant tout nouveau run - retenter aussitôt après un blocage est le
# signal le plus voyant possible pour un système anti-bot.
COOLDOWN_HEURES_APRES_BLOCAGE = 24
COOLDOWN_HEURES_APRES_SESSION_EXPIREE = 1  # probablement juste les cookies à renouveler, pas un blocage actif

# Durée maximale d'un run, tous groupes confondus.
SESSION_DUREE_MAX_MINUTES = 300

# --------------------------------------------------------------------------- #
# Throttle adaptatif (AIMD)
# --------------------------------------------------------------------------- #

NIVEAU_CONFIANCE_MIN = 0.2
NIVEAU_CONFIANCE_MAX = 1.0
NIVEAU_CONFIANCE_INITIAL = 1.0
NIVEAU_CONFIANCE_PALIER_SUSPICION = 0.5  # multiplicateur appliqué en cas de suspicion (decrease)
RUNS_PROPRES_POUR_RAMPUP = 3  # nb de runs propres consécutifs avant d'augmenter la confiance
RAMPUP_INCREMENT = 0.15  # augmentation additive, volontairement lente
RATIO_ANOMALIES_SUSPICION = 0.3  # >30% de groupes en erreur sur un run = signal de suspicion
COOLDOWN_MULTIPLICATEUR_MAX = 8  # plafonne le cooldown exponentiel (24h * 8 = 8 jours max)

# --------------------------------------------------------------------------- #
# Configuration LLM (structuration niveau 2)
# --------------------------------------------------------------------------- #
#
# OpenAI plutôt qu'Anthropic (changement demandé le 2026-08-01, remplacement
# complet - plus de dépendance anthropic dans requirements.txt). gpt-4o-mini
# retenu : c'est le moins cher des modèles OpenAI supportant les Structured
# Outputs au moment du choix ($0.15/1M tokens entrée, $0.60/1M sortie -
# comparé à gpt-4.1-mini à $0.40/$1.60, ~2.7x plus cher, sans intérêt ici vu
# la taille d'un post Facebook), rôle équivalent à claude-3-5-haiku utilisé
# avant. Prix vérifiés via recherche web le 2026-08-01, à revérifier
# périodiquement (les tarifs LLM changent souvent).
OPENAI_MODEL = "gpt-4o-mini"
OPENAI_TEMPERATURE = 0.0
OPENAI_MAX_TOKENS = 1024
LLM_MAX_CONCURRENCE = 5  # requêtes simultanées max (throttling coût + rate limits)
LLM_MAX_RETRIES = 3
LLM_BACKOFF_BASE_S = 2.0

TYPES_BIEN_VALIDES = ["parcelle", "maison", "villa", "ferme", "autre"]

# Schéma JSON envoyé à l'API OpenAI via Structured Outputs (response_format
# json_schema, strict=True) pour forcer une sortie JSON garantie conforme au
# schéma (plus robuste que de parser un bloc de texte libre en JSON) - même
# principe que le "tool use" utilisé avec l'API Claude précédemment.
#
# Contraintes du mode strict OpenAI (différentes d'Anthropic) : TOUTES les
# propriétés doivent figurer dans "required" (l'optionnalité se représente
# par un type nullable `["string", "null"]`, pas par absence de la clé), et
# "additionalProperties": false est obligatoire à chaque niveau d'objet.
SCHEMA_ANNONCE_PROPRIETES = {
    "est_une_annonce_valide": {
        "type": "boolean",
        "description": (
            "true si c'est une vraie annonce de vente d'un bien immobilier/foncier "
            "à Ouagadougou ou environs ; false si spam, recherche d'achat, "
            "hors-sujet ou contenu incompréhensible."
        ),
    },
    "type_bien": {
        "type": "string",
        "enum": TYPES_BIEN_VALIDES,
    },
    "quartier_zone": {
        "type": ["string", "null"],
        "description": "Quartier/secteur/commune mentionné, tel qu'écrit dans le texte.",
    },
    "superficie_m2": {
        "type": ["integer", "null"],
        "description": "Superficie convertie en m² (1 ha = 10000 m²). null si absente.",
    },
    "prix_fcfa": {
        "type": ["integer", "null"],
        "description": "Prix en FCFA, sans séparateurs. null si absent ou 'non précisé'.",
    },
    "statut_document": {
        "type": ["string", "null"],
        "description": "Ex: Attestation, Titre Foncier, PUH, Permis d'habiter, APFR.",
    },
    "contacts_whatsapp": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Numéros de téléphone/WhatsApp mentionnés, format brut.",
    },
    "mots_cles_pertinents": {
        "type": "array",
        "items": {"type": "string"},
    },
    "resume_court": {
        "type": "string",
        "description": "Résumé en une phrase (max ~25 mots), en français.",
    },
}

SCHEMA_ANNONCE_JSON_SCHEMA = {
    "name": "structurer_annonce_fonciere",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": SCHEMA_ANNONCE_PROPRIETES,
        "required": list(SCHEMA_ANNONCE_PROPRIETES.keys()),
        "additionalProperties": False,
    },
}

PROMPT_SYSTEME_LLM = (
    "Tu es un extracteur de données structurées spécialisé dans le marché foncier de "
    "Ouagadougou (Burkina Faso). Tu reçois le texte brut d'un post Facebook et tu dois "
    "répondre avec les champs extraits, au format JSON demandé. "
    "Règles strictes : ne devine JAMAIS une valeur absente du texte (mets null) ; "
    "ne convertis pas approximativement un prix ou une superficie ambigus, laisse null ; "
    "si le post est une recherche d'achat, du spam, ou non lié à l'immobilier/foncier "
    "de la région de Ouagadougou, mets `est_une_annonce_valide` à false."
)
