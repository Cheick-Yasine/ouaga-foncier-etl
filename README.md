# Pipeline ETL — Annonces foncières Ouagadougou (Facebook → base PostgreSQL + Excel)

Projet académique. Collecte des posts de groupes Facebook ciblés, filtrage
regex local (gratuit), puis structuration via l'API Claude, upsert dans une
base maître PostgreSQL unique (+ vue Excel régénérée à chaque run). Deux modes :
`daily` (dernières 24h, cron GitHub Actions 23h00 UTC) et `backfill`
(rattrapage historique paramétrable par lots).

## ⚠️ Risques et limites à lire avant utilisation

Ce projet a été généré en une seule session, **sans accès réseau vers
facebook.com** et avec un **sandbox d'exécution indisponible pendant une
grande partie du développement** (voir section Tests). Les points suivants
ne sont donc PAS vérifiés en conditions réelles et doivent être validés par
vous avant tout usage sérieux :

1. **Conformité Facebook** : l'automatisation de la navigation avec une
   session authentifiée (cookies) contrevient aux CGU de Meta. Risques
   réels : bannissement du compte utilisé, blocage IP/fingerprint du runner
   GitHub Actions, exposition légale (les posts contiennent des numéros de
   téléphone de tiers). Utilisez un compte dédié, jamais votre compte
   personnel principal. Ceci reste un choix technique risqué assumé dans le
   cadre académique du projet — évaluez si une alternative (API officielle,
   service tiers comme Apify) est acceptable pour votre cas d'usage avant
   de lancer le pipeline en production.
