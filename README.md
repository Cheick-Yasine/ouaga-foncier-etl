# Pipeline ETL — Annonces foncières Ouagadougou

Scraping de groupes Facebook ciblés → filtrage local → structuration par LLM → base PostgreSQL + export Excel. Projet académique.

## Description technique

- **Langage** : Python 3.12 (3.12.7 en CI).
- **Scraping** : Playwright (Chromium, async), session authentifiée via cookies (`FB_COOKIES_JSON`, ou `FB_COOKIES_JSON_1`…`FB_COOKIES_JSON_5` en multi-comptes), cible `m.facebook.com` avec un profil Android stable par compte. Une redirection éventuelle vers l'interface Comet est détectée et journalisée.
- **Filtrage** : regex locales, gratuites, aucun appel API (`config.py`).
- **Structuration** : API OpenAI (`gpt-4o-mini`), Structured Outputs (schéma JSON strict) pour extraire les champs (type de bien, quartier, superficie, prix, statut du document, contacts).
- **Stockage** : PostgreSQL (hébergé sur Neon), upsert par `id` de post — jamais de doublon. Export Excel régénéré à chaque run.
- **Orchestration** : GitHub Actions, cron quotidien + déclenchement manuel.

## Fonctionnement

Le pipeline s'exécute en 4 étapes, orchestrées par `main.py` :

1. **Scraping** (`scraper.py`) — pour chaque groupe actif de `groups.csv` : ouverture de l'URL normalisée vers `m.facebook.com`, extraction des posts "mis en avant" inédits, puis scroll variable avec interception des réponses réseau GraphQL. Le navigateur et son contexte sont sauvegardés puis fermés après chaque groupe, y compris à la frontière entre deux lots.
2. **Filtrage** (`processor.py`, étape A) — chaque post brut passe par des regex (mots-clés fonciers, exclusion des recherches pures et du spam) pour ne garder que les candidats plausibles, sans coût API.
3. **Structuration LLM** (`processor.py`, étape B) — chaque candidat est envoyé à l'API OpenAI, qui renvoie une structure validée (Pydantic) ou rejette le post s'il ne s'agit pas d'une vraie annonce. Les champs `quartier_zone` et `statut_document` sont ensuite normalisés (casse) contre une liste connue.
4. **Persistance** (`processor.py`) — upsert des annonces valides dans PostgreSQL, mise à jour de l'export Excel, détection de dérive de volume (alerte si un run quotidien produit anormalement peu de résultats vs l'historique).

Deux modes d'exécution (`--mode`) :
- `daily` : dernières 24h (`--days-back 1`), déclenché deux fois par jour par les crons statiques `00:17` et `12:41` UTC, puis retardé de 0 à 30 minutes. Les minutes du cron ne sont pas aléatoires automatiquement ; elles peuvent seulement être changées manuellement dans le workflow.
- `backfill` : rattrapage historique, `--days-back` réglable (déclenchement manuel uniquement, `workflow_dispatch`).

Le déclenchement manuel propose aussi `compte=all|1|2|3|4|5`. Pour un premier test réel, choisir un seul compte, `group_limit=1` et `days_back=1` ; les quatre autres jobs ne sont alors pas créés.

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
- Sur le runner auto-hébergé, aucun dump PostgreSQL ni export contenant des annonces n'est envoyé comme artefact GitHub. La base Neon reste la source de vérité et les fichiers de travail restent sur le PC.
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
  - En mode `proxy`, `PROXY_URL_<n>` est obligatoire. En mode `direct`, réservé au runner auto-hébergé, le workflow injecte `ALLOW_DIRECT_CONNECTION=true` et laisse volontairement les URL de proxy vides.
  - En local : ajouter `PROXY_URL` (ou `PROXY_URL_1`…`PROXY_URL_5`) à `.env`.
  - En CI : les secrets `PROXY_URL_1`…`PROXY_URL_5` ne sont utilisés que si l'entrée manuelle `network_mode=proxy` est choisie. Le cron du runner auto-hébergé utilise `network_mode=direct`.

Avant Facebook, chaque contexte ouvre `https://ip.decodo.com/json` et compare le pays observé au pays attendu, avec ou sans proxy. En mode direct, le workflow impose `BF` et `Africa/Ouagadougou`. Les variables suivantes restent configurables en mode proxy :

- `PROXY_COUNTRY_1`…`PROXY_COUNTRY_5` : code ISO à deux lettres, `BF` par défaut ;
- `BROWSER_LOCALE_1`…`BROWSER_LOCALE_5` : `fr-FR` par défaut ;
- `BROWSER_TIMEZONE_1`…`BROWSER_TIMEZONE_5` : `Africa/Ouagadougou` par défaut.

