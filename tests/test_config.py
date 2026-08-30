"""Tests du module config : regex de filtrage, normalisation de quartiers, groupes."""

from __future__ import annotations

import pytest

import config


class TestEstCandidatFoncier:
    def test_annonce_vente_claire_est_acceptee(self):
        texte = (
            "A VENDRE : Parcelle de 600 m2 à Ouaga 2000, titre foncier disponible, "
            "prix 15 000 000 FCFA négociable."
        )
        assert config.est_candidat_foncier(texte) is True

    def test_recherche_achat_pure_est_rejetee(self):
        texte = "Je recherche un terrain à Saaba, budget limité, merci de me contacter."
        assert config.est_candidat_foncier(texte) is False

    def test_recherche_avec_signal_vente_est_acceptee(self):
        # Cas ambigu : republication d'une recherche qui mentionne aussi "à vendre".
        texte = "Qui a une parcelle à vendre du côté de Komsilga ? Merci de proposer."
        assert config.est_candidat_foncier(texte) is True

    def test_spam_evident_est_rejete(self):
        texte = "Cliquez ici pour gagner 5000$ en 24h !!! forex trading gratuit"
        assert config.est_candidat_foncier(texte) is False

    def test_texte_hors_sujet_est_rejete(self):
        texte = "Joyeux anniversaire à toute l'équipe du groupe !"
        assert config.est_candidat_foncier(texte) is False

    def test_texte_vide_est_rejete(self):
        assert config.est_candidat_foncier("") is False
        assert config.est_candidat_foncier("   ") is False
        assert config.est_candidat_foncier(None) is False  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "mot_cle",
        ["terrain", "parcelle", "titre foncier", "PUH", "attestation", "superficie", "hectare"],
    )
    def test_mots_cles_individuels_declenchent_le_filtre(self, mot_cle):
        texte = f"Bonjour, j'ai un bien avec {mot_cle} à vendre, prix à discuter."
        assert config.est_candidat_foncier(texte) is True

    def test_superficie_collee_au_chiffre_sans_espace(self):
        # Format très courant dans les annonces réelles ("600m2" sans espace) :
        # un \b classique entre un chiffre et une lettre ne matche jamais,
        # d'où l'ancre dédiée MOTIF_SUPERFICIE_NUMERIQUE. Texte volontairement
        # dépourvu de tout autre mot-clé déclencheur pour isoler ce cas précis.
        texte = "Bien de 600m2 situé à Ouaga, contactez-moi au besoin."
        assert config.MOTIF_FONCIER.search(texte) is None  # confirme l'isolation du cas testé
        assert config.est_candidat_foncier(texte) is True

    def test_hectare_abrege_colle_au_chiffre(self):
        texte = "Bien de 5ha situé à Komsilga, contactez-moi au besoin."
        assert config.MOTIF_FONCIER.search(texte) is None
        assert config.est_candidat_foncier(texte) is True

    def test_m2_avec_symbole_exposant(self):
        texte = "Bien de 300m² situé à Pissy, contactez-moi au besoin."
        assert config.MOTIF_FONCIER.search(texte) is None
        assert config.est_candidat_foncier(texte) is True

    def test_prix_multi_chiffres_est_detecte_comme_signal_de_vente(self):
        # Régression : "prix\s*:?\s*\d" (sans +) ne matchait qu'un seul chiffre.
        assert config.MOTIF_SIGNAL_VENTE.search("prix : 15000000 FCFA") is not None


class TestNormaliserQuartier:
    def test_correspondance_exacte(self):
        assert config.normaliser_quartier("Karpala") == "Karpala"

    def test_correspondance_insensible_a_la_casse(self):
        assert config.normaliser_quartier("ouaga 2000") == "Ouaga 2000"

    def test_valeur_absente_de_la_liste_est_conservee(self):
        # Ne doit pas inventer/forcer une correspondance approximative.
        assert config.normaliser_quartier("Quartier Inconnu XYZ") == "Quartier Inconnu XYZ"

    def test_valeur_none_retourne_none(self):
        assert config.normaliser_quartier(None) is None

    def test_chaine_vide_retourne_none(self):
        assert config.normaliser_quartier("") is None


