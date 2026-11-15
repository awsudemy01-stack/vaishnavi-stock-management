"""Guards the project metadata and architecture rules declared in pyproject.toml.

Stdlib only (tomllib, pathlib, unittest) so it runs with the bare system Python:

    python -m unittest discover -s tests/core -t .
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
SRC_PKG = REPO_ROOT / "src" / "vaishnavi"

# Modules at the package root that are wiring, not a layer.
UNLAYERED_ROOT_MODULES = {"__init__", "__main__", "app_services"}

# Packages that are allowed to know which OS they run on (docs/03 §2, ADR-027).
OS_AWARE_GLOBS = {"main.py", "bootstrap/**", "src/vaishnavi/platform/**"}

VERSION_BOUND = re.compile(r"(>=|==|~=|<|>|!=)")


def _load() -> dict[str, Any]:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _dep_name(requirement: str) -> str:
    return re.split(r"[\s;<>=!~\[]", requirement, maxsplit=1)[0].lower()


class ProjectMetadataTest(unittest.TestCase):
    """pyproject.toml is well-formed and complete for the installer."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.data = _load()
        cls.project = cls.data["project"]

    def test_requires_python_matches_policy(self) -> None:
        # ADR-012: accept 3.11–3.13; newer interpreters are dry-run only by the installer.
        self.assertEqual(self.project["requires-python"], ">=3.11,<3.14")

    def test_every_dependency_has_a_version_bound(self) -> None:
        reqs = list(self.project["dependencies"])
        for extra in self.project["optional-dependencies"].values():
            reqs.extend(extra)
        unbounded = [r for r in reqs if not VERSION_BOUND.search(r.split(";")[0])
                     and not r.startswith("types-")]
        self.assertEqual(unbounded, [], "dependencies without a version bound")

    def test_os_specific_dependencies_carry_markers(self) -> None:
        deps = {_dep_name(r): r for r in self.project["dependencies"]}
        self.assertIn("sys_platform == 'win32'", deps["pywin32"])
        self.assertIn("sys_platform == 'linux'", deps["pyudev"])

    def test_console_script_points_at_package_main(self) -> None:
        self.assertEqual(self.project["scripts"]["vaishnavi"], "vaishnavi.__main__:main")

    def test_apscheduler_is_capped_below_v4(self) -> None:
        # 4.x is an incompatible rewrite; the scheduler module targets the 3.x API.
        deps = {_dep_name(r): r for r in self.project["dependencies"]}
        self.assertIn("<4", deps["apscheduler"])

    def test_package_data_includes_shipped_assets(self) -> None:
        data = self.data["tool"]["setuptools"]["package-data"]["vaishnavi"]
        for pattern in ("py.typed", "core/messages/*.toml", "ai/prompts/*.md", "ui/i18n/*.json",
                        "db/alembic/versions/*.py"):
            self.assertIn(pattern, data)
        self.assertTrue((SRC_PKG / "py.typed").exists())


class ArchitectureRulesTest(unittest.TestCase):
    """The import-linter and ruff configuration encode docs/03 §2 and ADR-040."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.data = _load()
        cls.contracts = {c["name"]: c for c in cls.data["tool"]["importlinter"]["contracts"]}
        cls.layers_contract = next(c for c in cls.contracts.values() if c["type"] == "layers")

    def _layer_packages(self) -> set[str]:
        packages: set[str] = set()
        for layer in self.layers_contract["layers"]:
            for part in layer.split("|"):
                packages.add(part.strip().removeprefix("vaishnavi."))
        return packages

    def test_every_top_level_package_is_assigned_to_a_layer(self) -> None:
        on_disk = {p.name for p in SRC_PKG.iterdir() if p.is_dir() and not p.name.startswith("__")}
        missing = on_disk - self._layer_packages()
        self.assertEqual(missing, set(), "packages not covered by the layers contract")

    def test_root_modules_are_only_wiring(self) -> None:
        root_modules = {p.stem for p in SRC_PKG.glob("*.py")}
        self.assertEqual(root_modules, UNLAYERED_ROOT_MODULES)

    def test_core_is_the_bottom_layer(self) -> None:
        self.assertEqual(self.layers_contract["layers"][-1], "vaishnavi.core")

    def test_stdlib_only_tier_modules_exist(self) -> None:
        contract = next(c for n, c in self.contracts.items() if n.startswith("Stdlib-only"))
        for module in contract["source_modules"]:
            rel = Path(*module.removeprefix("vaishnavi.").split("."))
            exists = (SRC_PKG / rel).with_suffix(".py").exists() or (SRC_PKG / rel / "__init__.py").exists()
            self.assertTrue(exists, f"{module} listed in the stdlib-only contract but not on disk")

    def test_stdlib_only_tier_forbids_the_heavy_packages(self) -> None:
        contract = next(c for n, c in self.contracts.items() if n.startswith("Stdlib-only"))
        for pkg in ("nicegui", "sqlalchemy", "pydantic", "numpy", "openai", "ollama"):
            self.assertIn(pkg, contract["forbidden_modules"])
        self.assertTrue(self.data["tool"]["importlinter"]["include_external_packages"])

    def test_sys_platform_is_banned_except_in_os_aware_packages(self) -> None:
        lint = self.data["tool"]["ruff"]["lint"]
        self.assertIn("sys.platform", lint["flake8-tidy-imports"]["banned-api"])
        exempt = {glob for glob, codes in lint["per-file-ignores"].items() if "TID251" in codes}
        self.assertEqual(exempt, OS_AWARE_GLOBS)

    def test_print_is_banned_in_the_application(self) -> None:
        lint = self.data["tool"]["ruff"]["lint"]
        self.assertIn("T20", lint["select"])
        allowed_print = {glob for glob, codes in lint["per-file-ignores"].items() if "T20" in codes}
        self.assertEqual(allowed_print, {"main.py", "bootstrap/**"})


if __name__ == "__main__":
    unittest.main()
