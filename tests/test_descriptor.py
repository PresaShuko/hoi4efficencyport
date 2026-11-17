"""descriptor: merged-mod launcher descriptors (name, versions, tags).

Rules under test: the name comes from the staging folder, the versions are the
oldest of the sources, tags are the case-insensitive union of tags and
categories, the inner file has no path and the launcher file points at mod/.
"""
from __future__ import annotations

import json

from _harness import BaseTest, run_cli
from pipeline import descriptor


class ParseTest(BaseTest):
    def test_scalars_and_blocks(self):
        d = descriptor.parse_descriptor(
            'version="1.4"\n'
            'tags={\n\t"Alternative History"\n\t"Gameplay"\n}\n'
            'name="Two"\nsupported_version="1.12.14"\n'
            'path="C:/mods/two"\nremote_file_id="123"\n')
        self.assertEqual(d.name, "Two")
        self.assertEqual(d.version, "1.4")
        self.assertEqual(d.supported_version, "1.12.14")
        self.assertEqual(d.tags, ("Alternative History", "Gameplay"))
        self.assertEqual(d.path, "C:/mods/two")
        self.assertEqual(d.remote_file_id, "123")

    def test_categories_are_tags_and_duplicates_collapse(self):
        d = descriptor.parse_descriptor(
            'tags={ "Gameplay" "Map" }\ncategories={ "gameplay" "Graphics" }\n')
        self.assertEqual(d.tags, ("Gameplay", "Map", "Graphics"))

    def test_replace_path_repeats_block_and_normalization(self):
        d = descriptor.parse_descriptor(
            'name="X"\n'
            'replace_path="common/ai_strategy"\n'
            'replace_path="history/states"\n'
            'replace_path="Common\\Ai_Strategy\\"\n'
            'replace_path={ "common/decisions" "history/states" }\n')
        self.assertEqual(d.replace_paths,
                         ("common/ai_strategy", "history/states",
                          "common/decisions"))

    def test_missing_file_is_an_empty_descriptor(self):
        d = descriptor.parse_descriptor("")
        self.assertEqual((d.name, d.version, d.supported_version, d.tags),
                         ("", "", "", ()))


class VersionTest(BaseTest):
    def test_numeric_order_not_lexicographic(self):
        self.assertLess(descriptor.version_key("1.9"), descriptor.version_key("1.14"))
        self.assertLess(descriptor.version_key("1.14.*"), descriptor.version_key("1.15"))
        self.assertEqual(descriptor.version_key("1.19.4.2"), (1, 19, 4, 2))

    def test_oldest_keeps_the_original_spelling(self):
        self.assertEqual(descriptor.oldest(["1.19.4", "1.12.14", "1.15.1"]),
                         "1.12.14")
        self.assertEqual(descriptor.oldest(["1.19.*", "1.18.2"]), "1.18.2")
        self.assertEqual(descriptor.oldest(["", "  "]), "")

    def test_unparsable_version_sorts_oldest(self):
        self.assertEqual(descriptor.oldest(["1.15", "beta"]), "beta")


class ReplacePathTest(BaseTest):
    def test_union_is_case_insensitive_after_normalizing(self):
        a = descriptor.Descriptor(replace_paths=("common/ai_strategy",))
        b = descriptor.Descriptor(replace_paths=("Common/Ai_Strategy/",
                                                 "history/states"))
        self.assertEqual(descriptor.merge_replace_paths([a, b]),
                         ("common/ai_strategy", "history/states"))

    def test_empty_values_are_dropped(self):
        self.assertEqual(descriptor.dedupe_paths(["", "/", "  ", "a/b"]),
                         ("a/b",))


class TagsTest(BaseTest):
    def test_union_is_case_insensitive_and_ordered(self):
        a = descriptor.Descriptor(tags=("Alternative History", "Gameplay"))
        b = descriptor.Descriptor(tags=("gameplay", "Map"))
        self.assertEqual(descriptor.merge_tags([a, b]),
                         ("Alternative History", "Gameplay", "Map"))

    def test_empty(self):
        self.assertEqual(descriptor.merge_tags([]), ())


