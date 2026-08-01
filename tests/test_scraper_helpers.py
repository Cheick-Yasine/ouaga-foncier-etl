"""Tests des fonctions pures/synchrones de scraper.py.

Les fonctions qui pilotent un vrai navigateur (creer_navigateur, scraper_groupe,
extraire_posts_visibles) nécessitent une session Playwright/Chromium et ne sont
PAS couvertes ici - voir la limite documentée dans README.md. On teste tout ce
qui peut l'être sans navigateur : parsing, validation, persistance.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import config
import scraper


class TestChargerCookies:
    def test_cookies_valides_sont_acceptes(self):
        brut = json.dumps([
            {"name": "c_user", "value": "123", "domain": ".facebook.com"},
            {"name": "xs", "value": "abc", "domain": ".facebook.com"},
        ])
        cookies = scraper.charger_cookies(brut)
        assert len(cookies) == 2
        assert cookies[0]["path"] == "/"  # valeur par défaut ajoutée

    def test_json_invalide_leve_value_error(self):
        with pytest.raises(ValueError):
            scraper.charger_cookies("{ceci n'est pas du json")

    def test_liste_vide_leve_value_error(self):
        with pytest.raises(ValueError):
            scraper.charger_cookies("[]")

    def test_objet_au_lieu_de_liste_leve_value_error(self):
        with pytest.raises(ValueError):
            scraper.charger_cookies(json.dumps({"name": "c_user"}))

    def test_champ_requis_manquant_leve_value_error(self):
        brut = json.dumps([{"name": "c_user"}])  # "value" et "domain" manquants
        with pytest.raises(ValueError):
            scraper.charger_cookies(brut)

    def test_export_extension_navigateur_est_converti_au_format_playwright(self):
        # Format réel d'un export d'extension de navigateur (chrome.cookies) :
        # expirationDate au lieu de expires, sameSite en minuscules avec des
        # valeurs hors de l'enum Playwright, clés inconnues de Playwright.
        # Valeurs synthétiques - jamais de vrai cookie de session dans les tests.
        brut = json.dumps([
            {
                "domain": ".facebook.com", "expirationDate": 1999999999.5,
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
            {
                # cookie de session : pas d'expirationDate, sameSite absent
                "domain": ".facebook.com", "hostOnly": False, "httpOnly": False,
                "name": "presence", "path": "/", "sameSite": None, "secure": True,
                "session": True, "storeId": None, "value": "test_presence_value",
            },
        ])
        cookies = scraper.charger_cookies(brut)
        par_nom = {c["name"]: c for c in cookies}

        # Clés non reconnues par Playwright supprimées.
        for c in cookies:
            assert set(c).issubset({"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"})

        assert par_nom["xs"]["sameSite"] == "None"  # no_restriction -> None
        assert par_nom["xs"]["expires"] == 1999999999.5  # expirationDate -> expires
        assert par_nom["c_user"]["sameSite"] == "Lax"  # lax -> Lax

        assert "expires" not in par_nom["presence"]  # cookie de session : pas de date inventée
        assert "sameSite" not in par_nom["presence"]  # valeur absente : pas de défaut inventé

    def test_sameSite_non_reconnu_est_ignore_sans_lever_derreur(self):
        brut = json.dumps([
            {"name": "c_user", "value": "v", "domain": ".facebook.com", "sameSite": "valeur_inconnue"},
        ])
        cookies = scraper.charger_cookies(brut)
        assert "sameSite" not in cookies[0]

    def test_format_playwright_natif_reste_accepte(self):
        # Rétrocompatibilité : un cookie déjà au format Playwright (expires,
        # sameSite en PascalCase) doit passer sans être altéré.
        brut = json.dumps([
            {"name": "c_user", "value": "v", "domain": ".facebook.com",
             "path": "/", "expires": 1999999999.0, "sameSite": "Strict"},
        ])
        cookies = scraper.charger_cookies(brut)
        assert cookies[0]["expires"] == 1999999999.0
        assert cookies[0]["sameSite"] == "Strict"


class TestExtraireIdDepuisUrl:
    @pytest.mark.parametrize(
        "url,id_attendu",
        [
            ("https://www.facebook.com/groups/123/posts/456789/", "456789"),
            ("https://www.facebook.com/groups/123/permalink/456789/", "456789"),
            ("https://www.facebook.com/story.php?story_fbid=456789&id=123", "456789"),
            (None, None),
        ],
    )
    def test_extraction(self, url, id_attendu):
        assert scraper._extraire_id_depuis_url(url) == id_attendu

    def test_url_sans_motif_connu_retourne_url_telle_quelle(self):
        url = "https://www.facebook.com/groups/123/"
        assert scraper._extraire_id_depuis_url(url) == url


class TestSeenIds:
    def test_charger_sans_fichier_retourne_dict_vide(self, repertoires_isoles):
        assert scraper.charger_seen_ids() == {}

    def test_sauvegarder_puis_charger_roundtrip(self, repertoires_isoles):
        maintenant = datetime.now(timezone.utc).isoformat()
        scraper.sauvegarder_seen_ids({"p1": maintenant, "p2": maintenant})
        recharge = scraper.charger_seen_ids()
        assert set(recharge) == {"p1", "p2"}

    def test_purge_les_entrees_trop_anciennes(self, repertoires_isoles):
        recent = datetime.now(timezone.utc).isoformat()
        ancien = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        scraper.sauvegarder_seen_ids({"recent": recent, "ancien": ancien}, retention_jours=90)
        recharge = scraper.charger_seen_ids()
        assert "recent" in recharge
        assert "ancien" not in recharge

    def test_fichier_corrompu_retourne_dict_vide_sans_planter(self, repertoires_isoles):
        config.SEEN_IDS_PATH.write_text("{pas du json valide", encoding="utf-8")
        assert scraper.charger_seen_ids() == {}


class TestParserHorodatageRelatif:
    """Le parseur d'horodatage mbasic est une fonction pure - contrairement à
    l'ancienne extraction (jamais implémentée faute d'accès DOM live), elle
    est intégralement testable hors-ligne.
    """

    MAINTENANT = datetime(2026, 8, 1, 15, 0, 0, tzinfo=timezone.utc)

    def test_minutes(self):
        resultat = scraper._parser_horodatage_relatif("12 min", self.MAINTENANT)
        assert resultat == self.MAINTENANT - timedelta(minutes=12)

    def test_heures(self):
        resultat = scraper._parser_horodatage_relatif("3 h", self.MAINTENANT)
        assert resultat == self.MAINTENANT - timedelta(hours=3)

    def test_jours(self):
        resultat = scraper._parser_horodatage_relatif("5 j", self.MAINTENANT)
        assert resultat == self.MAINTENANT - timedelta(days=5)

    def test_a_linstant(self):
        assert scraper._parser_horodatage_relatif("à l'instant", self.MAINTENANT) == self.MAINTENANT

    def test_hier_sans_heure(self):
        resultat = scraper._parser_horodatage_relatif("Hier", self.MAINTENANT)
        assert resultat == datetime(2026, 7, 31, 0, 0, tzinfo=timezone.utc)

    def test_hier_avec_heure(self):
        resultat = scraper._parser_horodatage_relatif("Hier à 14:30", self.MAINTENANT)
        assert resultat == datetime(2026, 7, 31, 14, 30, tzinfo=timezone.utc)

    def test_aujourdhui_avec_heure(self):
        resultat = scraper._parser_horodatage_relatif("Aujourd'hui à 09:15", self.MAINTENANT)
        assert resultat == datetime(2026, 8, 1, 9, 15, tzinfo=timezone.utc)

    def test_date_avec_mois_sans_annee_passee(self):
        # "1 août" un 1er août à 15h -> plus tôt le même jour, pas dans le futur.
        resultat = scraper._parser_horodatage_relatif("1 août", self.MAINTENANT)
        assert resultat == datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

    def test_date_sans_annee_dans_le_futur_recule_dun_an(self):
        maintenant = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
        resultat = scraper._parser_horodatage_relatif("1 août", maintenant)
        assert resultat == datetime(2025, 8, 1, 12, 0, tzinfo=timezone.utc)

    def test_date_avec_annee_explicite(self):
        resultat = scraper._parser_horodatage_relatif("3 mars 2024", self.MAINTENANT)
        assert resultat == datetime(2024, 3, 3, 12, 0, tzinfo=timezone.utc)

    def test_date_avec_heure_et_annee(self):
        resultat = scraper._parser_horodatage_relatif("3 mars 2024 à 18:45", self.MAINTENANT)
        assert resultat == datetime(2024, 3, 3, 18, 45, tzinfo=timezone.utc)

    def test_date_invalide_retourne_none(self):
        assert scraper._parser_horodatage_relatif("31 février", self.MAINTENANT) is None

    def test_texte_non_reconnu_retourne_none(self):
        assert scraper._parser_horodatage_relatif("mardi prochain", self.MAINTENANT) is None

    def test_texte_vide_ou_none_retourne_none(self):
        assert scraper._parser_horodatage_relatif("", self.MAINTENANT) is None
        assert scraper._parser_horodatage_relatif(None, self.MAINTENANT) is None

    def test_insensible_a_la_casse(self):
        resultat = scraper._parser_horodatage_relatif("3 H", self.MAINTENANT)
        assert resultat == self.MAINTENANT - timedelta(hours=3)


class TestCooldown:
    def test_aucun_fichier_pas_de_cooldown(self, repertoires_isoles):
        assert scraper.verifier_cooldown() is None

    def test_cooldown_actif_est_detecte(self, repertoires_isoles):
        scraper.activer_cooldown(heures=24, raison="test")
        fin = scraper.verifier_cooldown()
        assert fin is not None
        assert fin > datetime.now(timezone.utc)

    def test_cooldown_expire_nest_plus_actif(self, repertoires_isoles):
        # Cooldown déjà terminé dans le passé -> ne doit plus bloquer.
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        passe = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        config.COOLDOWN_PATH.write_text(
            json.dumps({"jusqu_a": passe, "raison": "expiré"}), encoding="utf-8"
        )
        assert scraper.verifier_cooldown() is None

    def test_fichier_cooldown_corrompu_nest_pas_bloquant(self, repertoires_isoles):
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        config.COOLDOWN_PATH.write_text("{pas du json", encoding="utf-8")
        assert scraper.verifier_cooldown() is None


class TestThrottleAdaptatif:
    """Le throttle AIMD est composé de fonctions pures (état -> état), donc
    testable sans navigateur ni fichier - sauf charger_sante/sauvegarder_sante
    qui touchent le disque et utilisent `repertoires_isoles`.
    """

    def test_charger_sante_sans_fichier_retourne_confiance_maximale(self, repertoires_isoles):
        etat = scraper.charger_sante()
        assert etat["niveau_confiance"] == config.NIVEAU_CONFIANCE_INITIAL
        assert etat["runs_propres_consecutifs"] == 0
        assert etat["cooldown_multiplicateur"] == 1

    def test_sauvegarder_puis_charger_roundtrip(self, repertoires_isoles):
        scraper.sauvegarder_sante({"niveau_confiance": 0.5, "runs_propres_consecutifs": 2, "cooldown_multiplicateur": 4})
        etat = scraper.charger_sante()
        assert etat["niveau_confiance"] == 0.5
        assert etat["runs_propres_consecutifs"] == 2
        assert etat["cooldown_multiplicateur"] == 4

    def test_charger_sante_fichier_corrompu_retourne_defaut(self, repertoires_isoles):
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        config.SANTE_PATH.write_text("{pas du json", encoding="utf-8")
        etat = scraper.charger_sante()
        assert etat["niveau_confiance"] == config.NIVEAU_CONFIANCE_INITIAL

    def test_ajustements_confiance_maximale(self):
        ajustements = scraper.calculer_ajustements({"niveau_confiance": 1.0})
        assert ajustements.delai_multiplicateur == 1.0
        assert ajustements.ratio_groupes == 1.0

    def test_ajustements_confiance_reduite_rallonge_delais_et_reduit_volume(self):
        ajustements = scraper.calculer_ajustements({"niveau_confiance": 0.5})
        assert ajustements.delai_multiplicateur == 2.0
        assert ajustements.ratio_groupes == 0.5

    def test_ajustements_bornes_respectees(self):
        # Valeur hors bornes dans le fichier (corruption/édition manuelle) -> clampée.
        bas = scraper.calculer_ajustements({"niveau_confiance": 0.0})
        assert bas.ratio_groupes == config.NIVEAU_CONFIANCE_MIN
        haut = scraper.calculer_ajustements({"niveau_confiance": 5.0})
        assert haut.ratio_groupes == config.NIVEAU_CONFIANCE_MAX

    def test_blocage_fait_chuter_la_confiance_au_plancher(self):
        etat = {"niveau_confiance": 1.0, "runs_propres_consecutifs": 2, "cooldown_multiplicateur": 1}
        nouvel_etat = scraper.mettre_a_jour_apres_run(etat, anomalies=0, total_groupes=5, bloque=True)
        assert nouvel_etat["niveau_confiance"] == config.NIVEAU_CONFIANCE_MIN
        assert nouvel_etat["runs_propres_consecutifs"] == 0

    def test_blocage_double_le_multiplicateur_de_cooldown(self):
        etat = {"niveau_confiance": 1.0, "runs_propres_consecutifs": 0, "cooldown_multiplicateur": 2}
        nouvel_etat = scraper.mettre_a_jour_apres_run(etat, anomalies=0, total_groupes=5, bloque=True)
        assert nouvel_etat["cooldown_multiplicateur"] == 4

    def test_cooldown_multiplicateur_plafonne(self):
        etat = {"niveau_confiance": 1.0, "runs_propres_consecutifs": 0, "cooldown_multiplicateur": config.COOLDOWN_MULTIPLICATEUR_MAX}
        nouvel_etat = scraper.mettre_a_jour_apres_run(etat, anomalies=0, total_groupes=5, bloque=True)
        assert nouvel_etat["cooldown_multiplicateur"] == config.COOLDOWN_MULTIPLICATEUR_MAX

    def test_session_expiree_reduit_la_confiance_moins_durement_quun_blocage(self):
        etat = {"niveau_confiance": 1.0, "runs_propres_consecutifs": 0, "cooldown_multiplicateur": 1}
        nouvel_etat = scraper.mettre_a_jour_apres_run(etat, anomalies=0, total_groupes=5, session_expiree=True)
        assert config.NIVEAU_CONFIANCE_MIN < nouvel_etat["niveau_confiance"] < 1.0

    def test_beaucoup_danomalies_declenche_une_suspicion(self):
        etat = {"niveau_confiance": 1.0, "runs_propres_consecutifs": 0, "cooldown_multiplicateur": 1}
        # 4 anomalies sur 5 groupes = 80% > seuil de 30%
        nouvel_etat = scraper.mettre_a_jour_apres_run(etat, anomalies=4, total_groupes=5)
        assert nouvel_etat["niveau_confiance"] < 1.0
        assert nouvel_etat["runs_propres_consecutifs"] == 0

    def test_peu_danomalies_ne_declenche_rien_et_compte_le_run_propre(self):
        etat = {"niveau_confiance": 0.8, "runs_propres_consecutifs": 0, "cooldown_multiplicateur": 1}
        nouvel_etat = scraper.mettre_a_jour_apres_run(etat, anomalies=0, total_groupes=5)
        assert nouvel_etat["niveau_confiance"] == 0.8  # pas encore de ramp-up
        assert nouvel_etat["runs_propres_consecutifs"] == 1

    def test_runs_propres_consecutifs_font_remonter_la_confiance(self):
        etat = {"niveau_confiance": 0.5, "runs_propres_consecutifs": config.RUNS_PROPRES_POUR_RAMPUP - 1, "cooldown_multiplicateur": 3}
        nouvel_etat = scraper.mettre_a_jour_apres_run(etat, anomalies=0, total_groupes=5)
        assert nouvel_etat["niveau_confiance"] == pytest.approx(0.5 + config.RAMPUP_INCREMENT)
        assert nouvel_etat["runs_propres_consecutifs"] == 0
        assert nouvel_etat["cooldown_multiplicateur"] == 1  # reset après un vrai streak propre

    def test_ramp_up_ne_depasse_jamais_le_maximum(self):
        etat = {
            "niveau_confiance": config.NIVEAU_CONFIANCE_MAX,
            "runs_propres_consecutifs": config.RUNS_PROPRES_POUR_RAMPUP - 1,
            "cooldown_multiplicateur": 1,
        }
        nouvel_etat = scraper.mettre_a_jour_apres_run(etat, anomalies=0, total_groupes=5)
        assert nouvel_etat["niveau_confiance"] == config.NIVEAU_CONFIANCE_MAX

    def test_zero_groupe_ne_plante_pas(self):
        etat = {"niveau_confiance": 1.0, "runs_propres_consecutifs": 0, "cooldown_multiplicateur": 1}
        nouvel_etat = scraper.mettre_a_jour_apres_run(etat, anomalies=0, total_groupes=0)
        assert nouvel_etat["niveau_confiance"] == 1.0


class TestSauvegarderPostsGroupe:
    def test_cree_un_fichier_json_dans_raw_dir(self, repertoires_isoles):
        posts = [{"id": "p1", "texte": "test"}]
        chemin = scraper.sauvegarder_posts_groupe(posts, "1111")
        assert chemin.exists()
        assert chemin.parent == config.RAW_DIR
        contenu = json.loads(chemin.read_text(encoding="utf-8"))
        assert contenu == posts