class TestNormaliserStatutDocument:
    def test_correspondance_exacte(self):
        assert config.normaliser_statut_document("Titre foncier") == "Titre foncier"

    def test_correspondance_insensible_a_la_casse(self):
        # Régression du bug réel du 2026-08-03 : "attestation", "ATTESTATION"
        # et "Attestation" coexistaient comme 3 valeurs distinctes en base
        # car cette fonction existait mais n'était jamais appelée dans
        # processor.py (contrairement à normaliser_quartier).
        assert config.normaliser_statut_document("attestation") == "Attestation"
        assert config.normaliser_statut_document("ATTESTATION") == "Attestation"

    def test_attestation_dattribution_reste_distincte_dattestation(self):
        # Volontaire (voir commentaire dans config.py) : ne pas fusionner des
        # statuts sémantiquement différents juste parce qu'ils se
        # ressemblent - seule la casse/forme est normalisée.
        assert config.normaliser_statut_document("attestation d'attribution") == "Attestation d'attribution"
        assert config.normaliser_statut_document("Attestation") == "Attestation"

    def test_valeur_absente_de_la_liste_est_conservee(self):
        assert config.normaliser_statut_document("Un statut jamais vu") == "Un statut jamais vu"

    def test_valeur_none_retourne_none(self):
        assert config.normaliser_statut_document(None) is None

    def test_chaine_vide_retourne_none(self):
        assert config.normaliser_statut_document("") is None