class RenderTest(BaseTest):
    def test_inner_file_syntax(self):
        text = descriptor.render("provatfrunito", version="1.4",
                                 supported_version="1.12.14",
                                 tags=("Alternative History", "Map"))
        self.assertEqual(text, (
            'version="1.4"\n'
            'tags={\n'
            '\t"Alternative History"\n'
            '\t"Map"\n'
            '}\n'
            'name="provatfrunito"\n'
            'supported_version="1.12.14"\n'))

    def test_replace_path_lines(self):
        text = descriptor.render("m", version="1", supported_version="1.15.*",
                                 replace_paths=("common/ai_strategy",
                                                "history/states"))
        self.assertEqual(text, (
            'version="1"\n'
            'name="m"\n'
            'supported_version="1.15.*"\n'
            'replace_path="common/ai_strategy"\n'
            'replace_path="history/states"\n'))

    def test_launcher_file_adds_the_path(self):
        text = descriptor.render("m", version="1", supported_version="1.15.*",
                                 path="mod/m")
        self.assertTrue(text.endswith('path="mod/m"\n'))

    def test_optional_fields_are_omitted(self):
        text = descriptor.render("m", version="1", supported_version="")
        self.assertNotIn("supported_version", text)
        self.assertNotIn("tags", text)
        self.assertNotIn("path", text)

    def test_quotes_cannot_break_the_field(self):
        text = descriptor.render('bad"name', version="1", supported_version="1")
        self.assertIn("name=\"bad'name\"", text)


class LauncherPathTest(BaseTest):
    def test_always_absolute_even_inside_a_mod_folder(self):
        staging = self.ws.root / "mod" / "provatfrunito"
        staging.mkdir(parents=True)
        self.assertEqual(descriptor.launcher_path(staging),
                         staging.resolve().as_posix())

    def test_elsewhere_is_absolute(self):
        staging = self.ws.root / "staging"
        staging.mkdir(parents=True)
        self.assertEqual(descriptor.launcher_path(staging),
                         staging.resolve().as_posix())


class WriteTest(BaseTest):
    def source(self, name: str, text: str):
        root = self.ws.root / name
        root.mkdir(parents=True, exist_ok=True)
        (root / "descriptor.mod").write_text(text, encoding="utf-8")
        return root

    def test_writes_both_files_with_the_folder_name_and_oldest_versions(self):
        a = self.source("one", 'version="2.1"\ntags={ "Gameplay" }\n'
                               'name="One"\nsupported_version="1.19.4"\n')
        b = self.source("two", 'version="1.4"\ntags={ "Map" }\n'
                               'name="Two"\nsupported_version="1.12.14"\n')
        staging = self.ws.root / "mod" / "provatfrunito"
        staging.mkdir(parents=True)
        (staging / "descriptor.mod").write_text('name="Two"\n', encoding="utf-8")
        info = descriptor.write_for_staging(staging, [a, b])
        self.assertTrue(info["written"])
        inner = descriptor.parse_descriptor(
            (staging / "descriptor.mod").read_text(encoding="utf-8"))
        outer = descriptor.parse_descriptor(
            (staging.parent / "provatfrunito.mod").read_text(encoding="utf-8"))
        # the copied source descriptor must be gone
        self.assertEqual(inner.name, "provatfrunito")
        self.assertEqual(outer.name, "provatfrunito")
        self.assertEqual(inner.version, "1.4")
        self.assertEqual(inner.supported_version, "1.12.14")
        self.assertEqual(inner.tags, ("Gameplay", "Map"))
        self.assertEqual(inner.path, "")
        self.assertEqual(outer.path, staging.resolve().as_posix())

    def test_replace_paths_are_unioned_into_both_files(self):
        a = self.source("one", 'name="One"\n'
                               'replace_path="common/ai_strategy"\n')
        b = self.source("two", 'name="Two"\n'
                               'replace_path="history/states"\n'
                               'replace_path="common/ai_strategy"\n')
        staging = self.ws.root / "mod" / "provatfrunito"
        staging.mkdir(parents=True)
        descriptor.write_for_staging(staging, [a, b])
        for path in (staging / "descriptor.mod",
                     staging.parent / "provatfrunito.mod"):
            parsed = descriptor.parse_descriptor(
                path.read_text(encoding="utf-8"))
            self.assertEqual(parsed.replace_paths,
                             ("common/ai_strategy", "history/states"), path.name)

    def test_thumbnail_is_referenced_when_present(self):
        a = self.source("one", 'name="One"\n')
        staging = self.ws.root / "stg"
        staging.mkdir()
        (staging / "thumbnail.png").write_bytes(b"x")
        descriptor.write_for_staging(staging, [a])
        self.assertIn('picture="thumbnail.png"',
                      (staging / "descriptor.mod").read_text(encoding="utf-8"))

    def test_disabled_writes_nothing(self):
        staging = self.ws.root / "stg"
        staging.mkdir()
        info = descriptor.write_for_staging(staging, [], enabled=False)
        self.assertFalse(info["written"])
        self.assertEqual(info["reason"], "disabled")
        self.assertFalse((staging / "descriptor.mod").exists())
        self.assertFalse((self.ws.root / "stg.mod").exists())

    def test_missing_staging_is_reported(self):
        info = descriptor.write_for_staging(self.ws.root / "nope", [])
        self.assertFalse(info["written"])
        self.assertEqual(info["reason"], "staging missing")

    def test_no_sources_still_writes_a_valid_file(self):
        staging = self.ws.root / "stg"
        staging.mkdir()
        descriptor.write_for_staging(staging, [])
        text = (staging / "descriptor.mod").read_text(encoding="utf-8")
        self.assertIn('name="stg"', text)
        self.assertIn('version="1.0"', text)
        self.assertNotIn("supported_version", text)


