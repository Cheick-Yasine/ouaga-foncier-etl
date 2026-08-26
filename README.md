# Pipeline ETL — Annonces foncières Ouagadougou

Scraping de groupes Facebook ciblés → filtrage local → structuration par LLM → base PostgreSQL + export Excel. Projet académique.

## Description technique

- **Langage** : Python 3.12.
- **Scraping** : Playwright (Chromium, async), session authentifiée via cookies (`FB_COOKIES_JSON`), cible `web.facebook.com` (interface "Comet").
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
- `daily` : dernières 24h, tourne automatiquement chaque nuit à 23h00 UTC (GitHub Actions).
- `backfill` : rattrapage historique, `--days-back` réglable (déclenchement manuel uniquement).

Un throttle adaptatif (délais, volume traité) et un circuit breaker (arrêt + cooldown en cas de blocage détecté ou de session expirée) protègent le compte Facebook utilisé — voir les commentaires de `config.py` et `scraper.py` pour le détail.

## Session expirée : recharger les cookies sans bloquer le pipeline

Quand Facebook invalide la session (`SessionExpireeError`), le run s'arrête et un cooldown d'1h se déclenche (`config.COOLDOWN_HEURES_APRES_SESSION_EXPIREE`) en attendant que le secret `FB_COOKIES_JSON` soit régénéré à la main.

Pour régénérer : ouvrir `web.facebook.com` connecté sur le compte dédié au scraping, exporter les cookies avec une extension type [Cookie-Editor](https://cookie-editor.com/) (format JSON brut, sans les modifier), puis lancer :

```bash
python scripts/maj_cookies.py export_cookie_editor.json --repo <owner>/<repo> --set-secret
```

Ce script (`scripts/maj_cookies.py`) :
1. Valide l'export (mêmes règles que `scraper.charger_cookies` — présence de `c_user`/`xs`, tolérance au format brut d'extension `expirationDate`/`sameSite`).
2. Met à jour le secret GitHub `FB_COOKIES_JSON` via `gh secret set` (ou affiche la commande/le JSON si `gh` n'est pas installé/authentifié — mise à jour manuelle alors possible depuis Settings → Secrets and variables → Actions).
3. Purge le cooldown et le `storage_state` en cache localement (`data/state/`) — **étape nécessaire**, pas juste pratique : `scraper._charger_cookies_caches()` donne priorité au `storage_state` mis en cache sur `FB_COOKIES_JSON` à chaque run, et un cookie invalidé côté serveur Facebook n'a pas forcément de date d'expiration dépassée (le test de fraîcheur par date ne le détecte donc pas). Sans cette purge, le run suivant rechargeait silencieusement les cookies morts du cache au lieu des nouveaux, malgré la mise à jour du secret — le pipeline restait bloqué en boucle. (`scraper.invalider_storage_state` applique la même purge côté run : dès qu'une `SessionExpireeError` est détectée, le `storage_state` du run en cours n'est plus mis en cache du tout.)
4. En option (`--clear-actions-cache`), supprime aussi le cache `actions/cache` `etat-scraper-*` côté CI (même contenu, côté GitHub Actions).

Ajouter `--clear-actions-cache` si le run précédent a tourné en CI (sinon le prochain run restaurera le `storage_state` mort depuis le cache GitHub Actions, même après la purge locale). Sans `gh` installé, le script affiche le JSON compact à coller manuellement dans le secret, et il faut alors aussi vider le cache Actions à la main (onglet Actions → Caches).

## Structure du code

```
ouaga-foncier-etl/
├── config.py       # mots-clés/regex, quartiers, statuts de document, groupes, délais anti-blocage
├── scraper.py       # Playwright : navigation, scroll, capture GraphQL, détection de blocage/session expirée
├── processor.py      # filtrage regex, appel API OpenAI, schéma + upsert PostgreSQL, export Excel
├── main.py         # orchestrateur CLI (--mode, --days-back, --group-limit, --batch-size, --skip-llm)
├── groups.csv       # liste des groupes Facebook ciblés (id, nom, url, actif, confidentialité)
├── requirements.txt
├── .env.example      # FB_COOKIES_JSON, OPENAI_API_KEY, DATABASE_URL
├── .github/workflows/daily_scraper.yml  # job tests (bloquant) -> job scraping
├── scripts/maj_cookies.py  # recharge FB_COOKIES_JSON depuis un export Cookie-Editor (voir section ci-dessus)
└── tests/          # une suite par module, API OpenAI et navigateur entièrement mockés
```

## Installation

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
cp .env.example .env  # renseigner FB_COOKIES_JSON, OPENAI_API_KEY, DATABASE_URL
```

## Utilisation

```bash
python main.py --mode daily                                    # run quotidien standard
python main.py --mode backfill --days-back 14 --group-limit 5   # rattrapage ciblé
python main.py --mode daily --skip-llm                          # test scraping+filtrage sans coût API
```

## Tests

```bash
pytest -q
```

163 tests passent (logique de filtrage, normalisation, parsing JSON Comet, throttle, CLI, base de données). Le scraping live contre Facebook et les appels API réels ne sont pas couverts par la suite automatisée — à valider manuellement après toute modification de `scraper.py` ou `processor.py`.

## Limites connues

- L'automatisation d'une session Facebook authentifiée contrevient aux CGU de Meta — usage sur un compte dédié, à volume mesuré, jamais sur un compte personnel principal.
- L'API interne de Facebook (JSON Comet, GraphQL) n'est pas documentée officiellement et peut changer sans préavis ; le parsing repose sur une signature structurelle plutôt que sur des chemins de clés fixes pour limiter l'impact.