Un échec du proxy, du contrôle géographique ou une différence de pays arrête le compte concerné avant toute visite de groupe. Cette vérification améliore la cohérence de configuration mais ne garantit pas l'absence de contrôle ou de blocage par Meta.

  Limite assumée (même réserve que pour les autres mesures de "Stratégie anti-blocage") : un proxy réduit ce risque précis, il ne l'élimine pas — un proxy lui-même partagé/mal réputé peut rester détecté. Ce n'est pas non plus un contournement des CGU de Meta évoquées plus haut, seulement un changement d'infrastructure réseau.

## Session expirée : recharger les cookies sans bloquer le pipeline

Quand Facebook invalide la session (`SessionExpireeError`), le run s'arrête et un cooldown d'1h se déclenche (`config.COOLDOWN_HEURES_APRES_SESSION_EXPIREE`) en attendant que le secret de cookies du compte concerné soit régénéré.

### Méthode recommandée sous Windows : connexion interactive

Cette méthode n'enregistre jamais le mot de passe ni le code 2FA. Elle ouvre
Chromium visiblement, vous laisse effectuer vous-même la connexion, capture
les cookies une fois l'accueil connecté visible, puis les transmet au secret
GitHub par l'entrée standard de `gh` (ils ne figurent donc pas dans la ligne de
commande).

Dans PowerShell, une seule fois :

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
gh auth login
```

Puis, pour renouveler par exemple le compte 1 :

```powershell
python scripts/maj_cookies.py --interactive --repo Cheick-Yasine/ouaga-foncier-etl --compte 1 --set-secret
```

Connectez-vous au compte demandé dans la fenêtre Chromium et terminez toute
2FA/vérification manuellement. N'appuyez sur Entrée dans PowerShell qu'une fois
l'accueil Facebook connecté visible. Répétez la commande avec `--compte 2`,
`3`, `4` puis `5`, en vérifiant soigneusement le compte ouvert à chaque fois.

> **Important : ne cliquez pas sur « Se déconnecter » après la capture.**
> Une déconnexion peut révoquer les cookies qui viennent d'être enregistrés.
> Appuyez simplement sur Entrée : le script capture la session puis ferme
> Chromium. La commande suivante crée automatiquement un contexte navigateur
> vierge pour le compte suivant ; aucune session locale du compte précédent
> n'y est réutilisée.

Cette procédure simplifie la capture, mais ne peut pas renouveler une session
révoquée sans reconnexion humaine. Si un cooldown vient d'être sauvegardé par
le dernier run, attendez sa fin avant de relancer le workflow.

### Méthode alternative : export Cookie-Editor

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
4. `--clear-actions-cache` est une opération exceptionnelle : le cache contient aussi `seen_post_ids.json`. Le script refuse désormais cette suppression sans la confirmation supplémentaire `--force-clear-actions-cache`.

Ne supprimez pas le cache Actions pour un renouvellement normal. Le run qui détecte une session expirée invalide déjà son `storage_state` tout en conservant l'historique des publications vues.

## Pages Facebook

Deux cibles ne sont pas des groupes mais des **Pages** Facebook (`https://www.facebook.com/PerfectorImmobilier/`, `https://www.facebook.com/offrimmo.bf/`) — ajoutées dans `groups.csv` (comptes 4 et 5) avec un `id` textuel (slug) plutôt qu'un id numérique de groupe, puisqu'une Page n'en a pas. Leur colonne `membres` est vide, volontairement : une Page n'a pas de nombre de membres.

