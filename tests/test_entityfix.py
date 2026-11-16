"""entityfix: comment neutralization, parsing, ambient detection, cull insertion.

BUG-03/04/05 (expectedFailure): the tokenizer drops numeric tokens, so
`cull_radius = 50` and `attach = { 0 = "x" }` are not recognized; nested
blocks (`sound = { name = ... }`) overwrite entity-level fields.
"""
from __future__ import annotations

import json
import unittest

from _harness import BaseTest
from pipeline import entityfix


class NeutralizeTest(unittest.TestCase):
    def test_offsets_preserved(self):
        text = 'a = 1 # c\nb = 2\n'
        out = entityfix._neutralize_comments(text)
        self.assertEqual(len(out), len(text))
        self.assertEqual(out[:6], "a = 1 ")

    def test_hash_in_string_kept(self):
        text = 'name = "a#b" # c\n'
        out = entityfix._neutralize_comments(text)
        self.assertIn("a#b", out.replace(" ", ""))
        self.assertNotIn("# c", out.replace(" ", "x"))

    def test_unterminated_comment_run(self):
        out = entityfix._neutralize_comments("# whole line\nx = 1")
        self.assertEqual(out.splitlines()[1], "x = 1")


class ParseEntitiesTest(unittest.TestCase):
    def test_basic_fields(self):
        # attach before the numeric cull_radius; a trailing identifier key
        # keeps the block parseable despite BUG-03 (numbers are not tokenized)
        text = ('entity = {\n name = "tank"\n pdxmesh = "tank_mesh"\n'
                ' attach = { gun = "muzzle" }\n cull_radius = 120\n'
                ' visible = yes\n}\n')
        recs = entityfix.parse_entities(text)
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["name"], "tank")
        self.assertEqual(r["pdxmesh"], "tank_mesh")
        self.assertTrue(r["has_cull"])
        self.assertEqual(r["attach"], ["muzzle"])

    def test_two_entities(self):
        text = ('entity = { name = "a" }\nentity = {\n name = "b"\n}\n')
        recs = entityfix.parse_entities(text)
        self.assertEqual([r["name"] for r in recs], ["a", "b"])

    def test_attach_name_key_excluded(self):
        text = 'entity = { name = "a" attach = { node = "x" name = "y" } }\n'
        recs = entityfix.parse_entities(text)
        self.assertEqual(recs[0]["attach"], ["x"])

    def test_unbalanced_block_does_not_crash(self):
        text = 'entity = {\n name = "a"\n'
        recs = entityfix.parse_entities(text)
        self.assertEqual(len(recs), 1)

    # --- BUG regressions (expected failures until fixed) -------------------
    def test_numeric_attach_nodes_are_parsed(self):
        text = ('entity = {\n name = "tank"\n'
                ' attach = { 0 = "node_lod0" 1 = "node_turret" }\n}\n')
        recs = entityfix.parse_entities(text)
        self.assertEqual(recs[0]["attach"], ["node_lod0", "node_turret"])
    def test_cull_radius_numeric_value_detected(self):
        text = 'entity = {\n name = "a"\n cull_radius = 50\n}\n'
        recs = entityfix.parse_entities(text)
        self.assertTrue(recs[0]["has_cull"],
                        "BUG-03: numeric value breaks key detection")
    def test_no_duplicate_cull_insertion_when_already_present(self):
        text = 'entity = {\n name = "amb"\n cull_radius = 50\n}\n'
        recs = entityfix.parse_entities(text)
        self.assertTrue(recs[0]["has_cull"])
        self.assertEqual(entityfix.defined_names(recs), {"amb"})
    def test_nested_block_does_not_leak_name(self):
        text = ('entity = {\n name = "tree"\n'
                ' sound = {\n  name = "treesound"\n }\n}\n')
        recs = entityfix.parse_entities(text)
        self.assertEqual(recs[0]["name"], "tree",
                         "BUG-05: nested sound block overwrote entity name")


class AmbientTest(BaseTest):
    def test_types_from_staging(self):
        self.ws.write("map/ambient_object.txt",
                      'ambient_object = {\n type = "tree_big"\n}\n'
                      'ambient_object = {\n type = "rock"\n}\n')
        names = entityfix.parse_ambient_entities(self.ws.modsrc, None)
        self.assertEqual(names, {"tree_big", "rock"})

    def test_vanilla_fallback(self):
        vanilla = self.ws.root / "vanilla"
        (vanilla / "map").mkdir(parents=True)
        (vanilla / "map" / "ambient_object.txt").write_text(
            'ambient_object = { type = "van_tree" }', encoding="utf-8")
        names = entityfix.parse_ambient_entities(self.ws.modsrc, vanilla)
        self.assertEqual(names, {"van_tree"})

    def test_staging_empty_falls_through_to_vanilla(self):
        self.ws.write("map/ambient_object.txt", '# nothing here\n')
        vanilla = self.ws.root / "vanilla"
        (vanilla / "map").mkdir(parents=True)
        (vanilla / "map" / "ambient_object.txt").write_text(
            'ambient_object = { type = "van_tree" }', encoding="utf-8")
        names = entityfix.parse_ambient_entities(self.ws.modsrc, vanilla)
        self.assertEqual(names, {"van_tree"})