2. **Sélecteurs DOM de `scraper.py` non vérifiés en live.** Le scraper cible
   maintenant `mbasic.facebook.com` (HTML léger, server-rendered) plutôt que
   le Facebook standard - DOM historiquement plus stable, mais toujours pas
   vérifié contre une session réelle (aucun accès réseau à facebook.com
   depuis mon environnement). `SELECTEURS` dans `scraper.py` s'appuie sur des
   patterns publiquement documentés pour mbasic (`data-ft`, liens "Voir
   plus"), pas sur une observation live. Testez avec `--group-limit 1
   --skip-llm` avant tout run sérieux.
3. **Extraction de l'horodatage : logique implémentée et testée, format
   textuel non vérifié.** `_parser_horodatage_relatif` (fonction pure, 15
   tests unitaires) convertit correctement "3 h", "Hier à 14:30", "1 août
   2025", etc. en date absolue - ce qui manquait totalement dans la version
   précédente de ce module (qui ciblait le Facebook standard, sans horodatage
   exploitable). Ce qui reste incertain : je ne sais pas avec certitude que
   ce sont exactement les chaînes que mbasic affiche en 2026 avec une locale
   fr-FR - à confirmer sur un run réel. Tant que ce n'est pas confirmé, un
   format non reconnu retourne `None` (`date_incertaine=True`) plutôt que de
   deviner, donc aucun risque de date silencieusement fausse - au pire, le
   critère d'arrêt basé sur `days_back` ne se déclenche pas sur ce post-là.
4. **`groups.csv` contient les 15 groupes réels** issus de `Groupe.csv`
   (converti par vous depuis `Groupe.xlsx`). Deux points à valider :
   - Ligne `1412949025757240` ("VENTE ET ACHAT A OUAGADOUGOU") est marquée
     `Private` — pour qu'un groupe privé soit scrapable, le compte associé
     à `FB_COOKIES_JSON` doit **déjà être membre approuvé**. Le scraper ne
     détecte pas spécifiquement ce cas ("demander à rejoindre" au lieu de
     "checkpoint") — à vérifier manuellement.
   - Ligne `352566539534344` ("Vente de parcelles et non lotis a Tenkodogo")
     mise à `actif=false` : Tenkodogo est une autre ville (≈200 km de
     Ouagadougou, chef-lieu du Boulgou), hors périmètre du projet tel que
     décrit. Je ne l'ai pas supprimée, juste désactivée — repassez-la à
     `true` si vous voulez quand même la scraper. La ligne `331961108512130`
     ("... partout au Burkina") reste active mais couvre plus large que la
     seule Ouagadougou, à vous de juger.
5. **Divergence dans le cahier des charges** : Python 3.11 est mentionné
   dans le bloc "Architecture" et Python 3.12.7 dans le bloc "Workflow
   GitHub Actions". J'ai retenu **3.12.7** dans le workflow (la mention la
   plus précise) — à confirmer.
6. **Suite de tests désormais exécutée réellement** (134/134 au 2026-08-01,
   voir section Tests) — mais uniquement la logique pure. Le scraping live
   contre facebook.com n'a toujours pas pu être testé (pas d'accès réseau
   vers Facebook depuis mon environnement).
7. **DATABASE_URL en CI = Neon (Postgres serverless, tier gratuit).**
   Migration SQLite → PostgreSQL faite à votre demande. Votre instance
   PostgreSQL locale n'étant pas joignable depuis un runner GitHub Actions
   (machine éphémère dans le cloud, pas sur votre réseau), vous avez choisi
   Neon plutôt qu'un tunnel ou un runner self-hosted. Voir la section
   "Base de données" ci-dessous pour le dimensionnement (le tier gratuit
   0,5 Go / 100 CU-heures est largement suffisant pour ce volume de données)
   et les étapes de configuration.

## Groupes (`groups.csv`)

`config.py` ne contient AUCUN groupe en dur — tout est chargé depuis
`groups.csv` à la racine du projet (colonnes requises : `id,nom,url,actif` ;
`confidentialite` et `membres` sont informatifs, ignorés par le loader).

Rempli à partir de `Groupe.csv` (export de `Groupe.xlsx`) : **15 groupes**
(pas 25 — c'est le nombre réel trouvé dans votre fichier). Les `id` viennent
des URLs (`facebook.com/groups/<id>/`), pas de la colonne numérique du CSV
source qui était en notation scientifique (`5,89699E+14`) et avait donc
perdu des chiffres. Les noms ont été décodés depuis les entités HTML
(`&amp;`, `&#xe0;`, etc.) et les emoji retirés (bruit visuel, aucune perte
d'information). Voir la section "Risques et limites" ci-dessus pour les
2 lignes qui méritent une relecture (groupe privé, groupe hors Ouagadougou).

## Installation

```bash
python -m venv .venv && source .venv/bin/activate  # ou .venv\Scripts\activate sous Windows
pip install -r requirements.txt
playwright install --with-deps chromium
cp .env.example .env  # puis renseignez FB_COOKIES_JSON, ANTHROPIC_API_KEY et DATABASE_URL
```

`FB_COOKIES_JSON` : export des cookies de session Facebook au format JSON
(liste d'objets `{"name", "value", "domain", ...}` compatible Playwright).
Généralement obtenu via une extension de navigateur d'export de cookies,
sur un compte dédié au scraping.

`DATABASE_URL` : chaîne de connexion PostgreSQL (`postgresql://user:password@hote:5432/base`).
Voir la section "Base de données" ci-dessous.

## Base de données

La base maître est **PostgreSQL**, hébergée sur **Neon** (tier gratuit).
`processor.py` s'y connecte via `psycopg` (v3) et crée son propre schéma au
premier lancement (`CREATE TABLE IF NOT EXISTS`, voir `SCHEMA_SQL` dans
`processor.py`) — rien à exécuter manuellement, ni sur Neon ni ailleurs.

**Pourquoi Neon plutôt que le Postgres local** : le job `scrape-and-process`
tourne sur un runner GitHub Actions hébergé dans le cloud, qui ne peut pas
atteindre une base qui n'écoute que sur le réseau local de l'utilisateur.
Neon est joignable depuis Internet nativement, sans tunnel ni runner
self-hosted à maintenir.

**Dimensionnement du tier gratuit (vérifié sur neon.com/pricing, 2026-08-01)**
— 0,5 Go de stockage et 100 CU-heures de calcul par mois, mise en veille
après 5 min d'inactivité (sans coût). Calcul de marge pour ce projet : même
avec une hypothèse haute de 50 annonces valides/jour et ~2 Ko/ligne (texte +
métadonnées), la table `annonces` grossit d'environ 3 Mo/mois — le tier
gratuit couvre donc plusieurs années à ce rythme, largement avant que le
stockage devienne un problème. Le calcul (CU-heures) est utilisé quelques
minutes par jour lors du run CI : très loin des 100 CU-heures/mois inclus.
Seule contrepartie réelle : la mise en veille après 5 min ajoute un léger
délai de "réveil" à la première connexion de chaque run (de l'ordre de la
seconde) — sans impact pour un job batch quotidien comme celui-ci.

**Configuration** :
1. Créez un compte sur [neon.com](https://neon.com), puis un projet (ex :
   `ouaga-foncier-etl`).
2. Copiez la chaîne de connexion fournie dans le dashboard Neon (format
   `postgresql://user:password@ep-xxxx.region.aws.neon.tech/dbname?sslmode=require`).
3. En local : collez-la dans `DATABASE_URL=` de votre `.env`.
4. En CI : ajoutez-la comme secret `DATABASE_URL` du repo GitHub
   (`Settings > Secrets and variables > Actions`).

Si `DATABASE_URL` n'est pas configurée ou devient injoignable, le job
`scrape-and-process` échoue à la connexion (`psycopg.OperationalError`,
capturé par `main.py` → code de sortie 1). C'est un échec explicite et
volontaire plutôt qu'un run silencieux qui perdrait des données.

## Utilisation

```bash
# Mode quotidien (dernières 24h, tous les groupes actifs)
python main.py --mode daily

# Rattrapage sur 14 jours, 5 groupes max, lots de 3
python main.py --mode backfill --days-back 14 --group-limit 5 --batch-size 3

# Test du scraping + filtrage regex uniquement, sans consommer l'API Claude
python main.py --mode daily --skip-llm
```

Sorties :
- **Base maître PostgreSQL (`DATABASE_URL`), source de vérité.** Mise à jour
  par upsert (`id` de post) à chaque run - jamais de doublon, jamais écrasée.
  Contient aussi la table `runs` (historique de volume, utilisée par la
  détection de dérive). Ce n'est plus un fichier local (voir section
  "Base de données" ci-dessus).
- `data/processed/annonces.xlsx` : vue Excel régénérée à chaque run depuis la
  base maître - un seul fichier, toujours à jour, à ouvrir directement.
- `data/processed/annonces_<horodatage>.csv` / `rejetes_<horodatage>.json` :
  trace d'audit ponctuelle du run (candidats valides / rejetés + motif) - la
  base maître reste la référence pour toute analyse.
- `data/raw/` : posts bruts par groupe (sauvegarde incrémentale)
- `data/state/seen_post_ids.json` : déduplication entre runs quotidiens
- `data/state/storage_state.json` : localStorage du navigateur, réutilisé au run
  suivant pour un fingerprint plus cohérent (voir "Stratégie anti-blocage")
- `data/state/cooldown_until.json` : présent uniquement si un blocage a été
  détecté — bloque les runs suivants jusqu'à expiration
- `data/state/sante_scraper.json` : score de confiance du throttle adaptatif
  (voir "Stratégie anti-blocage")
- `data/logs/pipeline.log` : logs

## Stratégie anti-blocage

Demande explicite du projet : être stratégique pour ne pas se faire bloquer.
Honnêteté d'abord — **le code n'est pas le facteur qui pèse le plus** dans le
risque de blocage. Par ordre d'impact réel estimé :

1. **Réputation de l'IP/ASN du runner.** Les runners GitHub Actions hébergés
   tournent sur des plages IP Azure/GitHub connues et publiques, que Meta
   peut trivialement classer comme trafic datacenter — indépendamment de tout
   ce qui est fait côté navigateur. Aucune astuce de fingerprint ne compense
   ça. Options réelles si ça devient un problème : un **runner self-hosted**
   sur une connexion résidentielle/bureau (changement d'infra, pas de code),
   ou accepter le risque tel quel pour un usage académique à faible volume.
   Je n'ai pas mis en place de rotation de proxies pour contourner ça — voir
   plus bas pourquoi.
2. **Confiance du compte Facebook utilisé.** Un compte ancien, actif
   organiquement, avec un historique normal, déclenche beaucoup moins de
   vérifications qu'un compte neuf ou inactif qui se met soudain à naviguer
   30 groupes d'affilée. Ça ne se corrige pas en code.
3. **Volume et vitesse.** Nombre de groupes/posts par run, fréquence des
   runs. C'est le seul axe entièrement pilotable par la config (`--group-limit`,
   `--batch-size`, `SESSION_DUREE_MAX_MINUTES`).
4. **Comportement observable** (délais, pauses, reprise après incident) —
   c'est ce que ce code peut réellement influencer, implémenté ci-dessous.

**Ce qui est implémenté (comportemental, code) :**
- Délais aléatoires 2–5s entre scrolls, 15–45s entre groupes, pause humaine
  entre groupes d'un même batch (`config.py`).
- **Circuit breaker** : un blocage détecté (`BlocageDetecteError`) arrête
  **tout le run immédiatement** (plus aucun autre groupe n'est tenté) et
  écrit un cooldown de 24h dans `data/state/cooldown_until.json` — tout run
  suivant se termine immédiatement tant que le cooldown est actif
  (`scraper.CooldownActifError`, code de sortie 0 en CI pour ne pas polluer
  l'historique de succès/échec avec un mécanisme de sécurité qui fonctionne).
  Une session expirée déclenche un cooldown plus court (6h) : c'est
  probablement juste les cookies à renouveler, pas un blocage actif.
- **Budget de session** : `SESSION_DUREE_MAX_MINUTES=45` — un run s'arrête
  proprement au-delà, les groupes restants passent au run suivant plutôt que
  de pousser une session marathon.
- **Fingerprint plus cohérent d'un run à l'autre** : le `localStorage` du
  navigateur (`storage_state`) est sauvegardé en fin de run et réutilisé au
  suivant (via cache GitHub Actions, voir le workflow), plutôt que de
  repartir d'un navigateur vierge chaque jour. Les cookies, eux, viennent
  toujours de `FB_COOKIES_JSON` (source de vérité).
- **Échauffement de session** : la première action de chaque run est une
  visite de `mbasic.facebook.com/`, pas un saut direct dans un groupe.
- Masquage du flag `navigator.webdriver` (une ligne, technique documentée
  publiquement, pas une suite de contournement).
- **Jitter temporel** : délai aléatoire de 0 à 20 min avant démarrage sur le
  déclenchement cron, pour éviter un trafic à heure fixe à la seconde près.
- **Throttle adaptatif (AIMD)** : un score de confiance persistant
  (`data/state/sante_scraper.json`, 0.2 à 1.0) ajuste automatiquement délais
  et volume de groupes traités d'un run à l'autre, sans intervention
  manuelle - même logique que le contrôle de congestion TCP (diminution
  multiplicative rapide au moindre signal négatif, augmentation additive
  lente seulement après plusieurs runs propres consécutifs) :
  - Un blocage fait chuter la confiance au plancher (délais x5, un cinquième
    des groupes seulement) ET double le multiplicateur de cooldown (24h,
    48h, 96h... jusqu'à 8 jours max si les blocages se répètent).
  - Une session expirée (probablement juste des cookies à renouveler)
    réduit la confiance plus légèrement.
  - Si plus de 30% des groupes d'un run lèvent une erreur inattendue
    (signal de suspicion plus discret qu'un blocage explicite), la
    confiance est divisée par deux.
  - Après 3 runs consécutifs sans aucun signal négatif, la confiance
    remonte lentement (+0.15) et le multiplicateur de cooldown est
    réinitialisé.
  C'est un mécanisme défensif auto-régulé (il ralentit tout seul face à
  l'incertitude), pas une technique pour déjouer Facebook plus finement -
  cohérent avec ce qui est refusé ci-dessous.
- **Tests obligatoires avant tout scraping en CI** : le workflow GitHub
  Actions a un job `tests` séparé (`needs: tests` sur le job de scraping) -
  si `pytest` échoue, le run ne touche même pas à Facebook. Ce n'est pas de
  l'anti-détection à proprement parler, mais ça évite qu'un bug de code soit
  découvert via un run réel gaspillé (ou pire, faussement interprété comme
  un blocage par le circuit breaker).

**Ce qui n'est délibérément PAS implémenté, et pourquoi :**
- Rotation de proxies résidentiels pour masquer l'IP du runner.
- Randomisation poussée de fingerprint (canvas/WebGL/audio), suites de type
  "stealth plugin".
- Contournement de CAPTCHA.
- Multi-comptes pour répartir la charge.

Ces techniques existent et sont documentées publiquement, mais je ne les
implémente pas ici : elles font basculer le projet d'une "automatisation
raisonnablement mesurée d'une session déjà authentifiée" vers une
infrastructure conçue spécifiquement pour déjouer les systèmes de sécurité
d'une plateforme à plus grande échelle — ce que je préfère ne pas construire,
même dans un cadre académique. Si le circuit breaker se déclenche souvent
malgré tout, c'est un signal qu'il faut réduire le volume ou l'infra
(runner self-hosted), pas ajouter une couche d'évasion.

## Tests

```bash
pip install -r requirements.txt

# Les tests de tests/test_processor_db.py ont besoin d'une base PostgreSQL de
# test dédiée (jamais votre DATABASE_URL de production - le fichier fait des
# DROP TABLE entre chaque test). Sans TEST_DATABASE_URL, ces tests sont
# automatiquement skippés plutôt qu'en échec (voir conftest.py).
export TEST_DATABASE_URL="postgresql://postgres:motdepasse@localhost:5432/ouaga_foncier_etl_test"
pytest -q
```

**État réel (mis à jour 2026-08-01) : 134/134 tests passent réellement**
(`pytest -q`, exécuté pour de vrai, pas juste écrit — y compris les tests de
base de données, contre un vrai serveur PostgreSQL 16 local via `pgserver`).
Couverture : Étape A (regex, 100% hors-ligne), Étape B (API Claude entièrement
mockée — aucun test ne consomme de crédits API), base maître PostgreSQL
(upsert, export Excel, détection de dérive, isolation par TRUNCATE entre
tests), throttle adaptatif AIMD (15 tests, purement arithmétique), parseur
d'horodatage mbasic (15 tests, désormais entièrement testable contrairement à
l'ancienne extraction jamais implémentée), CLI (`main.py`), cooldown/circuit-
breaker. Les parties de `scraper.py` qui pilotent un vrai navigateur
(`creer_navigateur`, `scraper_groupe`, `extraire_posts_visibles`,
`echauffement`, `_extraire_lien_page_suivante`) ne sont PAS testées
automatiquement — elles nécessitent une vraie session Playwright/mbasic.facebook.com,
toujours inaccessible depuis mon environnement. Le workflow GitHub Actions
bloque désormais le scraping si cette suite échoue (job `tests` séparé, avec
un service Postgres éphémère dédié — voir "Stratégie anti-blocage" et
`.github/workflows/daily_scraper.yml`).

**Bugs réels trouvés en exécutant la suite** (pas juste en la lisant) :
- `anthropic==0.34.2` (version initialement choisie sans pouvoir la tester)
  est incompatible avec `httpx>=0.28` (`TypeError` à l'instanciation du
  client) → remonté à `anthropic==0.69.0`.
- Le client Anthropic plante à l'instanciation dès qu'un proxy SOCKS est
  présent dans l'environnement (variable `ALL_PROXY`) sans le paquet
  `socksio` → ajouté à `requirements.txt`.
- `MOTIF_SPAM` rejetait la quasi-totalité des vraies annonces : le motif
  censé détecter le spam "juste un numéro de téléphone" matchait aussi
  n'importe quelle annonce légitime se terminant par un contact WhatsApp
  (le cas normal). Supprimé (voir commentaire dans `config.py`).
- `python-dotenv` était déclaré dans `requirements.txt` mais `load_dotenv()`
  n'était appelé nulle part dans `main.py` — le fichier `.env` local n'était
  donc jamais lu en pratique (trouvé en relisant le code pendant la migration
  PostgreSQL, pas via un test automatisé — aucun test n'exerçait le point
  d'entrée `main.py` avec un vrai `.env` sur disque). Corrigé : l'appel se
  fait maintenant tout en haut de `main.py`, avant `import config` (nécessaire
  car `config.py` lit `DATABASE_URL` depuis l'environnement dès l'import).

Avant l'accès au sandbox, une relecture manuelle avait déjà trouvé et corrigé
2 bugs regex distincts (`\bha\b`/`\bm2\b` ne matchant jamais un chiffre collé
à l'unité, `prix\s*:?\s*\d` tronqué) — cette double vérification (relecture +
exécution réelle) est ce qui a permis de tout corriger avant livraison plutôt
qu'après. **Cela dit, rien de tout ça ne teste le scraping Facebook réel** :
relancez `pytest -q` après toute modification, et testez `scraper.py` en
conditions réelles avec prudence (voir "Limite technique importante"
ci-dessus, sélecteurs non vérifiés en live).

## Structure

```
ouaga-foncier-etl/
├── config.py              # mots-clés, quartiers, délais, chargement des groupes
├── scraper.py             # Playwright async (mbasic), pagination, throttle adaptatif
├── processor.py           # filtrage regex + structuration API Claude + base maître PostgreSQL
├── main.py                # orchestrateur CLI
├── groups.csv             # liste des groupes (15, voir "Risques et limites")
├── requirements.txt
├── pytest.ini
├── .env.example
├── .github/workflows/daily_scraper.yml   # job tests (+ service Postgres) -> job scraping (bloquant)
└── tests/
    ├── test_config.py            # regex de filtrage, quartiers, groupes
    ├── test_processor_filter.py  # Étape A
    ├── test_processor_llm.py     # Étape B (API mockée)
    ├── test_processor_db.py      # base maître PostgreSQL, dérive de volume
    ├── test_scraper_helpers.py   # horodatage, cooldown, throttle adaptatif
    └── test_main_cli.py          # CLI, codes de sortie
```

## GitHub Actions

Secrets requis dans le repo (`Settings > Secrets and variables > Actions`) :
- `FB_COOKIES_JSON`
- `ANTHROPIC_API_KEY`
- `DATABASE_URL` — doit être joignable depuis un runner hébergé par GitHub,
  pas seulement depuis votre réseau local (voir section "Base de données").

Le job `tests` n'a besoin d'aucun de ces secrets : il utilise un service
PostgreSQL éphémère propre à la CI (`postgres:16`, détruit à la fin du job),
totalement indépendant de la base maître de production.

Déclenchement automatique quotidien à 23h00 UTC, ou manuel via l'onglet
Actions (`workflow_dispatch`) avec choix du mode/days_back/group_limit/batch_size.
