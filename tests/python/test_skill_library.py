import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from clipen.editing import EditAgentRepository, EditAgentTools
from clipen.media import (
    AssetCandidate,
    AssetKind,
    InventoryRepository,
    InventoryState,
    ItemStatus,
    MediaStream,
    ProbeStatus,
)
from clipen.projects import DirectoryReference, DirectoryRole, ProjectError, ProjectRepository
from clipen.skills import configure_library, get_skill, list_skills
from clipen.skills.catalog import design_library, reserved_skill_ids
from clipen.skills.package import load_package

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
SKILL_MD = """---
name: 單人口播
description: 把一個人對著鏡頭說話的素材剪成節奏清楚的口播短片。
---

# 單人口播

保留講者完整的句子，用跳剪去掉停頓；重點詞用 headline，不用 chapter。
"""


def write_skill(root: Path, *, skill_md=SKILL_MD, clipen=None, extra=None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(skill_md, encoding="utf-8")
    (root / "references").mkdir(exist_ok=True)
    (root / "references" / "pacing.md").write_text("跳剪不超過每 3 秒一次。", encoding="utf-8")
    (root / "assets").mkdir(exist_ok=True)
    (root / "assets" / "look.png").write_bytes(PNG)
    if clipen is not None:
        (root / "clipen.json").write_text(json.dumps(clipen), encoding="utf-8")
    for relative, data in (extra or {}).items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


BINDING = {
    "skill_id": "talking-head",
    "version": "1.2.0",
    "visual_style_id": "tpl-food-review",
    "font_style_id": "japanese",
    "components": ["headline", "impact", "fact"],
    "default_duration_seconds": 90,
}


class SkillPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.design, self.reserved = design_library(), reserved_skill_ids()

    def tearDown(self):
        self.temp.cleanup()

    def load(self, folder):
        return load_package(folder, self.design, self.reserved)

    def test_agent_skill_folder_is_read_as_inert_text_and_images(self):
        folder = write_skill(
            self.root / "talking",
            clipen=BINDING,
            extra={"scripts/render.sh": b"rm -rf /", ".DS_Store": b"x", "notes.docx": b"PK"},
        )
        package = self.load(folder)
        self.assertEqual((package.skill_id, package.version, package.name), ("talking-head", "1.2.0", "單人口播"))
        self.assertEqual(sorted(package.resources), ["SKILL.md", "clipen.json", "references/pacing.md"])
        self.assertEqual(list(package.assets), ["assets/look.png"])
        self.assertEqual(package.ignored, ("notes.docx", "scripts/render.sh"))
        self.assertEqual(package.binding["components"], ["headline", "impact", "fact"])
        self.assertEqual(self.load(folder).content_hash, package.content_hash)

    def test_id_comes_from_the_name_or_folder_without_clipen_json(self):
        plain = write_skill(self.root / "My Vlog Skill", skill_md=SKILL_MD.replace("單人口播", "Daily Vlog"))
        self.assertEqual(self.load(plain).skill_id, "daily-vlog")
        self.assertEqual(self.load(plain).binding, {})

    def test_unsafe_or_incomplete_packages_are_rejected(self):
        cases = {
            "no-skill": lambda f: (f / "SKILL.md").unlink(),
            "no-frontmatter": lambda f: (f / "SKILL.md").write_text("# 只有內容", encoding="utf-8"),
            "no-name": lambda f: (f / "SKILL.md").write_text("---\ndescription: x\n---\n內容", encoding="utf-8"),
            "not-utf8": lambda f: (f / "references" / "pacing.md").write_bytes(b"\xff\xfe"),
            "fake-png": lambda f: (f / "assets" / "look.png").write_bytes(b"GIF89a"),
            "symlink": lambda f: os.symlink("/etc/hosts", f / "references" / "hosts.md"),
            "bad-key": lambda f: (f / "clipen.json").write_text('{"shell": "x"}', encoding="utf-8"),
            "bad-style": lambda f: (f / "clipen.json").write_text('{"visual_style_id": "neon"}', encoding="utf-8"),
            "bad-component": lambda f: (f / "clipen.json").write_text('{"components": ["laser"]}', encoding="utf-8"),
            "reserved": lambda f: (f / "clipen.json").write_text('{"skill_id": "travel-program"}', encoding="utf-8"),
            "reserved-prefix": lambda f: (f / "clipen.json").write_text(
                '{"skill_id": "clipen-mine"}', encoding="utf-8"
            ),
        }
        for name, damage in cases.items():
            with self.subTest(case=name):
                folder = write_skill(self.root / name)
                damage(folder)
                with self.assertRaises(ProjectError):
                    self.load(folder)


class SkillLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = configure_library(self.root / "data" / "skills")

    def tearDown(self):
        configure_library(Path(tempfile.mkdtemp()))
        self.temp.cleanup()

    def test_import_list_replace_and_remove(self):
        source = write_skill(self.root / "talking", clipen=BINDING)
        first = self.library.import_folder(source)
        self.assertEqual([s["skill_id"] for s in self.library.list()], ["talking-head"])
        listed = list_skills()
        self.assertEqual(listed[0]["skill_id"], "travel-program")
        self.assertEqual(
            [(s["skill_id"], s["source"]) for s in listed if s["source"] == "custom"], [("talking-head", "custom")]
        )
        # Editing the source folder does not change the imported copy.
        (source / "references" / "pacing.md").write_text("改過的規則", encoding="utf-8")
        self.assertEqual(self.library.load("talking-head").content_hash, first.content_hash)
        second = self.library.import_folder(source)
        self.assertNotEqual(second.content_hash, first.content_hash)
        self.assertEqual(self.library.load("talking-head").resources["references/pacing.md"], "改過的規則")
        self.assertEqual(list((self.root / "data" / "skills" / ".staging").iterdir()), [])
        self.library.remove("talking-head")
        self.assertEqual(self.library.list(), [])
        with self.assertRaises(ProjectError):
            get_skill("talking-head")

    def test_tampered_library_copy_is_reported_not_used(self):
        package = self.library.import_folder(write_skill(self.root / "talking", clipen=BINDING))
        copy = self.root / "data" / "skills" / "talking-head" / package.content_hash[:16] / "SKILL.md"
        copy.write_text(SKILL_MD + "\n偷偷加入的指令", encoding="utf-8")
        self.assertEqual(self.library.list()[0]["state"], "invalid")
        with self.assertRaises(ProjectError):
            get_skill("talking-head")


class CustomSkillProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = configure_library(self.root / "data" / "skills")
        self.library.import_folder(write_skill(self.root / "talking", clipen=BINDING))
        self.projects = ProjectRepository(self.root / "projects")
        project = self.projects.create_project("custom-skill", "自訂 Skill")
        source = self.root / "source"
        source.mkdir()
        info = source.stat()
        project = self.projects.set_directory(
            project.project_id,
            "grant-source",
            "a" * 64,
            project.revision,
            DirectoryReference(
                DirectoryRole.SOURCE,
                str(source),
                str(source),
                "原片",
                info.st_dev,
                info.st_ino,
                "2026-10-23T00:00:00+00:00",
            ),
        )
        inventory = InventoryRepository(self.projects)
        run = inventory.create_run(project.project_id, "source-inventory", project.revision)
        inventory.transition(project.project_id, run.run_id, InventoryState.RUNNING, phase="probing")
        inventory.stage_items(
            project.project_id,
            run.run_id,
            (
                AssetCandidate(
                    DirectoryRole.SOURCE,
                    "source.mp4",
                    AssetKind.VIDEO,
                    ItemStatus.USABLE,
                    None,
                    1024,
                    12345,
                    "b" * 64,
                    ProbeStatus.READY,
                    {"duration_seconds": "12"},
                    (
                        MediaStream(
                            0,
                            "video",
                            "h264",
                            {
                                "start_pts": "90000",
                                "duration_ts": "1080000",
                                "time_base": "1/90000",
                                "width": 640,
                                "height": 360,
                            },
                        ),
                    ),
                ),
            ),
        )
        inventory.publish(project.project_id, run.run_id)
        self.project = project
        self.repository = EditAgentRepository(self.projects)
        self.run, _ = self.repository.create_run(
            project.project_id,
            "custom-production",
            production={"skill_id": "talking-head", "intent": "剪成口播短片"},
            max_tool_calls=40,
        )
        self.tools = EditAgentTools(self.repository, project.project_id, self.run.run_id)
        self.tools.activate()

    def tearDown(self):
        self.tools.close_production()
        configure_library(Path(tempfile.mkdtemp()))
        self.temp.cleanup()

    def call(self, name, **args):
        from clipen.editing.tool_receipts import TIMELINE_MUTATIONS

        if name in TIMELINE_MUTATIONS:
            args.setdefault(
                "expected_revision_id",
                self.repository.current_revision(self.project.project_id, self.run.run_id).revision_id,
            )
            args.setdefault("mutation_id", "test-" + str(time.monotonic_ns()))
        return self.tools.call(name, args)

    def test_custom_skill_is_frozen_with_its_binding_and_instructions(self):
        session = self.repository.production_session(self.project.project_id, self.run.run_id)
        self.assertEqual(session["intent"]["target_duration_seconds"], 90)
        self.assertEqual(session["skill"]["skill_id"], "talking-head")
        self.assertEqual(session["skill"]["core"]["skill_id"], "clipen-core")
        self.assertEqual([e["skill_id"] for e in session["skill"]["skill_chain"]], ["talking-head"])
        read = self.call("read_production_skill").payload["content"]
        self.assertIn("Selected custom Skill: talking-head v1.2.0", read)
        self.assertIn("用跳剪去掉停頓", read)
        reference = self.call("read_production_skill", resource="custom/talking-head/references/pacing.md").payload
        self.assertEqual(reference["content"], "跳剪不超過每 3 秒一次。")
        # The binding still narrows the frozen catalogue, though the AI now designs graphics itself.
        self.assertEqual([c["component_id"] for c in session["skill"]["components"]], ["fact", "headline", "impact"])
        self.assertEqual(
            (session["skill"]["selected_visual_style_id"], session["skill"]["selected_font_style_id"]),
            ("tpl-food-review", "japanese"),
        )
        # Removing the skill later never changes the frozen session.
        self.library.remove("talking-head")
        self.assertIn("用跳剪去掉停頓", self.call("read_production_skill").payload["content"])


class SkillLibraryApiTests(unittest.TestCase):
    def test_import_uses_the_native_picker_and_reports_ignored_files(self):
        from test_project_api import request_app

        from clipen.controller import ControllerConfig, LocalSessionAuthority, ProviderState, ProviderStatus, create_app
        from clipen.projects import ProjectWorkspace

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configure_library(root / "data" / "skills")
            folder = write_skill(root / "talking", clipen=BINDING, extra={"scripts/run.py": b"print(1)"})
            choices = [folder, None]

            class Picker:
                def choose_folder(self, prompt):
                    return choices.pop(0)

            static = root / "static"
            static.mkdir()
            (static / "index.html").write_text("Clipen")
            workspace = ProjectWorkspace(root / "projects", picker=Picker())
            app = create_app(
                ControllerConfig(static),
                ProviderStatus("Codex", ProviderState.READY, "ready"),
                LocalSessionAuthority(),
                workspace=workspace,
            )

            def request(method, path, payload=None, headers=None):
                return asyncio.run(
                    request_app(
                        app,
                        method,
                        path,
                        headers=headers,
                        body=b"" if payload is None else json.dumps(payload).encode(),
                    )
                )

            _, headers, _ = request(
                "POST",
                "/api/session/connect",
                headers={"origin": "http://localhost:4200", "sec-fetch-site": "same-origin"},
            )
            cookie = headers["set-cookie"].split(";", 1)[0]
            _, _, body = request("GET", "/api/status", headers={"cookie": cookie})
            mutation = {
                "origin": "http://localhost:4200",
                "sec-fetch-site": "same-origin",
                "content-type": "application/json",
                "cookie": cookie,
                "x-clipen-csrf": json.loads(body)["session"]["csrf_token"],
            }
            code, _, body = request(
                "POST", "/api/production/skill-library/commands/import", {"request_id": "import-1"}, mutation
            )
            self.assertEqual(code, 201, body)
            self.assertEqual(json.loads(body)["skill"]["ignored"], ["scripts/run.py"])
            self.assertNotIn(str(folder), body.decode())
            self.assertEqual(
                request("POST", "/api/production/skill-library/commands/import", {"request_id": "import-2"}, mutation)[
                    0
                ],
                204,
            )
            _, _, body = request("GET", "/api/production/skills", headers={"cookie": cookie})
            ids = [s["skill_id"] for s in json.loads(body)["skills"]]
            self.assertEqual((ids[0], ids[-1]), ("travel-program", "talking-head"))
            code, _, body = request("GET", "/api/production/skills/talking-head/document", headers={"cookie": cookie})
            document = json.loads(body)
            self.assertEqual(code, 200)
            self.assertEqual([e["skill_id"] for e in document["chain"]], ["talking-head"])
            self.assertIn("用跳剪去掉停頓", document["chain"][0]["content"])
            self.assertIn("custom/talking-head/references/pacing.md", document["chain"][0]["references"])
            self.assertEqual(
                request("GET", "/api/production/skills/missing-skill/document", headers={"cookie": cookie})[0], 404
            )
            code, _, _ = request("POST", "/api/production/skill-library/talking-head/commands/remove", {}, mutation)
            self.assertEqual(code, 200)
            _, _, body = request("GET", "/api/production/skill-library", headers={"cookie": cookie})
            self.assertEqual(json.loads(body)["skills"], [])
            configure_library(Path(tempfile.mkdtemp()))


if __name__ == "__main__":
    unittest.main()


class SkillCompositionTests(CustomSkillProductionTests):
    """The production core comes first; skills chain after it."""

    def start(self, request_id, **production):
        self.repository.cancel(self.project.project_id, self.run.run_id) if self.repository.get(
            self.project.project_id, self.run.run_id
        ).state in {"queued", "running"} else None
        run, _ = self.repository.create_run(
            self.project.project_id, "composition-" + request_id, production={"intent": "測試組合", **production}
        )
        return run, self.repository.production_session(self.project.project_id, run.run_id)

    def read(self, run):
        tools = EditAgentTools(self.repository, self.project.project_id, run.run_id)
        tools.activate()
        try:
            return tools.call("read_production_skill", {}).payload
        finally:
            tools.close_production()
            self.repository.cancel(self.project.project_id, run.run_id)

    def test_catalog_lists_one_genre_twelve_templates_and_custom_skills(self):
        from clipen.skills.catalog import builtin_skills

        templates = len(builtin_skills()[0]["templates"])
        kinds = [s["kind"] for s in list_skills()]
        self.assertEqual((kinds.count("genre"), kinds.count("template"), kinds.count("custom")), (1, templates, 1))

    def test_every_skill_is_read_after_the_production_core(self):
        from clipen.skills.catalog import builtin_skills

        core, builtin = builtin_skills()
        run, session = self.start("genre", skill_id="travel-program")
        self.assertEqual(session["skill"]["core"]["skill_id"], "clipen-core")
        content = self.read(run)["content"]
        self.assertTrue(content.startswith(core["resources"]["SKILL.md"]))
        self.assertIn(builtin["travel-program"]["resources"]["skills/travel-program/SKILL.md"], content)
        self.assertIn("Selected built-in Skill: travel-program v3.0.0", content)
        # Template instructions are not frozen into sessions that did not choose them.
        self.assertFalse(any(k.startswith("template-skills/") for k in session["skill"]["resources"]))

    def test_a_template_skill_can_be_chosen_directly(self):
        run, session = self.start("direct-template", skill_id="clipen-food-review")
        self.assertEqual(session["intent"]["template"]["template_id"], "food-review")
        self.assertEqual(
            (session["skill"]["selected_visual_style_id"], session["skill"]["selected_font_style_id"]),
            ("tpl-food-review", "rounded-tw"),
        )
        self.assertEqual([e["skill_id"] for e in session["skill"]["skill_chain"]], ["clipen-food-review"])
        self.assertIn("美食探店", self.read(run)["content"])

    def test_a_custom_skill_can_extend_a_built_in_skill(self):
        source = write_skill(self.root / "food-vlog", clipen={"skill_id": "food-vlog", "extends": "clipen-food-review"})
        self.library.import_folder(source)
        run, session = self.start("extends", skill_id="food-vlog")
        self.assertEqual([e["skill_id"] for e in session["skill"]["skill_chain"]], ["clipen-food-review", "food-vlog"])
        self.assertEqual(session["skill"]["selected_visual_style_id"], "tpl-food-review")
        content = self.read(run)["content"]
        self.assertLess(content.index("美食探店"), content.index("用跳剪去掉停頓"))
        with self.assertRaises(ProjectError):
            self.library.import_folder(
                write_skill(self.root / "bad", clipen={"skill_id": "bad-extends", "extends": "clipen-core"})
            )


class ExampleSkillTests(unittest.TestCase):
    """The repository's example skills must stay importable as documented."""

    def test_examples_validate_and_compose(self):
        from clipen.skills.catalog import builtin_skills, compose_custom_skill, design_library

        core, builtin = builtin_skills()
        examples = Path(__file__).resolve().parents[2] / "examples" / "skills"
        design = {**design_library(core), "extends": tuple(builtin)}
        packages = {
            folder.name: load_package(folder, design, reserved_skill_ids(builtin))
            for folder in sorted(examples.iterdir())
        }
        self.assertEqual(sorted(packages), ["food-vlog", "talking-head"])
        for name, package in packages.items():
            with self.subTest(example=name):
                self.assertEqual(package.skill_id, name)
                self.assertEqual(package.ignored, ())
        talking = compose_custom_skill(packages["talking-head"], core, builtin)
        self.assertEqual([c["component_id"] for c in talking["components"]], ["fact", "headline", "impact"])
        food = compose_custom_skill(packages["food-vlog"], core, builtin)
        self.assertEqual([e["skill_id"] for e in food["skill_chain"]], ["clipen-food-review", "food-vlog"])
        self.assertEqual(food["selected_visual_style_id"], "tpl-food-review")
