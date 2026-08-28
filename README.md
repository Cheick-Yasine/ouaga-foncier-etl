# Pipeline ETL — Annonces foncières Ouagadougou

Scraping de groupes Facebook ciblés → filtrage local → structuration par LLM → base PostgreSQL + export Excel. Projet académique.

## Description technique

- **Langage** : Python 3.12 (3.12.7 en CI).
- **Scraping** : Playwright (Chromium, async), session authentifiée via cookies (`FB_COOKIES_JSON`, ou `FB_COOKIES_JSON_1`…`FB_COOKIES_JSON_5` en multi-comptes — voir section "Multi-comptes"), cible `web.facebook.com` (interface "Comet").
- **Filtrage** : regex locales, gratuites, aucun appel API (`config.py`).
- **Structuration** : API OpenAI (`gpt-4o-mini`), Structured Outputs (schéma JSON strict) pour extraire les champs (type de bien, quartier, superficie, prix, statut du document, contacts).
- **Stockage** : PostgreSQL (hébergé sur Neon), upsert par `id` de post — jamais de doublon. Export Excel régénéré à chaque run.
- **Orchestration** : GitHub Actions, cron quotidien + déclenchement manuel.

## Fonctionnement

Le pipeline s'exécute en 4 étapes, orchestrées par `main.py` :

1. **Scraping** (`scraper.py`) — pour chaque groupe actif de `groups.csv` : ouverture de `web.facebook.com/groups/<id>`, extraction des posts "mis en avant" (JSON embarqué dans la page), puis scroll simulé avec interception des réponses réseau GraphQL pour récupérer le fil normal du groupe. S'arrête par groupe quand la fenêtre de dates (`--days-back`) est dépassée ou après plusieurs scrolls sans nouveau post.
2. **Filtrage** (`processor.py`, étape A) — chaque post brut passe par des regex (mots-clés fonciers, exclusion des recherches pures et du spam) pour ne garder que les candidats plausibles, sans coût API.
3. **Structuration LLM** (`processor.py`, étape B) — chaque candidat est envoyé à l'API OpenAI, qui renvoie une structure validée (Pydantic) ou rejette le post s'il ne s'agit pas d'une vraie annonce. Les champs `quartier_zone` et `statut_document` sont ensuite normalisés (casse) contre une liste connue.
4. **Persistance** (`processor.py`) — upsert des annonces valides dans PostgreSQL, mise à jour de l'export Excel, détection de dérive de volume (alerte si un run quotidien produit anormalement peu de résultats vs l'historique).

