from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from synai.coding_agent.context import (
    ContextConfidence,
    ContextEngine,
    ContextExpansionRequest,
    ContextItem,
    ContextKind,
    ContextLimits,
    ContextPackage,
    ContextRequest,
    parse_task,
    render_context,
)
from synai.intelligence import IndexLimits, RepositoryIndex


class ContextFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "app").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "app" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "app" / "client.py").write_text(
            "from .retry import RetryPolicy\n"
            "\n"
            "class Client:\n"
            "    def request(self, url):\n"
            "        policy = RetryPolicy()\n"
            "        return send(url, policy)\n"
            "\n"
            "def send(url, policy):\n"
            "    return url\n",
            encoding="utf-8",
        )
        (self.root / "app" / "retry.py").write_text(
            "class RetryPolicy:\n"
            "    def __init__(self):\n"
            "        self.attempts = 3\n",
            encoding="utf-8",
        )
        (self.root / "app" / "service.py").write_text(
            "class Service:\n"
            "    def unrelated(self):\n"
            "        return 'nothing to do here'\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "test_client.py").write_text(
            "from app.client import Client\n"
            "\n"
            "class TestClient:\n"
            "    def test_request_retries(self):\n"
            "        return Client().request('x')\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "test_service.py").write_text(
            "def test_unrelated_service():\n"
            "    assert True\n",
            encoding="utf-8",
        )
        self.index = RepositoryIndex(self.root)
        self.engine = ContextEngine()

    def build(self, task: str, **kwargs) -> ContextPackage:
        return self.engine.build(ContextRequest(task=task, repository=self.index, **kwargs))