class TestChargerGroupes:
    def test_charge_uniquement_les_groupes_actifs(self, fichier_groupes_valide):
        groupes = config.charger_groupes(chemin=fichier_groupes_valide)
        assert [g.id for g in groupes] == ["1111", "2222"]

    def test_ignore_les_lignes_todo(self, fichier_groupes_valide):
        groupes = config.charger_groupes(chemin=fichier_groupes_valide)
        assert all(not g.id.startswith("TODO") for g in groupes)

    def test_respecte_la_limite(self, fichier_groupes_valide):
        groupes = config.charger_groupes(chemin=fichier_groupes_valide, limite=1)
        assert len(groupes) == 1

    def test_fichier_absent_leve_une_erreur(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            config.charger_groupes(chemin=tmp_path / "inexistant.csv")

    def test_uniquement_des_lignes_todo_leve_une_erreur(self, tmp_path):
        chemin = tmp_path / "vide.csv"
        chemin.write_text(
            'id,nom,url,actif\nTODO_1,"x","https://x",false\n', encoding="utf-8"
        )
        with pytest.raises(ValueError):
            config.charger_groupes(chemin=chemin)

    def test_entetes_invalides_levent_une_erreur(self, tmp_path):
        chemin = tmp_path / "invalide.csv"
        chemin.write_text("colonne_a,colonne_b\n1,2\n", encoding="utf-8")
        with pytest.raises(ValueError):
            config.charger_groupes(chemin=chemin)

    def test_colonne_compte_absente_assigne_le_compte_1_par_defaut(
        self, fichier_groupes_valide
    ):
        # Rétrocompatibilité : un groups.csv historique (sans colonne
        # `compte`, comme la fixture) ne doit pas planter - tous les groupes
        # sont considérés comme appartenant au compte "1".
        groupes = config.charger_groupes(chemin=fichier_groupes_valide)
        assert all(g.compte == "1" for g in groupes)

    def test_filtre_par_compte(self, tmp_path):
        chemin = tmp_path / "groups.csv"
        chemin.write_text(
            "id,nom,url,actif,compte\n"
            '1111,"Groupe A","https://x/1111",true,1\n'
            '2222,"Groupe B","https://x/2222",true,2\n'
            '3333,"Groupe C","https://x/3333",true,1\n',
            encoding="utf-8",
        )
        groupes_compte_1 = config.charger_groupes(chemin=chemin, compte="1")
        assert [g.id for g in groupes_compte_1] == ["1111", "3333"]
        groupes_compte_2 = config.charger_groupes(chemin=chemin, compte="2")
        assert [g.id for g in groupes_compte_2] == ["2222"]

    def test_compte_absent_de_groups_csv_leve_une_erreur(self, tmp_path):
        chemin = tmp_path / "groups.csv"
        chemin.write_text(
            'id,nom,url,actif,compte\n1111,"Groupe A","https://x/1111",true,1\n',
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            config.charger_groupes(chemin=chemin, compte="2")

    def test_compte_invalide_leve_une_erreur(self, fichier_groupes_valide):
        with pytest.raises(ValueError):
            config.charger_groupes(chemin=fichier_groupes_valide, compte="9")

    def test_valeur_de_compte_invalide_dans_le_csv_leve_une_erreur(self, tmp_path):
        chemin = tmp_path / "groups.csv"
        chemin.write_text(
            'id,nom,url,actif,compte\n1111,"Groupe A","https://x/1111",true,9\n',
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            config.charger_groupes(chemin=chemin)

    @pytest.mark.parametrize("sous_domaine", ["www", "web"])
    def test_url_facebook_est_normalisee_vers_mobile(self, tmp_path, sous_domaine):
        chemin = tmp_path / "groups.csv"
        chemin.write_text(
            "id,nom,url,actif,compte\n"
            f'1111,"Groupe A","https://{sous_domaine}.facebook.com/groups/1111",true,1\n',
            encoding="utf-8",
        )
        groupe = config.charger_groupes(chemin=chemin, compte="1")[0]
        assert groupe.url == "https://m.facebook.com/groups/1111"


class TestEtatParCompte:
    """Isolation de l'état persistant (cookies, cooldown, santé, rotation)
    entre comptes Facebook - voir config.py, section "Multi-comptes"."""

    def test_compte_none_retourne_les_chemins_historiques(self):
        assert config.storage_state_path(None) == config.STORAGE_STATE_PATH
        assert config.cooldown_path(None) == config.COOLDOWN_PATH
        assert config.sante_path(None) == config.SANTE_PATH
        assert config.dernier_post_connu_path(None) == config.DERNIER_POST_CONNU_PATH
        assert (
            config.index_prochain_groupe_path(None)
            == config.INDEX_PROCHAIN_GROUPE_PATH
        )

    def test_chemins_distincts_par_compte(self):
        chemin_1 = config.storage_state_path("1")
        chemin_2 = config.storage_state_path("2")
        assert chemin_1 != chemin_2
        assert chemin_1 != config.STORAGE_STATE_PATH

    def test_compte_invalide_leve_une_erreur(self):
        with pytest.raises(ValueError):
            config.storage_state_path("9")

    def test_nom_secret_cookies(self):
        assert config.nom_secret_cookies(None) == "FB_COOKIES_JSON"
        assert config.nom_secret_cookies("3") == "FB_COOKIES_JSON_3"

class TestProxyPlaywright:
    """Parsing de l'URL de proxy optionnelle (voir config.proxy_playwright,
    section README "Proxy (réputation IP/ASN)")."""

    def test_nom_secret_proxy(self):
        assert config.nom_secret_proxy(None) == "PROXY_URL"
        assert config.nom_secret_proxy("3") == "PROXY_URL_3"

    def test_absent_retourne_none(self, monkeypatch):
        monkeypatch.delenv("PROXY_URL", raising=False)
        assert config.proxy_playwright(None) is None

    def test_vide_multicompte_leve_une_erreur(self, monkeypatch):
        monkeypatch.delenv("ALLOW_DIRECT_CONNECTION", raising=False)
        monkeypatch.setenv("PROXY_URL_2", "   ")
        with pytest.raises(ValueError, match="PROXY_URL_2"):
            config.proxy_playwright("2")

    @pytest.mark.parametrize("valeur", ["1", "true", "vrai", "yes", "oui", " TRUE "])
    def test_connexion_directe_explicitement_autorisee(self, monkeypatch, valeur):
        monkeypatch.setenv("ALLOW_DIRECT_CONNECTION", valeur)
        monkeypatch.setenv("PROXY_URL_2", "   ")
        assert config.proxy_playwright("2") is None

    def test_vide_reste_optionnel_en_mono_compte(self, monkeypatch):
        monkeypatch.setenv("PROXY_URL", "   ")
        assert config.proxy_playwright(None) is None

    def test_avec_authentification(self, monkeypatch):
        monkeypatch.setenv("PROXY_URL_1", "http://alice:s3cret@proxy.example.com:8080")
        assert config.proxy_playwright("1") == {
            "server": "http://proxy.example.com:8080",
            "username": "alice",
            "password": "s3cret",
        }

    def test_sans_authentification(self, monkeypatch):
        monkeypatch.setenv("PROXY_URL_4", "http://proxy.example.com:3128")
        assert config.proxy_playwright("4") == {
            "server": "http://proxy.example.com:3128",
        }

    def test_identifiants_encodes_url_sont_decodes(self, monkeypatch):
        monkeypatch.setenv(
            "PROXY_URL_5", "http://user%40x:p%40ss@proxy.example.com:8080"
        )
        proxy = config.proxy_playwright("5")
        assert proxy["username"] == "user@x"
        assert proxy["password"] == "p@ss"

    def test_url_illisible_multicompte_leve_une_erreur(self, monkeypatch):
        monkeypatch.setenv("PROXY_URL_3", "ceci-nest-pas-une-url-de-proxy")
        with pytest.raises(ValueError, match="PROXY_URL_3"):
            config.proxy_playwright("3")

    def test_schema_non_supporte_est_refuse(self, monkeypatch):
        monkeypatch.setenv("PROXY_URL_1", "ftp://proxy.example.com:21")
        with pytest.raises(ValueError, match="invalide"):
            config.proxy_playwright("1")


class TestProfilMobileEtRegion:
    def test_profil_est_stable_et_distinct_par_compte(self):
        profils = [config.choisir_fingerprint_mobile(str(i)) for i in range(1, 6)]
        assert len({ua for ua, _ in profils}) == 5
        assert config.choisir_fingerprint_mobile("2") == profils[1]
        assert all("Android" in ua and "iPhone" not in ua for ua, _ in profils)

    def test_viewport_retourne_est_une_copie(self):
        _, viewport = config.choisir_fingerprint_mobile("1")
        viewport["width"] = 1
        assert config.choisir_fingerprint_mobile("1")[1]["width"] == 360

    def test_region_par_defaut_est_burkina(self, monkeypatch):
        for nom in ("PROXY_COUNTRY", "BROWSER_LOCALE", "BROWSER_TIMEZONE"):
            monkeypatch.delenv(nom, raising=False)
            monkeypatch.delenv(f"{nom}_1", raising=False)
        region = config.parametres_regionaux("1")
        assert region.pays == "BF"
        assert region.locale == "fr-FR"
        assert region.fuseau_horaire == "Africa/Ouagadougou"

    def test_region_peut_etre_surchargee_par_compte(self, monkeypatch):
        monkeypatch.setenv("PROXY_COUNTRY_3", "ci")
        monkeypatch.setenv("BROWSER_LOCALE_3", "fr-CI")
        monkeypatch.setenv("BROWSER_TIMEZONE_3", "Africa/Abidjan")
        region = config.parametres_regionaux("3")
        assert (region.pays, region.locale, region.fuseau_horaire) == (
            "CI", "fr-CI", "Africa/Abidjan"
        )
