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
- Le groupe privé `1412949025757240` est désactivé (`actif=false` dans `groups.csv`) : le compte de scraping n'y est pas membre.
- L'API interne de Facebook (JSON Comet, GraphQL) n'est pas documentée officiellement et peut changer sans préavis ; le parsing repose sur une signature structurelle plutôt que sur des chemins de clés fixes pour limiter l'impact.
