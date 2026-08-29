"""Tests de scripts/maj_cookies.py - le "gadget cookies" qui recharge
FB_COOKIES_JSON depuis un export d'extension (Cookie-Editor ou équivalent)
sans laisser le pipeline bloqué à attendre une intervention manuelle.

`gh` (GitHub CLI) n'est jamais réellement invoqué ici : soit il est absent du
PATH de test (chemin "affiche la commande / le JSON"), soit on monkeypatch
`shutil.which` pour simuler sa présence et on vérifie la commande construite
sans l'exécuter pour de vrai.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import maj_cookies  # noqa: E402
import config  # noqa: E402


def _export_valide() -> str:
    # Format réel d'export Cookie-Editor (expirationDate, sameSite en
    # minuscules) - valeurs entièrement synthétiques.
    return json.dumps([
        {
            "domain": ".facebook.com", "expirationDate": 1999999999.0,
            "hostOnly": False, "httpOnly": True, "name": "xs", "path": "/",
            "sameSite": "no_restriction", "secure": True, "session": False,
            "storeId": None, "value": "test_xs_value",
        },
        {
            "domain": ".facebook.com", "expirationDate": 1999999999.0,
            "hostOnly": False, "httpOnly": False, "name": "c_user", "path": "/",
            "sameSite": "lax", "secure": True, "session": False,
            "storeId": None, "value": "test_c_user_value",
        },
    ])


class TestCaptureInteractive:
    def test_valide_et_filtre_les_cookies_facebook(self):
        cookies = json.loads(_export_valide()) + [
            {"name": "autre", "value": "secret", "domain": ".example.com"}
        ]

        resultat = json.loads(maj_cookies.valider_cookies_captures(cookies))

        assert {cookie["name"] for cookie in resultat} == {"c_user", "xs"}

    def test_refuse_une_connexion_non_terminee(self):
        with pytest.raises(ValueError, match="c_user"):
            maj_cookies.valider_cookies_captures(
                [{"name": "xs", "value": "v", "domain": ".facebook.com"}]
            )


class TestValiderExport:
    def test_export_valide_retourne_le_json_brut_recompacte(self, tmp_path):
        brut = _export_valide()
        chemin = tmp_path / "export.json"
        chemin.write_text(brut, encoding="utf-8")

        resultat = maj_cookies.valider_export(chemin)

        # Le JSON brut (pas normalisé) doit rester ré-obtenable tel quel.
        assert json.loads(resultat) == json.loads(brut)

    def test_export_sans_c_user_xs_avertit_mais_ne_leve_pas(self, tmp_path, capsys):
        brut = json.dumps([{"name": "presence", "value": "v", "domain": ".facebook.com"}])
        chemin = tmp_path / "export.json"
        chemin.write_text(brut, encoding="utf-8")

        resultat = maj_cookies.valider_export(chemin)

        assert json.loads(resultat) == json.loads(brut)
        assert "AVERTISSEMENT" in capsys.readouterr().err

    def test_export_invalide_leve_value_error(self, tmp_path):
        chemin = tmp_path / "export.json"
        chemin.write_text("[]", encoding="utf-8")

        with pytest.raises(ValueError):
            maj_cookies.valider_export(chemin)

    def test_json_corrompu_leve(self, tmp_path):
        chemin = tmp_path / "export.json"
        chemin.write_text("{pas du json", encoding="utf-8")

        with pytest.raises(ValueError):
            maj_cookies.valider_export(chemin)


class TestPurgerEtatLocal:
    def test_supprime_cooldown_et_storage_state_existants(self, repertoires_isoles):
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        config.COOLDOWN_PATH.write_text("{}", encoding="utf-8")
        config.STORAGE_STATE_PATH.write_text("{}", encoding="utf-8")

        maj_cookies.purger_etat_local()

        assert not config.COOLDOWN_PATH.exists()
        assert not config.STORAGE_STATE_PATH.exists()

    def test_aucun_fichier_ne_leve_pas(self, repertoires_isoles):
        maj_cookies.purger_etat_local()  # ne doit pas lever


class TestMajSecretGithub:
    def test_gh_absent_affiche_le_json_sans_erreur(self, monkeypatch, capsys):
        monkeypatch.setattr(maj_cookies.shutil, "which", lambda _: None)

        maj_cookies.maj_secret_github(None, '{"a":1}', appliquer=True)

        assert '{"a":1}' in capsys.readouterr().out

    def test_gh_present_sans_set_secret_affiche_la_commande(self, monkeypatch, capsys):
        monkeypatch.setattr(maj_cookies.shutil, "which", lambda _: "/usr/bin/gh")

        maj_cookies.maj_secret_github("owner/repo", '{"a":1}', appliquer=False)

        sortie = capsys.readouterr().out
        assert "gh secret set" in sortie
        assert "owner/repo" in sortie

    def test_gh_present_avec_set_secret_appelle_subprocess(self, monkeypatch, capsys):
        appels = []

        class FauxResultat:
            returncode = 0

        def faux_run(commande, input=None, text=None, **kwargs):
            appels.append((commande, input))
            return FauxResultat()

        monkeypatch.setattr(maj_cookies.shutil, "which", lambda _: "/usr/bin/gh")
        monkeypatch.setattr(maj_cookies.subprocess, "run", faux_run)

        maj_cookies.maj_secret_github("owner/repo", '{"a":1}', appliquer=True)

        assert len(appels) == 1
        commande, entree = appels[0]
        assert commande == ["gh", "secret", "set", config.ENV_FB_COOKIES, "--repo", "owner/repo"]
        assert entree == '{"a":1}'
        assert "mis à jour" in capsys.readouterr().out

    def test_gh_present_echec_subprocess_sort_en_erreur(self, monkeypatch):
        class FauxResultat:
            returncode = 1

        monkeypatch.setattr(maj_cookies.shutil, "which", lambda _: "/usr/bin/gh")
        monkeypatch.setattr(maj_cookies.subprocess, "run", lambda *a, **k: FauxResultat())

        with pytest.raises(SystemExit):
            maj_cookies.maj_secret_github(None, '{"a":1}', appliquer=True)


class TestMainCli:
    def test_refuse_de_vider_le_cache_sans_confirmation_forcee(self, tmp_path):
        with pytest.raises(SystemExit):
            maj_cookies.main(
                [str(tmp_path / "export.json"), "--clear-actions-cache"]
            )

    def test_fichier_introuvable_sort_en_erreur(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            maj_cookies.main([str(tmp_path / "absent.json")])

    def test_export_invalide_sort_en_erreur(self, tmp_path, repertoires_isoles):
        chemin = tmp_path / "export.json"
        chemin.write_text("[]", encoding="utf-8")

        with pytest.raises(SystemExit):
            maj_cookies.main([str(chemin)])

    def test_run_nominal_purge_letat_et_affiche_le_json(
        self, tmp_path, repertoires_isoles, monkeypatch, capsys
    ):
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        config.COOLDOWN_PATH.write_text("{}", encoding="utf-8")
        config.STORAGE_STATE_PATH.write_text("{}", encoding="utf-8")

        monkeypatch.setattr(maj_cookies.shutil, "which", lambda _: None)

        chemin = tmp_path / "export.json"
        chemin.write_text(_export_valide(), encoding="utf-8")

        code = maj_cookies.main([str(chemin)])

        assert code == 0
        assert not config.COOLDOWN_PATH.exists()
        assert not config.STORAGE_STATE_PATH.exists()

    def test_no_purge_etat_local_conserve_les_fichiers(
        self, tmp_path, repertoires_isoles, monkeypatch
    ):
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        config.COOLDOWN_PATH.write_text("{}", encoding="utf-8")

        monkeypatch.setattr(maj_cookies.shutil, "which", lambda _: None)

        chemin = tmp_path / "export.json"
        chemin.write_text(_export_valide(), encoding="utf-8")

        maj_cookies.main([str(chemin), "--no-purge-etat-local"])

        assert config.COOLDOWN_PATH.exists()

    def test_mode_interactif_capture_et_cible_le_bon_compte(
        self, repertoires_isoles, monkeypatch
    ):
        appels = []
        monkeypatch.setattr(maj_cookies, "capturer_session_interactive", _export_valide)
        monkeypatch.setattr(maj_cookies.shutil, "which", lambda _: "/usr/bin/gh")
        monkeypatch.setattr(
            maj_cookies,
            "maj_secret_github",
            lambda repo, cookies, appliquer, compte=None: appels.append(
                (repo, json.loads(cookies), appliquer, compte)
            ),
        )

        code = maj_cookies.main([
            "--interactive", "--repo", "owner/repo", "--compte", "4", "--set-secret"
        ])

        assert code == 0
        assert appels[0][0] == "owner/repo"
        assert appels[0][2:] == (True, "4")

    def test_exige_une_source_unique(self):
        with pytest.raises(SystemExit):
            maj_cookies.main([])
        with pytest.raises(SystemExit):
            maj_cookies.main(["export.json", "--interactive"])

    def test_mode_interactif_exige_set_secret(self, monkeypatch):
        monkeypatch.setattr(maj_cookies.shutil, "which", lambda _: "/usr/bin/gh")
        with pytest.raises(SystemExit):
            maj_cookies.main(["--interactive"])