class RunTest(BaseTest):
    def _tree(self, with_cull: bool = False):
        ws = self.ws
        cull1 = "\n cull_radius = 120" if with_cull else ""
        cull2 = "\n cull_radius = 200" if with_cull else ""
        ws.write("gfx/entities/ambient.asset",
                 'entity = {\n name = "amb_tree"\n pdxmesh = "tree_mesh"\n'
                 f' attach = {{ gun = "unit_tank" }}{cull1}\n}}\n'
                 'entity = {\n name = "unit_tank"\n pdxmesh = "tank_mesh"\n}\n')
        ws.write("gfx/entities/other.asset",
                 'entity = {\n name = "amb_rock"\n pdxmesh = "rock_mesh"\n'
                 f'{cull2}\n}}\n')
        ws.write("map/ambient_object.txt",
                 'ambient_object = {\n type = "amb_tree"\n}\n'
                 'ambient_object = {\n type = "amb_rock"\n}\n')

    def test_no_asset_files_returns_2(self):
        self.assertEqual(entityfix.run(self.ws.modsrc, self.ws.build,
                                       apply=False, vanilla=None), 2)

    def test_audit_counts_and_report(self):
        self._tree()
        rc = entityfix.run(self.ws.modsrc, self.ws.build, apply=False, vanilla=None)
        self.assertEqual(rc, 0)
        report = (self.ws.build / "entity_report.txt").read_text("utf-8")
        self.assertIn("entities: 3", report)
        self.assertIn("Attach to non-existent entities (staging+vanilla): 0",
                      report)
        mf = json.loads((self.ws.build / "entityfix_manifest.json").read_text("utf-8"))
        self.assertFalse(mf["applied"])
        self.assertEqual(mf["eligible"], 2)      # amb_tree + amb_rock

    def test_broken_attach_detected(self):
        ws = self.ws
        ws.write("gfx/entities/x.asset",
                 'entity = {\n name = "a"\n attach = { gun = "ghost_entity" }\n}\n')
        entityfix.run(ws.modsrc, ws.build, apply=False, vanilla=None)
        mf = json.loads((ws.build / "entityfix_manifest.json").read_text("utf-8"))
        self.assertEqual(mf["broken_attach"], ["ghost_entity"])

    def test_apply_inserts_cull_only_for_ambient(self):
        self._tree()
        rc = entityfix.run(self.ws.modsrc, self.ws.build, apply=True, vanilla=None,
                           cull_radius=100)
        self.assertEqual(rc, 0)
        text = (self.ws.modsrc / "gfx/entities/ambient.asset").read_text("utf-8")
        self.assertEqual(text.count("cull_radius = 100"), 1)  # amb_tree only
        self.assertIn('name = "unit_tank"', text)
        # unit entity untouched: no cull_radius line added to it
        tank_block = text.split('name = "unit_tank"')[0].split("entity = {")[-1]
        self.assertNotIn("cull_radius", tank_block)
        # inserted right after the opening line
        self.assertIn('entity = {\n\tcull_radius = 100\n', text)
        mf = json.loads((self.ws.build / "entityfix_manifest.json").read_text("utf-8"))
        self.assertEqual(sum(e["insertions"] for e in mf["edits"]), 2)

    def test_apply_idempotent(self):
        self._tree()
        entityfix.run(self.ws.modsrc, self.ws.build, apply=True, vanilla=None)
        rc = entityfix.run(self.ws.modsrc, self.ws.build, apply=True, vanilla=None)
        self.assertEqual(rc, 0)
        mf = json.loads((self.ws.build / "entityfix_manifest.json").read_text("utf-8"))
        self.assertEqual(mf["edits"], [])
    def test_existing_cull_respected(self):
        self._tree(with_cull=True)
        entityfix.run(self.ws.modsrc, self.ws.build, apply=True, vanilla=None)
        text = (self.ws.modsrc / "gfx/entities/ambient.asset").read_text("utf-8")
        self.assertNotIn("cull_radius = 100", text)
        self.assertIn("cull_radius = 120", text)
    def test_single_line_entity_insertion(self):
        ws = self.ws
        ws.write("gfx/entities/one.asset",
                 'entity = { name = "amb_lamp" pdxmesh = "lamp_mesh" }\n')
        ws.write("map/ambient_object.txt", 'ambient_object = { type = "amb_lamp" }\n')
        rc = entityfix.run(ws.modsrc, ws.build, apply=True, vanilla=None)
        self.assertEqual(rc, 0)
        text = (ws.modsrc / "gfx/entities/one.asset").read_text("utf-8")
        self.assertIn("cull_radius = 100", text)
        recs = entityfix.parse_entities(text)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["name"], "amb_lamp")

    def test_vanilla_entity_names_resolve_attach(self):
        ws = self.ws
        vanilla = ws.root / "vanilla"
        (vanilla / "gfx" / "entities").mkdir(parents=True)
        (vanilla / "gfx" / "entities" / "van.asset").write_text(
            'entity = { name = "van_base" }\n', encoding="utf-8")
        ws.write("gfx/entities/x.asset",
                 'entity = {\n name = "a"\n attach = { gun = "van_base" }\n}\n')
        entityfix.run(ws.modsrc, ws.build, apply=False, vanilla=vanilla)
        report = (ws.build / "entity_report.txt").read_text("utf-8")
        self.assertIn("non-existent entities (staging+vanilla): 0", report)


if __name__ == "__main__":
    unittest.main()
