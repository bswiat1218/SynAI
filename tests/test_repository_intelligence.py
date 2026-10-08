from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from synai.config import ConversationEnvironment, Settings
from synai.intelligence import IndexLimits, RepositoryIndex
from synai.models import Session
from synai.tools import INTELLIGENCE_TOOLS, Tools, schemas


class RepositoryIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "pkg").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "pkg" / "__init__.py").write_text("from .core import Base as Alias\n", encoding="utf-8")
        (self.root / "pkg" / "core.py").write_text(
            "class Base:\n"
            "    def method(self):\n"
            "        return helper()\n"
            "\n"
            "class Child(Base):\n"
            "    async def run(self):\n"
            "        return await async_helper()\n"
            "\n"
            "def helper():\n"
            "    return 1\n"
            "\n"
            "async def async_helper():\n"
            "    return 2\n",
            encoding="utf-8",
        )
        (self.root / "pkg" / "consumer.py").write_text(
            "from .core import helper as imported_helper\n"
            "def caller():\n"
            "    return imported_helper()\n"
            "def dynamic(obj):\n"
            "    return obj.helper()\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "test_core.py").write_text(
            "class TestCore:\n"
            "    def test_helper(self):\n"
            "        pass\n"
            "async def test_async():\n"
            "    pass\n",
            encoding="utf-8",
        )
        (self.root / "pkg" / "core_test.py").write_text("def test_core():\n    pass\n", encoding="utf-8")
        (self.root / "README.md").write_text("helper documentation\n", encoding="utf-8")
        self.index = RepositoryIndex(self.root)

    def query(self, operation: str, arguments: dict[str, str] | None = None) -> dict:
        return self.index.query(operation, arguments or {})

    def test_indexes_modules_classes_methods_functions_async_and_relationships(self) -> None:
        symbols = self.query("find_symbol", {"name": "pkg.core.Child"})["results"]
        self.assertEqual(len(symbols), 1)
        self.assertEqual(symbols[0]["kind"], "class")
        self.assertEqual(symbols[0]["path"], "pkg/core.py")
        found = self.query("find_symbol", {"name": "async_helper"})["results"]
        self.assertEqual(found[0]["kind"], "async_function")
        methods = self.query("find_symbol", {"name": "run"})["results"]
        self.assertEqual(methods[0]["kind"], "async_method")
        classes = self.query("find_symbol", {"name": "Base"})["results"]
        self.assertEqual(classes[0]["qualified_name"], "pkg.core.Base")
        modules = self.query("find_symbol", {"name": "pkg.core"})["results"]
        self.assertEqual(modules[0]["kind"], "module")
        implementation = self.query("find_implementations", {"name": "pkg.core.Base"})
        self.assertEqual([row["qualified_name"] for row in implementation["results"]], ["pkg.core.Child"])
        self.assertEqual(implementation["results"][0]["relationship"], "direct_subclass")

    def test_definitions_references_callers_and_imports_report_static_limits(self) -> None:
        definitions = self.query("find_definition", {"name": "helper"})["results"]
        self.assertEqual(len(definitions), 1)
        references = self.query("find_references", {"name": "helper"})
        self.assertTrue(any(row["path"] == "pkg/core.py" for row in references["results"]))
        self.assertIn("lexical AST", references["limitations"][0])
        callers = self.query("find_callers", {"name": "helper"})
        self.assertIn("pkg.core.Base.method", {row["scope"] for row in callers["results"]})
        self.assertTrue(all(row["resolution"] == "syntactic_name_match" for row in callers["results"]))
        imports = self.query("find_imports", {"query": "Alias"})["results"]
        self.assertEqual(imports[0]["module"], "pkg.core")
        self.assertEqual(imports[0]["name"], "Base")
        self.assertEqual(imports[0]["bound_name"], "Alias")
        self.assertEqual(len(self.query("find_imports", {"query": "pkg.core"})["results"]), 2)

    def test_duplicate_names_are_not_collapsed_and_ambiguous_bases_are_omitted(self) -> None:
        (self.root / "pkg" / "other.py").write_text(
            "class Base:\n    pass\nclass Derived(Base):\n    pass\n",
            encoding="utf-8",
        )
        found = self.query("find_symbol", {"name": "Base"})["results"]
        self.assertEqual(len(found), 2)
        self.assertEqual(self.query("find_implementations", {"name": "Base"})["results"], [])

    def test_test_discovery_uses_paths_and_test_names_without_coverage_claims(self) -> None:
        tests = self.query("find_tests")
        paths = {row["path"] for row in tests["results"]}
        self.assertIn("tests/test_core.py", paths)
        self.assertIn("pkg/core_test.py", paths)
        self.assertTrue(any(row["qualified_name"].endswith(".TestCore") for row in tests["results"]))
        self.assertTrue(any(row["name"] == "test_async" for row in tests["results"]))
        self.assertFalse(any(row["name"] == "dynamic" for row in tests["results"]))
        self.assertIn("does not establish code coverage", tests["limitations"][0])

    def test_search_is_literal_bounded_and_ignores_binary_and_excluded_paths(self) -> None:
        self.index = RepositoryIndex(self.root, IndexLimits(max_search_results=2, max_text_per_result=12))
        (self.root / "binary.py").write_bytes(b"helper\x00ignored")
        excluded = self.root / ".venv"
        excluded.mkdir()
        (excluded / "excluded.py").write_text("helper\n", encoding="utf-8")
        result = self.query("search_code", {"query": "helper"})
        self.assertLessEqual(len(result["results"]), 2)
        self.assertTrue(result["truncated"])
        self.assertTrue(all(len(row["snippet"]) <= 12 for row in result["results"]))
        self.assertTrue(all(".venv" not in row["path"] and row["path"] != "binary.py" for row in result["results"]))

    def test_syntax_errors_empty_and_unicode_sources_are_indexed(self) -> None:
        (self.root / "broken.py").write_text("def bad(:\n", encoding="utf-8")
        (self.root / "empty.py").write_text("", encoding="utf-8")
        (self.root / "unicode.py").write_text("# café\nclass Éclair:\n    pass\n", encoding="utf-8")
        diagnostics = self.query("get_diagnostics")
        syntax = [row for row in diagnostics["results"] if row["kind"] == "python_syntax_error"]
        self.assertEqual(syntax[0]["path"], "broken.py")
        self.assertEqual(self.query("find_symbol", {"name": "empty"})["results"][0]["kind"], "module")
        self.assertEqual(self.query("find_symbol", {"name": "Éclair"})["results"][0]["path"], "unicode.py")

    def test_changed_and_deleted_files_invalidate_incremental_index(self) -> None:
        self.assertEqual(len(self.query("find_symbol", {"name": "helper"})["results"]), 1)
        (self.root / "pkg" / "core.py").write_text("def replacement():\n    pass\n", encoding="utf-8")
        self.assertEqual(self.query("find_symbol", {"name": "helper"})["results"], [])
        self.assertEqual(len(self.query("find_symbol", {"name": "replacement"})["results"]), 1)
        (self.root / "pkg" / "core.py").unlink()
        self.assertEqual(self.query("find_symbol", {"name": "replacement"})["results"], [])

    def test_unchanged_content_is_not_reparsed_and_results_are_deterministic(self) -> None:
        first = self.query("get_project_structure")
        second = self.query("get_project_structure")
        self.assertEqual(first, second)
        with unittest.mock.patch.object(self.index, "_parse_file", wraps=self.index._parse_file) as parser:
            self.query("find_symbol", {"name": "helper"})
        parser.assert_not_called()

    def test_project_structure_and_resource_limit_diagnostics(self) -> None:
        structure = self.query("get_project_structure")
        self.assertTrue(any(row["path"] == "pkg" and row["kind"] == "directory" for row in structure["results"]))
        self.assertTrue(any(row["path"] == "README.md" and row["language"] == "markdown" for row in structure["results"]))
        limited = RepositoryIndex(self.root, IndexLimits(max_file_bytes=20))
        result = limited.query("get_diagnostics", {})
        self.assertTrue(any(row["kind"] == "file_size_limit" for row in result["results"]))
        self.assertLessEqual(result["index"]["indexed_bytes"], 67_108_864)

    def test_workspace_symlinks_and_exclusions_are_not_traversed(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside"
        outside.mkdir()
        self.addCleanup(lambda: outside.exists() and __import__("shutil").rmtree(outside))
        (outside / "secret.py").write_text("class Secret:\n    pass\n", encoding="utf-8")
        (self.root / "linked.py").symlink_to(outside / "secret.py")
        (self.root / "linked-dir").symlink_to(outside, target_is_directory=True)
        for ignored in (".git", "node_modules", "__pycache__", "dist"):
            folder = self.root / ignored
            folder.mkdir()
            (folder / "secret.py").write_text("class Secret:\n    pass\n", encoding="utf-8")
        self.assertEqual(self.query("find_symbol", {"name": "Secret"})["results"], [])
        structure = self.query("get_project_structure")
        paths = {row["path"] for row in structure["results"]}
        self.assertNotIn("linked.py", paths)
        self.assertNotIn("linked-dir", paths)
        self.assertFalse(any(set(Path(path).parts) & EXCLUDED_NAMES for path in paths))

    def test_file_count_total_byte_and_depth_limits_are_reported(self) -> None:
        small = RepositoryIndex(self.root, IndexLimits(max_files=1))
        diagnostics = small.query("get_diagnostics", {})
        self.assertTrue(any(row["kind"] == "index_truncated" for row in diagnostics["results"]))
        total = RepositoryIndex(self.root, IndexLimits(max_total_bytes=10))
        self.assertTrue(total.query("get_diagnostics", {})["truncated"])
        deep_dir = self.root / "one" / "two"
        deep_dir.mkdir(parents=True)
        (deep_dir / "x.py").write_text("pass\n", encoding="utf-8")
        shallow = RepositoryIndex(self.root, IndexLimits(max_depth=1))
        self.assertTrue(any(
            "maximum_directory_depth" in row["message"]
            for row in shallow.query("get_diagnostics", {})["results"]
        ))
        entries_limited = RepositoryIndex(self.root, IndexLimits(max_directory_entries=1))
        self.assertTrue(any(
            "maximum_directory_entries" in row["message"]
            for row in entries_limited.query("get_diagnostics", {})["results"]
        ))

    def test_invalid_limits_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            IndexLimits(max_files=0)
        with self.assertRaises(ValueError):
            IndexLimits(max_search_results=True)
        with self.assertRaises(ValueError):
            IndexLimits(max_scan_seconds=0)
        with self.assertRaises(ValueError):
            IndexLimits(max_scan_seconds=float("nan"))

    def test_unreadable_file_is_reported_as_a_diagnostic(self) -> None:
        with unittest.mock.patch(
            "synai.intelligence.index._read_workspace_file",
            side_effect=PermissionError("access denied"),
        ):
            diagnostics = self.query("get_diagnostics")
        self.assertTrue(any(
            row["kind"] == "unreadable_file" and "access denied" in row["message"]
            for row in diagnostics["results"]
        ))

    def test_diagnostic_result_limit_is_reported(self) -> None:
        for number in range(3):
            (self.root / f"broken_{number}.py").write_text("def broken(:\n", encoding="utf-8")
        limited = RepositoryIndex(self.root, IndexLimits(max_diagnostic_results=1))
        result = limited.query("get_diagnostics", {})
        self.assertLessEqual(len(result["results"]), 1)
        self.assertIn("maximum_diagnostic_results", result["truncation_reasons"])


EXCLUDED_NAMES = {".git", "node_modules", "__pycache__", "dist"}


class IntelligenceToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "workspace"
        self.root.mkdir()
        (self.root / "main.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
        self.settings = replace(
            Settings(), execution_mode="host", history_dir=Path(self.temporary.name) / "history",
        )
        self.backend = type("Backend", (), {})()
        self.backend.settings = self.settings
        self.backend.workspace = self.root
        self.backend.matches = lambda session: session.workspace == str(self.backend.workspace)
        self.backend.execute = AsyncMock()
        self.approve = AsyncMock(return_value=False)
        self.tools = Tools(self.backend, self.approve)
        self.session = Session("test", self.settings.ollama_url, str(self.root))
        self.session.set_environment(ConversationEnvironment.from_settings(self.settings, self.root))

    async def test_registered_schemas_are_strict_and_queries_are_read_only(self) -> None:
        tool_schemas = {item["function"]["name"]: item["function"]["parameters"] for item in schemas()}
        self.assertTrue(INTELLIGENCE_TOOLS.issubset(tool_schemas))
        for name in INTELLIGENCE_TOOLS:
            self.assertFalse(tool_schemas[name]["additionalProperties"])
            self.assertEqual(tool_schemas[name]["required"], list(tool_schemas[name]["properties"]))
        result = await self.tools.call("find_definition", {"name": "answer"}, session=self.session)
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["path"], "main.py")
        self.approve.assert_not_awaited()
        self.backend.execute.assert_not_awaited()

    async def test_malformed_arguments_extra_keys_and_absolute_paths_are_rejected(self) -> None:
        for name, arguments in (
            ("search_code", {"query": "answer", "path": str(self.root)}),
            ("find_symbol", {"name": "../etc/passwd"}),
            ("get_diagnostics", {"path": "/etc/passwd"}),
            ("find_symbol", {"name": 1}),
        ):
            result = await self.tools.call(name, arguments)
            self.assertFalse(result["ok"])
        self.assertEqual(await self.tools.call("search_code", {"query": "answer"}, session=self.session), await self.tools.call(
            "search_code", {"query": "answer"}, session=self.session,
        ))
        self.assertNotIn(str(self.root), json.dumps(
            await self.tools.call("get_project_structure", {}, session=self.session),
        ))

    async def test_missing_or_invalid_workspace_fails_closed(self) -> None:
        self.backend.workspace = None
        result = await self.tools.call("get_project_structure", {}, session=self.session)
        self.assertFalse(result["ok"])
        self.backend.workspace = Path("/")
        result = await self.tools.call("get_project_structure", {}, session=self.session)
        self.assertFalse(result["ok"])

    async def test_workspace_switch_is_rejected_for_the_original_conversation(self) -> None:
        alternate = Path(self.temporary.name) / "alternate"
        alternate.mkdir()
        self.backend.workspace = alternate
        result = await self.tools.call("get_project_structure", {}, session=self.session)
        self.assertFalse(result["ok"])
        self.assertIn("matching active conversation", result["error"])
        self.assertEqual(self.tools._indexes, {})

    async def test_tool_output_is_bounded_by_configured_limit(self) -> None:
        for number in range(40):
            (self.root / f"module_{number:02}.py").write_text(
                f"class Example{number}:\n    pass\n", encoding="utf-8",
            )
        self.backend.settings = replace(self.settings, output_bytes=1024)
        result = await self.tools.call("get_project_structure", {}, session=self.session)
        self.assertTrue(result["ok"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=True).encode()), 1024)
        self.assertTrue(result["truncated"])

    async def test_tool_cancellation_propagates(self) -> None:
        def slow_query(_index, _operation, _arguments, cancellation=None):
            del _index, _operation, _arguments
            while cancellation is not None and not cancellation.wait(0.01):
                pass
            raise InterruptedError("Repository intelligence query cancelled")

        with unittest.mock.patch.object(RepositoryIndex, "query", slow_query):
            task = asyncio.create_task(self.tools.call(
                "get_project_structure", {}, session=self.session,
            ))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
