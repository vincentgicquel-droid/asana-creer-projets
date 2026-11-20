#!/usr/bin/env python3
"""Tests unitaires de creer-projets.py (aucun appel reseau).

Lancer : python3 test_creation.py
"""

import importlib.util
import unittest
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "creer_projets", Path(__file__).with_name("creer-projets.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class NormalisationTests(unittest.TestCase):
    def test_normalize_tolere_accents_casse_emojis(self):
        self.assertEqual(module.normalize("🚨🚀 Niveau stratégique T&O"), "niveau strategique t o")
        self.assertEqual(module.normalize("PRIORITE"), module.normalize("Priorité"))
        self.assertEqual(module.normalize("START DATE"), module.normalize("start_date"))

    def test_extract_project_gid(self):
        url = "https://app.asana.com/1/1200000000000001/project/1200000000000002"
        self.assertEqual(module.extract_project_gid(url), "1200000000000002")

    def test_extract_project_gid_vide(self):
        self.assertIsNone(module.extract_project_gid(""))
        self.assertIsNone(module.extract_project_gid(None))
        self.assertIsNone(module.extract_project_gid("https://exemple.fr"))

    def test_valid_dates(self):
        self.assertEqual(module.valid_dates("2024-11-02", "2027-01-22"), ("2024-11-02", "2027-01-22", None))
        self.assertEqual(module.valid_dates(None, "2027-01-22"), (None, "2027-01-22", None))
        # debut sans echeance : ignore avec alerte
        start, due, alerte = module.valid_dates("2024-11-02", None)
        self.assertIsNone(start)
        self.assertIsNone(due)
        self.assertTrue(alerte)
        # debut apres echeance : tout ignore
        start, due, alerte = module.valid_dates("2027-01-22", "2024-11-02")
        self.assertIsNone(start)
        self.assertIsNone(due)
        self.assertTrue(alerte)

    def test_parse_number_vide(self):
        self.assertIsNone(module.parse_number(""))
        self.assertIsNone(module.parse_number(None))
        self.assertEqual(module.parse_number("5000"), 5000.0)
        self.assertEqual(module.parse_number("63,9"), 63.9)

    def test_to_iso_date(self):
        self.assertEqual(module.to_iso_date("2024-11-02"), "2024-11-02")
        self.assertEqual(module.to_iso_date("02/11/2024"), "2024-11-02")
        self.assertIsNone(module.to_iso_date("  "))


class SourceColumnTests(unittest.TestCase):
    def test_colonne_exacte(self):
        row = {"NAME": "x", "PRJ_CODE": "PRJ1"}
        self.assertEqual(module.source_column(row, "NAME"), "NAME")

    def test_colonne_avec_variantes(self):
        row = {"Start Date": "2024-11-02"}
        self.assertEqual(module.source_column(row, "START DATE"), "Start Date")
        row = {"PRJ_CODE ": "x"}
        self.assertEqual(module.source_column(row, "PRJ_CODE"), "PRJ_CODE ")

    def test_colonne_absente(self):
        self.assertIsNone(module.source_column({"A": "1"}, "B"))

    def test_colonne_ambigue(self):
        row = {"prj_code": "x", "PRJ  CODE": "y"}
        with self.assertRaises(RuntimeError):
            module.source_column(row, "PRJ_CODE")


class BuildPayloadTests(unittest.TestCase):
    def setUp(self):
        self.fields = [
            {"gid": "1", "name": "Budget Initial", "subtype": "number", "options": {}},
            {"gid": "2", "name": "Pluriannuel", "subtype": "enum",
             "options": {"oui": "201", "non": "202"}},
            {"gid": "3", "name": "Date d'arrete", "subtype": "date", "options": {}},
            {"gid": "4", "name": "CDP", "subtype": "people", "options": {}},
            {"gid": "5", "name": "Directions", "subtype": "multi_enum",
             "options": {"inf": "301", "finances": "302"}},
            {"gid": "6", "name": "Commentaire", "subtype": "text", "options": {}},
        ]
        self.mapping = {
            "Budget Initial": "1",
            "Pluriannuel": "2",
            "Date d'arrete": "3",
            "CDP": "4",
            "Directions": "5",
            "Commentaire": "6",
        }
        self.users = module.UserCache("pat", "ws")
        self.users._by_email = {"vincent@exemple.fr": "999"}

    def payload(self, row):
        return module.build_payload(row, self.mapping, self.fields, self.users)

    def test_number_enum_date_text(self):
        row = {"Budget Initial": "4500000", "Pluriannuel": "Oui",
               "Date d'arrete": "30/09/2026", "Commentaire": " ok "}
        payload, alertes = self.payload(row)
        self.assertEqual(payload["1"], 4500000.0)
        self.assertEqual(payload["2"], "201")
        self.assertEqual(payload["3"], {"date": "2026-09-30"})
        self.assertEqual(payload["6"], "ok")
        self.assertEqual(alertes, [])

    def test_people_par_email(self):
        payload, alertes = self.payload({"CDP": "vincent@exemple.fr"})
        self.assertEqual(payload["4"], ["999"])
        self.assertEqual(alertes, [])

    def test_people_email_inconnu_alerte(self):
        payload, alertes = self.payload({"CDP": "inconnu@exemple.fr"})
        self.assertNotIn("4", payload)
        self.assertEqual(len(alertes), 1)

    def test_multi_enum_separateurs(self):
        payload, alertes = self.payload({"Directions": "INF, Finances"})
        self.assertEqual(payload["5"], ["301", "302"])
        self.assertEqual(alertes, [])

    def test_enum_option_absente(self):
        payload, alertes = self.payload({"Pluriannuel": "Peut-etre"})
        self.assertNotIn("2", payload)
        self.assertEqual(len(alertes), 1)

    def test_colonne_absente_du_csv(self):
        row = {k: "" for k in self.mapping}
        del row["Commentaire"]
        payload, alertes = self.payload(row)
        self.assertNotIn("6", payload)
        self.assertEqual(alertes, [])

    def test_vide_rien_ecrire(self):
        row = {k: "" for k in self.mapping}
        payload, alertes = self.payload(row)
        self.assertEqual(payload, {})

    def test_enum_par_gid_brut(self):
        payload, alertes = self.payload({"Pluriannuel": "201"})
        self.assertEqual(payload["2"], "201")
        self.assertEqual(alertes, [])


class BuildRequestedDatesTests(unittest.TestCase):
    COL_START = "START DATE"
    COL_DUE = "DUE DATE"

    def test_debut_echeance_variables_projet(self):
        variables = [{"gid": "1", "name": "Project Start Date"},
                     {"gid": "2", "name": "Project Due Date"}]
        row = {"START DATE": "2024-11-02", "DUE DATE": "2027-01-22"}
        dates, alertes = module.build_requested_dates(variables, row, self.COL_START, self.COL_DUE)
        self.assertEqual(dates, [{"gid": "1", "value": "2024-11-02"},
                                 {"gid": "2", "value": "2027-01-22"}])
        self.assertEqual(alertes, [])

    def test_variable_nommee_depuis_une_colonne(self):
        variables = [{"gid": "1", "name": "Project Start Date"},
                     {"gid": "777", "name": "Date de cadrage"}]
        row = {"START DATE": "2024-11-02", "Date de cadrage": "15/01/2025"}
        dates, alertes = module.build_requested_dates(variables, row, self.COL_START, self.COL_DUE)
        self.assertIn({"gid": "777", "value": "2025-01-15"}, dates)
        self.assertIn({"gid": "1", "value": "2024-11-02"}, dates)
        self.assertEqual(alertes, [])

    def test_variable_absente_alerte_defaut(self):
        variables = [{"gid": "777", "name": "Date de cadrage"}]
        row = {"START DATE": "2024-11-02"}
        dates, alertes = module.build_requested_dates(variables, row, self.COL_START, self.COL_DUE)
        self.assertEqual(dates, [])
        self.assertEqual(len(alertes), 1)
        self.assertIn("defaut", alertes[0])

    def test_sans_variable(self):
        dates, alertes = module.build_requested_dates([], {"START DATE": "2024-11-02"}, self.COL_START, self.COL_DUE)
        self.assertEqual(dates, [])
        self.assertEqual(alertes, [])


class ResolveMappingTests(unittest.TestCase):
    def test_resolution_par_nom_normalise(self):
        fields = [{"gid": "10", "name": "Niveau stratégique T&O", "subtype": "enum", "options": {}}]
        resolved = module.resolve_mapping({"Niveau stratégique T&O": "10"}, fields)
        self.assertEqual(resolved, {"Niveau stratégique T&O": "10"})

    def test_doublon_interdit(self):
        fields = [{"gid": "10", "name": "A", "subtype": "text", "options": {}}]
        with self.assertRaises(RuntimeError):
            module.resolve_mapping({"col1": "A", "col2": "A"}, fields)

    def test_champ_absent_ignore(self):
        fields = [{"gid": "10", "name": "A", "subtype": "text", "options": {}}]
        resolved = module.resolve_mapping({"col1": "A", "col2": "Inexistant"}, fields)
        self.assertEqual(resolved, {"col1": "10"})


class IndexItemsTests(unittest.TestCase):
    def test_index_gid_code_nom(self):
        items = [{"gid": "555", "name": "Mon projet"}]
        details = {"555": {"custom_fields": [
            {"gid": "9", "name": "PRJ_CODE", "text_value": "PRJ6098787"},
        ]}}
        index = module.index_items(items, details, "PRJ_CODE")
        self.assertIn("555", index)
        self.assertIn("prj6098787", index)
        self.assertIn("mon projet", index)
        self.assertIs(index["prj6098787"], items[0])

    def test_colonne_code_absente_des_projets(self):
        items = [{"gid": "555", "name": "Mon projet"}]
        details = {"555": {"custom_fields": []}}
        index = module.index_items(items, details, "PRJ_CODE")
        self.assertIn("555", index)
        self.assertIn("mon projet", index)


class CollectFieldDefsTests(unittest.TestCase):
    def test_options_enum_collectees(self):
        details = {"custom_fields": [
            {"gid": "2", "name": "Pluriannuel", "resource_subtype": "enum",
             "enum_options": [{"gid": "201", "name": "Oui", "enabled": True},
                              {"gid": "202", "name": "Non", "enabled": True}]},
        ]}
        fields = module.collect_field_defs([details])
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0]["options"]["oui"], "201")
        self.assertEqual(fields[0]["options"]["201"], "201")

    def test_fusion_deux_projets(self):
        d1 = {"custom_fields": [{"gid": "2", "name": "A", "resource_subtype": "text", "enum_options": []}]}
        d2 = {"custom_fields": [{"gid": "2", "name": "A", "resource_subtype": "text",
                                 "enum_options": [{"gid": "x", "name": "opt", "enabled": True}]}]}
        fields = module.collect_field_defs([d1, d2])
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0]["options"]["opt"], "x")


if __name__ == "__main__":
    unittest.main()
