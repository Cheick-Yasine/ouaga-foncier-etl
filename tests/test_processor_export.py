"""Tests de l'export CSV/JSON et du chargement/dédoublonnage des fichiers bruts."""

from __future__ import annotations

import csv
import json

import processor


class TestExporterCsv:
    def test_ecrit_les_bonnes_colonnes_et_lignes(self, tmp_path):
        annonces = [
            {
                "id": "p1", "groupe_nom": "Test", "url": "https://x", "date_publication": None,
                "date_incertaine": True, "type_bien": "parcelle", "quartier_zone": "Ouaga 2000",
                "superficie_m2": 600, "prix_fcfa": 15000000, "statut_document": "Titre Foncier",
                "contacts_whatsapp": ["70123456", "76000000"], "mots_cles_pertinents": ["parcelle"],
                "resume_court": "Résumé test", "texte_nettoye": "texte complet",
            }
        ]
        chemin = tmp_path / "sortie.csv"
        processor.exporter_csv(annonces, chemin)

        with chemin.open(encoding="utf-8-sig") as f:
            lignes = list(csv.DictReader(f))

        assert len(lignes) == 1
        assert lignes[0]["id"] == "p1"
        assert lignes[0]["contacts_whatsapp"] == "70123456; 76000000"
        assert lignes[0]["superficie_m2"] == "600"

    def test_liste_vide_produit_un_csv_avec_en_tetes_seulement(self, tmp_path):
        chemin = tmp_path / "vide.csv"
        processor.exporter_csv([], chemin)
        with chemin.open(encoding="utf-8-sig") as f:
            lignes = list(csv.DictReader(f))
        assert lignes == []

    def test_valeurs_manquantes_deviennent_chaine_vide(self, tmp_path):
        annonces = [{"id": "p1"}]  # toutes les autres colonnes absentes
        chemin = tmp_path / "partiel.csv"
        processor.exporter_csv(annonces, chemin)
        with chemin.open(encoding="utf-8-sig") as f:
            ligne = next(csv.DictReader(f))
        assert ligne["prix_fcfa"] == ""
        assert ligne["quartier_zone"] == ""


class TestExporterJsonAudit:
    def test_sauvegarde_les_rejetes(self, tmp_path):
        rejetes = [{"id": "p2", "motif_rejet": "regex_niveau1"}]
        chemin = tmp_path / "audit.json"
        processor.exporter_json_audit(rejetes, chemin)
        contenu = json.loads(chemin.read_text(encoding="utf-8"))
        assert contenu == rejetes


class TestChargerPostsBruts:
    def test_fusionne_plusieurs_fichiers(self, tmp_path):
        f1 = tmp_path / "a.json"
        f2 = tmp_path / "b.json"
        f1.write_text(json.dumps([{"id": "p1", "texte": "A"}]), encoding="utf-8")
        f2.write_text(json.dumps([{"id": "p2", "texte": "B"}]), encoding="utf-8")

        posts = processor.charger_posts_bruts([f1, f2])
        assert {p["id"] for p in posts} == {"p1", "p2"}

    def test_deduplique_par_id_entre_fichiers(self, tmp_path):
        f1 = tmp_path / "a.json"
        f2 = tmp_path / "b.json"
        f1.write_text(json.dumps([{"id": "p1", "texte": "ancienne version"}]), encoding="utf-8")
        f2.write_text(json.dumps([{"id": "p1", "texte": "nouvelle version"}]), encoding="utf-8")

        posts = processor.charger_posts_bruts([f1, f2])
        assert len(posts) == 1
        assert posts[0]["texte"] == "nouvelle version"  # le dernier fichier gagne

    def test_fichier_illisible_est_ignore_sans_planter(self, tmp_path, caplog):
        f_corrompu = tmp_path / "corrompu.json"
        f_corrompu.write_text("{ceci n'est pas du json valide", encoding="utf-8")

        posts = processor.charger_posts_bruts([f_corrompu])
        assert posts == []
