#!/usr/bin/env bash
# Raccourci pour scripts/maj_cookies.py : détecte automatiquement le dépôt
# GitHub depuis le remote git courant, applique --set-secret et
# --clear-actions-cache par défaut, et ferme l'issue GitHub "session-expiree"
# ouverte automatiquement par le workflow (voir daily_scraper.yml, étape
# "Alerte : session Facebook expirée") si `gh` est authentifié.
#
# But : que le geste complet de régénération (valider l'export, pousser le
# secret, purger l'état local ET le cache Actions, refermer l'alerte) tienne
# en UNE commande, au lieu d'un aller-retour de plusieurs flags à retenir/
# recopier à la main à chaque fois.
#
# Usage :
#   ./scripts/regenerer_cookies.sh export_cookie_editor.json
#
# Le fichier passé en argument est l'export BRUT de l'extension (Cookie-
# Editor ou équivalent) - voir scripts/maj_cookies.py pour le détail du
# format attendu et des validations effectuées.

set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "Usage : $0 <export_cookie_editor.json>" >&2
    exit 1
fi

EXPORT="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

if [ ! -f "$EXPORT" ]; then
    echo "Fichier introuvable : $EXPORT" >&2
    exit 1
fi

# Détection du dépôt owner/repo depuis le remote git "origin" - fonctionne
# aussi bien avec une URL SSH (git@github.com:owner/repo.git) qu'HTTPS
# (https://github.com/owner/repo.git ou .git omis).
REPO="$(
    cd "$REPO_ROOT" \
        && git remote get-url origin 2>/dev/null \
        | sed -E 's#^(git@|https://)github\.com[:/]##; s#\.git$##'
)"

if [ -z "$REPO" ]; then
    echo "Impossible de détecter le dépôt GitHub depuis le remote 'origin' - " \
        "lance scripts/maj_cookies.py directement avec --repo owner/repo." >&2
    exit 1
fi

echo "Dépôt détecté : $REPO"

python3 "$SCRIPT_DIR/maj_cookies.py" "$EXPORT" \
    --repo "$REPO" \
    --set-secret \
    --clear-actions-cache

# Referme l'issue d'alerte ouverte automatiquement par le workflow, si `gh`
# est disponible/authentifié - best-effort, ne doit jamais faire échouer le
# script (les cookies sont déjà régénérés à ce stade, c'est le plus important).
if command -v gh >/dev/null 2>&1; then
    NUMERO="$(
        gh issue list --repo "$REPO" --state open --label "session-expiree" \
            --json number --jq '.[0].number' 2>/dev/null || true
    )"
    if [ -n "$NUMERO" ]; then
        gh issue close "$NUMERO" --repo "$REPO" \
            --comment "Cookies régénérés via scripts/regenerer_cookies.sh - à confirmer au prochain run." \
            || echo "Avertissement : échec de fermeture de l'issue #$NUMERO (non bloquant)." >&2
        echo "Issue #$NUMERO fermée."
    fi
else
    echo "'gh' introuvable - pense à fermer manuellement l'issue GitHub 'session-expiree' si elle existe." >&2
fi

echo
echo "Terminé. Relance le workflow manuellement depuis l'onglet Actions (Run workflow) si tu veux vérifier tout de suite."