Deux modes d'exécution (`--mode`) :
- `daily` : dernières 24h (`--days-back 1`), déclenché automatiquement **deux fois par jour, à 00h00 et 12h00 UTC** (GitHub Actions), avec un délai aléatoire de 0 à 30 min avant démarrage sur les déclenchements cron (pas d'horaire parfaitement fixe et prévisible côté Facebook).
- `backfill` : rattrapage historique, `--days-back` réglable (déclenchement manuel uniquement, `workflow_dispatch`).

Deux stratégies de répartition des groupes au sein d'un run :
- **Par défaut** : tous les groupes actifs (du compte concerné) sont traités d'affilée dans la même session, avec des pauses longues entre eux (voir "Stratégie anti-blocage").
- **`--round-robin --groups-per-run N`** : seuls N groupes sont traités à ce run, en rotation (index persistant, isolé par compte). Pensé pour un cron fréquent : l'espacement entre deux passages sur un même groupe vient alors de l'espacement entre les runs, pas d'une pause interne. Exposé aussi en déclenchement manuel (`round_robin`, `groups_per_run`).

## Base de données

Base maître PostgreSQL (source de vérité), hébergée sur **Neon** (tier gratuit), atteinte via la chaîne de connexion `DATABASE_URL` (secret GitHub en CI, `.env` en local) :

```
postgresql://user:password@ep-xxxx.region.aws.neon.tech/dbname?sslmode=require
```

Schéma créé automatiquement au premier run (`processor.SCHEMA_SQL`, `CREATE TABLE IF NOT EXISTS`) :

- `annonces` — une ligne par annonce, clé primaire `id` (id du post Facebook). Écriture en `INSERT ... ON CONFLICT (id) DO UPDATE` : une annonce revue lors d'un run ultérieur est mise à jour, jamais dupliquée, et sa date de `premiere_collecte` est préservée (seul `derniere_maj` bouge). Champs : `groupe_nom`, `url`, `date_publication`, `date_incertaine`, `type_bien`, `quartier_zone`, `superficie_m2`, `prix_fcfa`, `statut_document`, `contacts_whatsapp`, `mots_cles_pertinents`, `resume_court`, `texte_nettoye`, `premiere_collecte`, `derniere_maj`.
- `runs` — un historique par exécution (`horodatage` en clé primaire, `mode`, `nb_posts_bruts`, `nb_candidats`, `nb_valides`), utilisé par la détection de dérive de volume.

Points à connaître :
- La base et l'export `data/processed/annonces.xlsx` sont **partagés par les 5 comptes** : l'upsert se fait par `id` de post, peu importe quel compte a scrapé le post.
- Aucun repli silencieux sur une base locale : si `DATABASE_URL` est absente ou le serveur injoignable, le run échoue bruyamment (`psycopg.OperationalError`) plutôt que d'écrire dans le vide.
- Le workflow tente en fin de run un `pg_dump` de la base dans l'artefact (sauvegarde optionnelle, `continue-on-error`) — voir les commentaires de `.github/workflows/daily_scraper.yml` sur le mismatch de version `pg_dump`/Neon rencontré en réel.
- Les mots de passe sont masqués (`main._masquer_dsn`) avant tout affichage dans les logs ou le résumé GitHub Actions.
- **`TEST_DATABASE_URL`** (tests uniquement) doit pointer sur une base **séparée** : la suite fait des `DROP TABLE` entre les tests. Absente → les tests concernés sont automatiquement skippés (voir "Tests").

## Stratégie anti-blocage

Le facteur qui pèse le plus sur le risque de blocage n'est pas le code mais l'infrastructure (réputation de l'IP/ASN du runner) et la confiance du compte utilisé. Ce qui suit ne compense pas ces facteurs, ça réduit seulement le risque évitable côté comportement (détail et justifications dans les commentaires de `config.py` et `scraper.py`) :

- **Pauses longues et aléatoires** : 10-15 min entre deux groupes (`PAUSE_ENTRE_GROUPES_MIN_S`/`MAX_S`), 20-30 min entre deux lots de `--batch-size` groupes (`PAUSE_ENTRE_BATCHES_*`).
- **Throttle adaptatif** : un score de confiance par compte (`NIVEAU_CONFIANCE_*`, persisté dans `sante_scraper.json`) module délais et volume traité ; il est divisé (`NIVEAU_CONFIANCE_PALIER_SUSPICION`) dès qu'un signal de suspicion est détecté.
- **Circuit breaker** : blocage anti-bot détecté (`BlocageDetecteError`) → arrêt de TOUT le run + cooldown de `COOLDOWN_HEURES_APRES_BLOCAGE` = 24h. Session invalidée (`SessionExpireeError`) → arrêt + cooldown de `COOLDOWN_HEURES_APRES_SESSION_EXPIREE` = 1h, le temps de régénérer les cookies (voir section suivante).
- **Budget de session** : `SESSION_DUREE_MAX_MINUTES` = 300, sous la limite dure de 360 min d'un job GitHub Actions (voir le log "Budget de session atteint"), pour laisser 60 min de marge aux étapes hors scraping.
- **Sérialisation par compte** : `concurrency` du workflow par `matrix.compte` — deux runs concurrents sur la même session Facebook multiplieraient le risque de détection ; deux comptes distincts n'ont aucune raison de s'attendre.
- **Cooldown et état isolés par compte** : un blocage sur un compte ne gèle pas les 4 autres.

Un cooldown actif fait sortir le run en code 0 (`CooldownActifError`) : c'est le mécanisme de sécurité qui fonctionne, pas un échec de workflow.

## Proxy (réputation IP/ASN)

Constat récurrent en CI : un run échoue avec `SessionExpireeError` dès la toute première requête (signature `USER_ID`/`actorID` à `0` dans les logs — voir `detecter_blocage_ou_session_expiree` dans `scraper.py`) **même juste après une régénération complète des cookies** (export frais + `scripts/maj_cookies.py --set-secret --clear-actions-cache`, donc ni cache local ni cache `actions/cache` en cause). Dans ce cas, le problème n'est pas le cookie mais l'IP d'où part la requête : un runner GitHub Actions utilise une IP de datacenter, jamais vue par le compte auparavant, que le système de risque de Facebook peut invalider instantanément côté serveur — indépendamment de la fraîcheur du cookie. C'est exactement la limite déjà annoncée dans la section "Stratégie anti-blocage" ci-dessus : aucune mesure de comportement ne compense une mauvaise réputation d'IP/ASN.

Un proxy (résidentiel ou mobile de préférence — un proxy datacenter n'apporte rien de plus que l'IP du runner) fait sortir la session Facebook par une IP à réputation plus proche d'un usage humain normal :

- Variable d'environnement `PROXY_URL` (repli global) ou `PROXY_URL_<n>` (dédiée à un compte, prioritaire — voir `config.nom_secret_proxy`), au format :
  ```
    http://utilisateur:motdepasse@hote:port
    http://hote:port                          # proxy sans authentification
    socks5://utilisateur:motdepasse@hote:port  # Playwright supporte aussi SOCKS5
    ```
  - **Entièrement optionnel** : absente ou vide, `config.proxy_playwright(compte)` retourne `None` et le run se comporte exactement comme avant (aucun proxy, sortie directe par l'IP du runner). Une valeur mal formée est aussi ignorée (avec un avertissement loggué), plutôt que de faire échouer le run.
  - En local : ajouter `PROXY_URL` (ou `PROXY_URL_1`…`PROXY_URL_5`) à `.env`.
  - En CI : créer les secrets `PROXY_URL_1`…`PROXY_URL_5` (Settings → Secrets and variables → Actions) — idéalement un proxy **distinct par compte**, pour que chacun des 5 comptes conserve une IP de sortie cohérente d'un run à l'autre (comme un vrai navigateur qui revient), plutôt que 5 comptes partageant une même IP proxy, qui reconstituerait le même signal de risque à une autre échelle. Le workflow (`daily_scraper.yml`) injecte déjà ces 5 secrets vers le job matriciel correspondant.

  Limite assumée (même réserve que pour les autres mesures de "Stratégie anti-blocage") : un proxy réduit ce risque précis, il ne l'élimine pas — un proxy lui-même partagé/mal réputé peut rester détecté. Ce n'est pas non plus un contournement des CGU de Meta évoquées plus haut, seulement un changement d'infrastructure réseau.

## Session expirée : recharger les cookies sans bloquer le pipeline

Quand Facebook invalide la session (`SessionExpireeError`), le run s'arrête et un cooldown d'1h se déclenche (`config.COOLDOWN_HEURES_APRES_SESSION_EXPIREE`) en attendant que le secret de cookies du compte concerné soit régénéré à la main.

Pour régénérer : ouvrir `web.facebook.com` connecté sur le compte dédié au scraping, exporter les cookies avec une extension type [Cookie-Editor](https://cookie-editor.com/) (format JSON brut, sans les modifier), puis lancer :

```bash
# Multi-comptes : cible le secret FB_COOKIES_JSON_<n> et l'état du compte <n>
python scripts/maj_cookies.py export_cookie_editor.json --repo <owner>/<repo> --compte 3 --set-secret

# Mono-compte (historique, sans --compte) : secret FB_COOKIES_JSON et état global
python scripts/maj_cookies.py export_cookie_editor.json --repo <owner>/<repo> --set-secret
```

Ce script (`scripts/maj_cookies.py`) :
1. Valide l'export (mêmes règles que `scraper.charger_cookies` — présence de `c_user`/`xs`, tolérance au format brut d'extension `expirationDate`/`sameSite`).
2. Met à jour le secret GitHub `FB_COOKIES_JSON_<compte>` (ou `FB_COOKIES_JSON` sans `--compte`) via `gh secret set` — ou affiche la commande/le JSON si `gh` n'est pas installé/authentifié, mise à jour manuelle alors possible depuis Settings → Secrets and variables → Actions.
3. Purge le cooldown et le `storage_state` en cache localement (`data/state/compte_<n>/`, ou `data/state/` sans `--compte`) — **étape nécessaire**, pas juste pratique : `scraper._charger_cookies_caches()` donne priorité au `storage_state` mis en cache sur le secret de cookies à chaque run, et un cookie invalidé côté serveur Facebook n'a pas forcément de date d'expiration dépassée (le test de fraîcheur par date ne le détecte donc pas). Sans cette purge, le run suivant rechargeait silencieusement les cookies morts du cache au lieu des nouveaux, malgré la mise à jour du secret — le pipeline restait bloqué en boucle. (`scraper.invalider_storage_state` applique la même purge côté run : dès qu'une `SessionExpireeError` est détectée, le `storage_state` du run en cours n'est plus mis en cache du tout.)
4. En option (`--clear-actions-cache`), supprime aussi le cache `actions/cache` côté CI — `etat-scraper-compte-<n>-*` avec `--compte`, `etat-scraper-*` sans.

Ajouter `--clear-actions-cache` si le run précédent a tourné en CI (sinon le prochain run restaurera le `storage_state` mort depuis le cache GitHub Actions, même après la purge locale). Sans `gh` installé, le script affiche le JSON compact à coller manuellement dans le secret, et il faut alors aussi vider le cache Actions à la main (onglet Actions → Caches).

## Pages Facebook

Deux cibles ne sont pas des groupes mais des **Pages** Facebook (`https://www.facebook.com/PerfectorImmobilier/`, `https://www.facebook.com/offrimmo.bf/`) — ajoutées dans `groups.csv` (comptes 4 et 5) avec un `id` textuel (slug) plutôt qu'un id numérique de groupe, puisqu'une Page n'en a pas. Leur colonne `membres` est vide, volontairement : une Page n'a pas de nombre de membres.

`scraper_groupe` navigue désormais vers `groupe.url` tel quel (au lieu de reconstruire systématiquement `/groups/<id>/` à partir de l'id) — ce qui permet de traiter une Page avec le même code qu'un groupe, SANS garantie que ça fonctionne : la structure JSON Comet d'un fil de Page n'a jamais été vérifiée en conditions réelles ici, contrairement à celle d'un groupe (confirmée sur plusieurs échantillons réels, voir historique dans `config.py`). Si ces deux entrées remontent 0 post en continu après plusieurs runs (`data/logs/debug_page_vide_*.html`, `data/logs/debug_scroll_vide_*.json`), c'est le signe que Comet structure différemment un fil de Page — inspecter ces fichiers de debug pour confirmer, une extraction dédiée (`scraper_page`) serait alors nécessaire plutôt qu'une réutilisation telle quelle de `extraire_stories_depuis_json`.

## Multi-comptes

Depuis le 2026-08-26, les groupes de `groups.csv` sont répartis entre **5 comptes Facebook** (colonne `compte`, valeurs `"1"` à `"5"`) plutôt que scrapés en totalité par un seul compte : chaque compte ne voit et ne scrolle QUE ses propres groupes, avec sa propre session, son propre cooldown et son propre score de confiance (throttle adaptatif) — un blocage sur un compte n'affecte pas les 4 autres.

**Ce que ça change concrètement :**
- `groups.csv` a une colonne `compte` en plus (`id,nom,url,actif,confidentialite,membres,compte`). Colonne **optionnelle** : un `groups.csv` sans cette colonne reste valide, tous les groupes sont alors traités comme appartenant au compte `"1"` (rétrocompatibilité avec l'ancien fonctionnement mono-compte).
- `main.py` accepte `--compte {1,2,3,4,5}` : restreint le run au secret `FB_COOKIES_JSON_<compte>`, à un état persistant isolé (`data/state/compte_<n>/`), et aux groupes de `groups.csv` assignés à ce compte. Omis (comportement historique) : secret `FB_COOKIES_JSON` unique, état global, tous les groupes actifs confondus.
- **5 secrets GitHub distincts** à créer (Settings → Secrets and variables → Actions) : `FB_COOKIES_JSON_1` … `FB_COOKIES_JSON_5`, un export Cookie-Editor par compte dédié (voir section précédente pour la procédure d'export/régénération, identique par compte).
- Le workflow `.github/workflows/daily_scraper.yml` lance **5 jobs indépendants en parallèle** (matrice `compte: ["1".."5"]`) : un blocage/cooldown sur l'un ne bloque pas les autres (`fail-fast: false`), chacun a son propre cache d'état (`etat-scraper-compte-<n>-*`) et son propre artefact (`annonces-foncieres-compte-<n>-*`).
- La base PostgreSQL (`DATABASE_URL`) et `data/processed/annonces.xlsx` restent **partagés** entre les 5 comptes : upsert par `id` de post, peu importe quel compte a scrapé quel post — c'est toujours la même annonce.
- `data/state/seen_post_ids.json` (déduplication des posts déjà vus) reste lui aussi **global**, volontairement non isolé par compte — un post public déjà vu par un compte n'a pas besoin d'être re-scrapé/re-structuré par un autre.

**Répartition actuelle** (`groups.csv`) : 25 entrées, exactement 5 par compte. Les deux seules entrées `actif=false` (`1412949025757240`, `352566539534344`) sont toutes les deux assignées au **compte 3**, qui ne traite donc que **3 groupes actifs** contre 5 pour les quatre autres comptes. Ce n'est pas un bug (le filtrage `actif`/`compte` fonctionne comme prévu) mais un déséquilibre de charge : à rééquilibrer en réassignant deux groupes vers le compte 3 si l'on veut une répartition réellement homogène.

**Risque à avoir en tête** (déjà signalé plus haut pour le compte unique, mais qui s'accumule x5 ici) : multiplier les comptes automatisés reste une automatisation de sessions Facebook authentifiées, ce qui contrevient aux CGU de Meta — chaque compte doit être un compte dédié, à volume mesuré, jamais un compte personnel principal ; le risque de bannissement/désactivation s'applique indépendamment à chacun des 5.

## Structure du code

```
ouaga-foncier-etl/
├── config.py        # mots-clés/regex, quartiers, statuts de document, groupes, délais anti-blocage
├── scraper.py       # Playwright : navigation, scroll, capture GraphQL, détection de blocage/session expirée
├── processor.py     # filtrage regex, appel API OpenAI, schéma + upsert PostgreSQL, export Excel
├── main.py          # orchestrateur CLI (--mode, --days-back, --group-limit, --batch-size,
│                    #                     --round-robin, --groups-per-run, --compte, --skip-llm)
├── groups.csv       # groupes Facebook ciblés (id, nom, url, actif, confidentialite, membres, compte)
├── requirements.txt
├── pytest.ini       # asyncio_mode = auto, testpaths = tests
├── .env.example     # FB_COOKIES_JSON(_1..5), OPENAI_API_KEY, DATABASE_URL, TEST_DATABASE_URL
├── .github/workflows/daily_scraper.yml  # job tests (bloquant) -> job scraping (matrice 5 comptes)
├── scripts/maj_cookies.py  # recharge FB_COOKIES_JSON(_n) depuis un export Cookie-Editor (voir ci-dessus)
└── tests/           # une suite par module, API OpenAI et navigateur entièrement mockés
```

## Installation

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
cp .env.example .env  # renseigner FB_COOKIES_JSON_1..5 (ou FB_COOKIES_JSON en mono-compte), OPENAI_API_KEY, DATABASE_URL
```

## Utilisation

```bash
python main.py --mode daily                                          # run quotidien, mono-compte (historique)
python main.py --mode daily --compte 3                               # run quotidien, uniquement les groupes du compte 3
python main.py --mode backfill --days-back 14 --group-limit 5        # rattrapage ciblé
python main.py --mode daily --compte 2 --round-robin --groups-per-run 2  # rotation : 2 groupes du compte 2 par run
python main.py --mode daily --skip-llm                               # test scraping+filtrage sans coût API
```

## Tests

```bash
pytest -q                                    # 188 passent, 12 skippés (sans PostgreSQL de test)
TEST_DATABASE_URL=postgresql://... pytest -q  # 200/200 avec une base de test dédiée
```

**200 tests** au total (logique de filtrage, normalisation, parsing JSON Comet, throttle, CLI, base de données, isolation d'état multi-comptes). 12 d'entre eux exigent un vrai serveur PostgreSQL via `TEST_DATABASE_URL` (base **séparée** — la suite fait des `DROP TABLE`) et sont automatiquement skippés si elle est absente ou injoignable ; la CI les exécute contre un service `postgres:16` éphémère. Le scraping live contre Facebook et les appels API réels ne sont pas couverts par la suite automatisée — à valider manuellement après toute modification de `scraper.py` ou `processor.py`.

## Limites connues

- L'automatisation d'une session Facebook authentifiée contrevient aux CGU de Meta — usage sur un compte dédié, à volume mesuré, jamais sur un compte personnel principal.
- L'API interne de Facebook (JSON Comet, GraphQL) n'est pas documentée officiellement et peut changer sans préavis ; le parsing repose sur une signature structurelle plutôt que sur des chemins de clés fixes pour limiter l'impact.
- Le fil des deux **Pages** (comptes 4 et 5) n'a jamais été validé en conditions réelles — voir section "Pages Facebook".
- Les posts "mis en avant" (épinglés) sont collectés sans filtre de date : ils peuvent être anciens par nature, leur date n'intervient jamais dans la décision de lancer le scroll.
