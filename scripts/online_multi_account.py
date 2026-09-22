"""Lance le pipeline GitHub Actions avec bascule automatique entre comptes Facebook.

Les cookies restent exclusivement dans les GitHub Actions Secrets :
FB_COOKIES_JSON_COMPTE1, FB_COOKIES_JSON_COMPTE2, ... puis, en secours,
le secret historique FB_COOKIES_JSON.

Chaque compte reçoit FB_SESSION_NAME afin que config.py isole son
storage_state/cooldown. Les codes 2 (session expirée), 3 (blocage) et
4 (cooldown actif en mode multi-compte) déclenchent le compte suivant.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "scripts" / "resilient_entrypoint.py"

ACCOUNT_SECRET_NAMES = [
    ("compte1", "FB_COOKIES_JSON_COMPTE1"),
    ("compte2", "FB_COOKIES_JSON_COMPTE2"),
    ("compte3", "FB_COOKIES_JSON_COMPTE3"),
    ("compte4", "FB_COOKIES_JSON_COMPTE4"),
    ("compte5", "FB_COOKIES_JSON_COMPTE5"),
]
LEGACY_SECRET = ("legacy", "FB_COOKIES_JSON")
RETRYABLE_CODES = {2, 3, 4}


def _configured_accounts() -> list[tuple[str, str]]:
    dedicated: list[tuple[str, str]] = []
    for account_name, env_name in ACCOUNT_SECRET_NAMES:
        value = os.environ.get(env_name, "").strip()
        if value:
            dedicated.append((account_name, value))

    # Répartit les runs planifiés entre les comptes au lieu de solliciter
    # systématiquement compte1. Le secret historique reste uniquement en
    # dernier recours.
    if len(dedicated) > 1:
        try:
            run_number = int(os.environ.get("GITHUB_RUN_NUMBER", "1"))
        except ValueError:
            run_number = 1
        offset = (run_number - 1) % len(dedicated)
        dedicated = dedicated[offset:] + dedicated[:offset]

    legacy_value = os.environ.get(LEGACY_SECRET[1], "").strip()
    if legacy_value:
        dedicated.append((LEGACY_SECRET[0], legacy_value))

    return dedicated


def main() -> int:
    if not ENTRYPOINT.exists():
        print(f"Entrée résiliente introuvable : {ENTRYPOINT}")
        return 1

    accounts = _configured_accounts()
    if not accounts:
        print(
            "Aucun cookie Facebook configuré. Ajoute au moins "
            "FB_COOKIES_JSON_COMPTE1 dans GitHub Actions Secrets."
        )
        return 1

    print(
        "Comptes Facebook disponibles pour ce run : "
        + ", ".join(name for name, _ in accounts)
    )

    last_code = 2
    for index, (account_name, cookies_json) in enumerate(accounts, start=1):
        print(
            f"\n=== Tentative Facebook {index}/{len(accounts)} : "
            f"{account_name} ==="
        )

        env = os.environ.copy()
        env["FB_COOKIES_JSON"] = cookies_json
        env["FB_SESSION_NAME"] = account_name
        env["FB_MULTI_ACCOUNT_MODE"] = "1"

        result = subprocess.run(
            [sys.executable, str(ENTRYPOINT), *sys.argv[1:]],
            cwd=str(ROOT),
            env=env,
            check=False,
        )
        code = int(result.returncode)
        last_code = code

        if code == 0:
            print(f"Compte {account_name} : pipeline terminé avec succès.")
            return 0

        if code in RETRYABLE_CODES:
            reason = {
                2: "session expirée",
                3: "blocage Facebook détecté",
                4: "cooldown actif",
            }.get(code, f"code {code}")
            print(
                f"Compte {account_name} indisponible ({reason}). "
                "Bascule vers le compte suivant."
            )
            continue

        print(
            f"Compte {account_name} : erreur non liée à la session "
            f"(code {code}). Arrêt sans changer de compte."
        )
        return code

    print(
        "\nTous les comptes Facebook configurés sont indisponibles. "
        "Les RAW déjà checkpointés dans Neon restent conservés."
    )
    return last_code


if __name__ == "__main__":
    raise SystemExit(main())