class TaskParsingTests(ContextFixture):
    def test_extracts_paths_filenames_symbols_quoted_names_and_tokens(self) -> None:
        terms = parse_task(
            'Update app/client.py and "RetryPolicy" for Client.request; mention retry behavior in service.py.'
        )
        self.assertIn("app/client.py", terms.paths)
        self.assertIn("service.py", terms.filenames)
        self.assertIn("Client.request", terms.qualified_symbols)
        self.assertIn("retry", terms.words)
        self.assertIn("RetryPolicy", terms.identifiers)

    def test_generic_task_and_task_without_useful_identifiers_parse_deterministically(self) -> None:
        generic = parse_task("Make the request more resilient during temporary failures")
        self.assertIn("resilient", generic.words)
        self.assertEqual(parse_task("Update it").identifiers, ("Update", "it"))
        self.assertEqual(parse_task("Update it").words, ())

    def test_invalid_or_empty_task_is_rejected(self) -> None:
        for task in ("", "   ", 3):
            with self.subTest(task=task), self.assertRaises(ValueError):
                parse_task(task)  # type: ignore[arg-type]

    def test_path_traversal_in_task_is_not_loaded(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside.py"
        outside.write_text("class Secret:\n    pass\n", encoding="utf-8")
        self.addCleanup(outside.unlink)
        package = self.build(f"Inspect {outside}")
        self.assertFalse(any("Secret" in item.content for item in package.items))
        self.assertFalse(any(item.path == str(outside) for item in package.items))


class ContextSelectionTests(ContextFixture):
    def test_explicit_path_and_qualified_symbol_rank_above_other_evidence(self) -> None:
        package = self.build(
            "Update app/client.py and improve Client.request handling",
            budget=4_000,
        )
        self.assertEqual(package.items[0].kind, ContextKind.TASK)
        qualified = next(item for item in package.items if item.symbol == "app.client.Client.request")
        explicit_file = next(item for item in package.items if item.kind == ContextKind.FILE)
        self.assertIn("qualified_symbol_match", qualified.reasons)
        self.assertGreater(explicit_file.relevance_score, 0)
        self.assertIn("explicit_user_path", explicit_file.reasons)
        self.assertLess(package.used_budget, package.budget)
        caller = next(item for item in package.items if item.kind == ContextKind.SNIPPET)
        self.assertGreater(qualified.relevance_score, caller.relevance_score)

    def test_symbol_and_related_test_discovery_avoid_unrelated_module(self) -> None:
        package = self.build("Add retry handling to Client.request and update its tests")
        selected = {(item.path, item.symbol) for item in package.items}
        self.assertIn(("app/client.py", "app.client.Client.request"), selected)
        self.assertTrue(any(
            item.kind == ContextKind.TEST and item.path == "tests/test_client.py"
            for item in package.items
        ))
        self.assertFalse(any(item.path == "app/service.py" for item in package.items))
        self.assertTrue(any("related_test" in item.reasons for item in package.items))

    def test_explicit_filename_and_quoted_identifier_are_prioritized(self) -> None:
        package = self.build('Improve "RetryPolicy" in client.py')
        self.assertTrue(any(
            item.path == "app/client.py" and "task_filename_match" in item.reasons
            for item in package.items
        ))
        self.assertTrue(any(
            item.symbol == "app.retry.RetryPolicy" and "quoted_term_match" in item.reasons
            for item in package.items
        ))

    def test_recent_modified_files_only_get_boost_in_selected_neighborhood(self) -> None:
        package = self.build(
            "Improve Client.request",
            previously_modified_files=("app/client.py", "app/service.py"),
        )
        client = [item for item in package.items if item.path == "app/client.py"]
        service = [item for item in package.items if item.path == "app/service.py"]
        self.assertTrue(any("recently_modified" in item.reasons for item in client))
        self.assertFalse(service)

    def test_syntactic_call_and_reference_evidence_is_low_confidence(self) -> None:
        package = self.build("Improve Client.request")
        self.assertTrue(any(
            item.confidence == ContextConfidence.LOW
            and item.resolution in {"syntactic_name_match", "lexical_ast_match"}
            for item in package.items
        ))
        self.assertTrue(any(
            "syntactic" in limitation.lower() or "lexical" in limitation.lower()
            for limitation in package.limitations
        ))

    def test_import_dependency_is_included_shallowly(self) -> None:
        package = self.build("Improve Client.request and RetryPolicy")
        dependencies = [
            item for item in package.items
            if item.kind == ContextKind.DEPENDENCY and item.path == "app/retry.py"
        ]
        self.assertTrue(dependencies)
        self.assertTrue(any("import_dependency" in item.reasons for item in dependencies))

    def test_missing_explicit_path_and_symbol_are_reported_without_fabrication(self) -> None:
        package = self.build("Fix missing/file.py and NonexistentSymbol")
        self.assertFalse(any(item.path == "missing/file.py" for item in package.items))
        self.assertFalse(any(item.symbol and "NonexistentSymbol" in item.symbol for item in package.items))
        self.assertTrue(any("not available" in limitation for limitation in package.limitations))

    def test_ambiguous_exact_symbol_is_not_claimed_as_high_confidence_qualified_fact(self) -> None:
        (self.root / "app" / "other.py").write_text(
            "class Client:\n"
            "    def request(self):\n"
            "        return None\n",
            encoding="utf-8",
        )
        package = self.build("Improve Client.request")
        matching = [
            item for item in package.items if item.symbol and item.symbol.endswith(".request")
        ]
        self.assertTrue(matching)
        self.assertTrue(all(item.confidence != ContextConfidence.HIGH for item in matching))

    def test_relevant_diagnostics_and_incomplete_index_are_propagated(self) -> None:
        (self.root / "app" / "client.py").write_text("def broken(:\n", encoding="utf-8")
        (self.root / "app" / "service.py").write_text("def also_broken(:\n", encoding="utf-8")
        package = self.build("Fix app/client.py")
        self.assertTrue(any(
            item.kind == ContextKind.DIAGNOSTIC and item.path == "app/client.py"
            for item in package.items
        ))
        self.assertFalse(any(
            item.kind == ContextKind.DIAGNOSTIC and item.path == "app/service.py"
            for item in package.items
        ))
        incomplete_index = RepositoryIndex(self.root, IndexLimits(max_files=1))
        limited = ContextEngine().build(ContextRequest("Improve Client.request", incomplete_index))
        self.assertTrue(any("incomplete" in item.lower() for item in limited.limitations))

    def test_symlink_and_excluded_sources_are_not_selectable(self) -> None:
        outside = self.root.parent / f"{self.root.name}-secret.py"
        outside.write_text("class SecretPolicy:\n    pass\n", encoding="utf-8")
        self.addCleanup(outside.unlink)
        (self.root / "outside.py").symlink_to(outside)
        ignored = self.root / ".venv"
        ignored.mkdir()
        (ignored / "hidden.py").write_text("class HiddenPolicy:\n    pass\n", encoding="utf-8")
        package = self.build("Inspect outside.py and HiddenPolicy")
        self.assertFalse(any(item.path in {"outside.py", ".venv/hidden.py"} for item in package.items))
        self.assertFalse(any("SecretPolicy" in item.content for item in package.items))


class ContextBudgetAndRenderingTests(ContextFixture):
    def test_budget_reserve_and_high_priority_items_survive_truncation(self) -> None:
        limits = ContextLimits(
            default_budget=500,
            max_context_budget=1_000,
            reserve_fraction=0.2,
            max_snippet_characters=1_000,
            max_selected_files=10,
        )
        engine = ContextEngine(limits)
        package = engine.build(ContextRequest(
            "Improve Client.request and update tests",
            self.index,
            budget=500,
        ))
        self.assertLessEqual(package.used_budget + package.reserve, package.budget)
        self.assertEqual(package.items[0].kind, ContextKind.TASK)
        self.assertTrue(package.truncated)
        self.assertIn("context_budget_exhausted", package.truncation_reasons)
        self.assertGreater(package.reserve, 0)
        self.assertTrue(package.expansion_candidates)

    def test_large_symbol_range_is_clipped_with_correct_source_locations(self) -> None:
        (self.root / "app" / "large.py").write_text(
            "def enormous():\n" + "".join(f"    value_{line} = {line}\n" for line in range(100)),
            encoding="utf-8",
        )
        engine = ContextEngine(ContextLimits(
            default_budget=5_000, max_context_budget=10_000,
            max_snippet_lines=8, max_snippet_characters=100,
        ))
        package = engine.build(ContextRequest("Inspect enormous", self.index))
        item = next(item for item in package.items if item.symbol == "app.large.enormous")
        self.assertLessEqual(item.end_line - item.start_line + 1, 8)
        self.assertLessEqual(len(item.content), 100)
        self.assertTrue(item.limitations)

    def test_unicode_source_cost_is_character_count_and_serialization_round_trips(self) -> None:
        (self.root / "app" / "unicode.py").write_text(
            "def café():\n    return 'naïve'\n",
            encoding="utf-8",
        )
        package = self.build("Inspect café")
        encoded = package.to_dict()
        self.assertEqual(ContextPackage.from_dict(encoded), package)
        self.assertEqual(json.loads(json.dumps(encoded, ensure_ascii=False))["task"], package.task)
        unicode_item = next(item for item in package.items if item.symbol == "app.unicode.café")
        self.assertEqual(unicode_item.estimated_cost, len(unicode_item.content))
        self.assertIn("naïve", unicode_item.content)

    def test_repeated_runs_are_ordered_and_deterministic(self) -> None:
        first = self.build("Improve Client.request and update its tests")
        second = self.build("Improve Client.request and update its tests")
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(
            [(item.relevance_score, item.path, item.start_line) for item in first.items],
            sorted(
                [(item.relevance_score, item.path, item.start_line) for item in first.items],
                key=lambda row: (-row[0], row[1] or "", row[2] or 0),
            ),
        )

    def test_renderer_explains_location_reason_and_respects_limit(self) -> None:
        package = self.build("Improve Client.request")
        rendered = render_context(package, max_characters=1_000)
        self.assertIn("[task]", rendered)
        self.assertIn("reasons=", rendered)
        self.assertIn("confidence=", rendered)
        self.assertLessEqual(len(render_context(package, max_characters=10)), 10)

    def test_package_deserialization_rejects_corrupt_budget_and_items(self) -> None:
        package = self.build("Improve Client.request")
        corrupted = package.to_dict()
        corrupted["used_budget"] += 1
        with self.assertRaises(ValueError):
            ContextPackage.from_dict(corrupted)
        bad_item = package.items[0].to_dict()
        bad_item["reasons"] = []
        with self.assertRaises(ValueError):
            ContextItem.from_dict(bad_item)

    def test_context_build_time_limit_returns_structured_minimal_package(self) -> None:
        engine = ContextEngine(ContextLimits(
            default_budget=1_000, max_context_budget=1_000, max_build_seconds=0.01,
        ))
        original = RepositoryIndex.query

        def slow_query(index, operation, arguments, cancellation=None):
            time.sleep(0.03)
            return original(index, operation, arguments, cancellation)

        with patch.object(RepositoryIndex, "query", slow_query):
            package = engine.build(ContextRequest("Improve Client.request", self.index))
        self.assertTrue(package.truncated)
        self.assertIn("maximum_context_build_duration", package.truncation_reasons)
        self.assertEqual([item.kind for item in package.items], [ContextKind.TASK])


class ContextExpansionTests(ContextFixture):
    def test_explicit_file_expansion_adds_bounded_context_and_reason(self) -> None:
        package = self.build("Improve Client.request", budget=3_000)
        expanded = self.engine.expand_context(
            package,
            ContextExpansionRequest("file", "app/client.py", path="app/client.py", start_line=1, end_line=2),
            self.index,
        )
        item = next(item for item in expanded.items if item.path == "app/client.py" and item.kind == ContextKind.FILE)
        self.assertIn("context_expansion", item.reasons)
        self.assertLessEqual(expanded.used_budget + expanded.reserve, expanded.budget)

    def test_symbol_expansion_selects_requested_symbol(self) -> None:
        package = self.build("Review retry changes", budget=4_000)
        expanded = self.engine.expand_context(
            package, ContextExpansionRequest("symbol", "app.retry.RetryPolicy"), self.index,
        )
        self.assertTrue(any(
            item.symbol == "app.retry.RetryPolicy" and "context_expansion" in item.reasons
            for item in expanded.items
        ))

    def test_expansion_of_deleted_file_fails_without_stale_context(self) -> None:
        package = self.build("Improve Client.request")
        (self.root / "app" / "client.py").unlink()
        with self.assertRaises(ValueError):
            self.engine.expand_context(
                package,
                ContextExpansionRequest("file", "app/client.py", path="app/client.py"),
                self.index,
            )

    def test_invalid_or_out_of_workspace_expansion_is_rejected(self) -> None:
        package = self.build("Improve Client.request")
        for request in (
            ContextExpansionRequest("invalid", "anything"),
            ContextExpansionRequest("file", "../outside.py"),
            ContextExpansionRequest("file", "/etc/passwd", path="/etc/passwd"),
        ):
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.engine.expand_context(package, request, self.index)

    def test_expansion_budget_and_cancellation_are_respected(self) -> None:
        package = self.build("x", budget=200)
        expanded = self.engine.expand_context(
            package, ContextExpansionRequest("file", "app/client.py", path="app/client.py"), self.index,
        )
        self.assertTrue(
            expanded.used_budget + expanded.reserve <= expanded.budget
            or "context_budget_exhausted" in expanded.truncation_reasons
        )
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(InterruptedError):
            self.engine.expand_context(
                package, ContextExpansionRequest("file", "app/client.py", path="app/client.py"),
                self.index, cancelled,
            )