`scraper_groupe` navigue désormais vers `groupe.url` tel quel (au lieu de reconstruire systématiquement `/groups/<id>/` à partir de l'id) — ce qui permet de traiter une Page avec le même code qu'un groupe, SANS garantie que ça fonctionne : la structure JSON Comet d'un fil de Page n'a jamais été vérifiée en conditions réelles ici, contrairement à celle d'un groupe (confirmée sur plusieurs échantillons réels, voir historique dans `config.py`). Si ces deux entrées remontent 0 post en continu après plusieurs runs (`data/logs/debug_page_vide_*.html`, `data/logs/debug_scroll_vide_*.json`), c'est le signe que Comet structure différemment un fil de Page — inspecter ces fichiers de debug pour confirmer, une extraction dédiée (`scraper_page`) serait alors nécessaire plutôt qu'une réutilisation telle quelle de `extraire_stories_depuis_json`.

## Multi-comptes

Depuis le 2026-08-26, les groupes de `groups.csv` sont répartis entre **5 comptes Facebook** (colonne `compte`, valeurs `"1"` à `"5"`) plutôt que scrapés en totalité par un seul compte : chaque compte ne voit et ne scrolle QUE ses propres groupes, avec sa propre session, son propre cooldown et son propre score de confiance (throttle adaptatif) — un blocage sur un compte n'affecte pas les 4 autres.

**Ce que ça change concrètement :**
- `groups.csv` a une colonne `compte` en plus (`id,nom,url,actif,confidentialite,membres,compte`). Colonne **optionnelle** : un `groups.csv` sans cette colonne reste valide, tous les groupes sont alors traités comme appartenant au compte `"1"` (rétrocompatibilité avec l'ancien fonctionnement mono-compte).
- `main.py` accepte `--compte {1,2,3,4,5}` : restreint le run au secret `FB_COOKIES_JSON_<compte>`, à un état persistant isolé (`data/state/compte_<n>/`), et aux groupes de `groups.csv` assignés à ce compte. Omis (comportement historique) : secret `FB_COOKIES_JSON` unique, état global, tous les groupes actifs confondus.
- **5 secrets GitHub distincts** à créer (Settings → Secrets and variables → Actions) : `FB_COOKIES_JSON_1` … `FB_COOKIES_JSON_5`, un export Cookie-Editor par compte dédié (voir section précédente pour la procédure d'export/régénération, identique par compte).
- Le workflow `.github/workflows/daily_scraper.yml` crée **5 jobs indépendants** (matrice `compte: ["1".."5"]`). Avec un seul runner auto-hébergé, ils s'exécutent l'un après l'autre ; leur état reste dans `data/state/compte_<n>/` sur le PC.
- La base PostgreSQL (`DATABASE_URL`) et `data/processed/annonces.xlsx` restent **partagés** entre les 5 comptes : upsert par `id` de post, peu importe quel compte a scrapé quel post — c'est toujours la même annonce.
- `data/state/compte_<n>/seen_post_ids.json` conserve localement sur le PC la déduplication des posts déjà vus pour chaque compte.

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

## Runner GitHub Actions auto-hébergé (Windows)

Le job de tests reste sur `ubuntu-latest`. Seul le scraping utilise le PC
Windows, avec les labels `self-hosted`, `Windows`, `X64` et
`ouaga-foncier`. En mode `direct`, aucune variable `PROXY_URL_<n>` n'est
transmise : la connexion Internet résidentielle du PC est utilisée.

Dans GitHub : **Settings → Actions → Runners → New self-hosted runner**,
choisir **Windows / x64**, puis exécuter dans PowerShell les commandes
personnalisées affichées par GitHub. Pendant la configuration, ajouter le label
`ouaga-foncier`. Installer le runner comme service permet aux crons de
fonctionner sans ouvrir PowerShell, mais le PC doit rester allumé et connecté.

Sur le PC, Python 3.12 doit être disponible. Le workflow installe ensuite les
dépendances et Chromium automatiquement. Le checkout est volontairement lancé
avec `clean: false` afin de conserver `data/state/compte_<n>/` entre les
runs. Les cookies de session, les exports et le dump PostgreSQL ne sont pas
envoyés dans les caches ou artefacts GitHub. Pour un premier essai manuel :
`compte=1`, `network_mode=direct`, `days_back=1`, `group_limit=1`,
`batch_size=1`.

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
pytest -q                                    # 215 passent, 12 ignorés sans PostgreSQL de test
TEST_DATABASE_URL=postgresql://... pytest -q  # exécute aussi les cas PostgreSQL
```

La suite couvre désormais aussi les profils mobiles stables, le contrôle du pays du proxy, les redirections de domaine et la fermeture du navigateur après chaque groupe, y compris en cas d'erreur. Les tests PostgreSQL nécessitent toujours une base séparée ; la CI les exécute contre un service `postgres:16` éphémère. Le scraping live contre Facebook et les appels API réels ne sont pas couverts — ils doivent être validés avec un seul groupe avant un run complet.

## Limites connues

- L'automatisation d'une session Facebook authentifiée contrevient aux CGU de Meta — usage sur un compte dédié, à volume mesuré, jamais sur un compte personnel principal.
- L'API interne de Facebook (JSON Comet, GraphQL) n'est pas documentée officiellement et peut changer sans préavis ; le parsing repose sur une signature structurelle plutôt que sur des chemins de clés fixes pour limiter l'impact.
- Le fil des deux **Pages** (comptes 4 et 5) n'a jamais été validé en conditions réelles — voir section "Pages Facebook".
- Les posts "mis en avant" (épinglés) sont collectés sans filtre de date : ils peuvent être anciens par nature, leur date n'intervient jamais dans la décision de lancer le scroll.