class SyncIntegrationTest(BaseTest):
    def _mod(self, name: str, text: str):
        root = self.ws.root / name
        root.mkdir(parents=True, exist_ok=True)
        (root / "descriptor.mod").write_text(text, encoding="utf-8")
        (root / "shared.txt").write_text(name, encoding="utf-8")
        return root

    def test_sync_generates_and_reports_the_descriptor(self):
        a = self._mod("one", 'tags={ "Gameplay" }\nname="One"\n'
                              'supported_version="1.19.4"\n'
                              'replace_path="common/ai_strategy"\n')
        b = self._mod("two", 'tags={ "Map" "Gameplay" }\nname="Two"\n'
                              'supported_version="1.12.14"\n'
                              'replace_path="history/states"\n')
        staging = self.ws.root / "mod" / "provatfrunito"
        rc, out, err = run_cli("sync", "--mod", str(a), "--mod", str(b),
                               "--staging", str(staging),
                               "--out", str(self.ws.build))
        self.assertEqual(rc, 0, err)
        self.assertIn("Descriptor:", out)
        self.assertTrue((staging / "descriptor.mod").is_file())
        self.assertTrue((staging.parent / "provatfrunito.mod").is_file())
        parsed = descriptor.parse_descriptor(
            (staging / "descriptor.mod").read_text(encoding="utf-8"))
        self.assertEqual(parsed.name, "provatfrunito")
        self.assertEqual(parsed.supported_version, "1.12.14")
        self.assertEqual(parsed.tags, ("Gameplay", "Map"))
        self.assertEqual(parsed.replace_paths,
                         ("common/ai_strategy", "history/states"))

    def test_no_descriptor_keeps_the_copied_one(self):
        a = self._mod("one", 'name="One"\nsupported_version="1.19.4"\n')
        staging = self.ws.root / "stg"
        rc, out, err = run_cli("sync", "--mod", str(a), "--staging", str(staging),
                               "--no-descriptor")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Descriptor:", out)
        parsed = descriptor.parse_descriptor(
            (staging / "descriptor.mod").read_text(encoding="utf-8"))
        self.assertEqual(parsed.name, "One", "the source descriptor stays")
        self.assertFalse((self.ws.root / "stg.mod").exists())


class GuiOptionTest(BaseTest):
    def test_gui_flag_and_argv(self):
        import unittest

        try:
            from pipeline import gui
        except ImportError as exc:  # tkinter not available
            raise unittest.SkipTest(str(exc)) from exc
        on = gui.Opts(mods=("D:/a",), staging="s", out="o")
        off = gui.Opts(mods=("D:/a",), staging="s", out="o", descriptor=False)
        self.assertNotIn("--no-descriptor",
                         gui.stage_command("sync", on, prefix=["P"]))
        self.assertIn("--no-descriptor",
                      gui.stage_command("sync", off, prefix=["P"]))

    def test_settings_roundtrip(self):
        import unittest

        try:
            from pipeline import gui
        except ImportError as exc:  # tkinter not available
            raise unittest.SkipTest(str(exc)) from exc
        target = self.ws.root / "gui.json"
        gui.Settings(descriptor=False).save(target)
        self.assertFalse(json.loads(target.read_text(encoding="utf-8"))["descriptor"])
        self.assertFalse(gui.Settings.load(target).descriptor)
