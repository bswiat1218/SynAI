from __future__ import annotations

import json
import math
import re
import threading
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from synai.intelligence.index import RepositoryIndex, SourceSnapshot


class ContextKind(StrEnum):
    TASK = "task"
    METADATA = "metadata"
    SYMBOL = "symbol"
    FILE = "file"
    TEST = "test"
    DIAGNOSTIC = "diagnostic"
    DEPENDENCY = "dependency"
    DIFF = "diff"
    SNIPPET = "snippet"


class ContextConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class ContextLimits:
    default_budget: int = 12_000
    max_context_budget: int = 48_000
    reserve_fraction: float = 0.2
    max_selected_files: int = 12
    max_selected_symbols: int = 24
    max_selected_tests: int = 8
    max_snippets: int = 20
    max_snippet_lines: int = 80
    max_snippet_characters: int = 4_000
    max_dependency_depth: int = 1
    max_query_candidates: int = 12
    max_intelligence_queries: int = 64
    max_build_seconds: float = 5.0
    surrounding_lines: int = 2

    def __post_init__(self) -> None:
        integers = (
            self.default_budget, self.max_context_budget, self.max_selected_files,
            self.max_selected_symbols, self.max_selected_tests, self.max_snippets,
            self.max_snippet_lines, self.max_snippet_characters,
            self.max_query_candidates, self.max_intelligence_queries, self.surrounding_lines,
        )
        if any(type(value) is not int or value < 0 for value in integers):
            raise ValueError("Context limits must be non-negative integers")
        if any(value < 1 for value in (
            self.max_snippet_lines, self.max_snippet_characters,
            self.max_query_candidates, self.max_intelligence_queries,
        )):
            raise ValueError("Context query and snippet limits must be positive")
        if self.default_budget < 1 or self.max_context_budget < self.default_budget:
            raise ValueError("Context budget limits are inconsistent")
        if type(self.max_dependency_depth) is not int or not 0 <= self.max_dependency_depth <= 1:
            raise ValueError("Context dependency depth must be 0 or 1")
        if (
            isinstance(self.reserve_fraction, bool)
            or not isinstance(self.reserve_fraction, (int, float))
            or not math.isfinite(self.reserve_fraction)
            or not 0 <= self.reserve_fraction < 0.5
        ):
            raise ValueError("Context reserve fraction must be in [0, 0.5)")
        if (
            isinstance(self.max_build_seconds, bool)
            or not isinstance(self.max_build_seconds, (int, float))
            or not math.isfinite(self.max_build_seconds)
            or self.max_build_seconds <= 0
        ):
            raise ValueError("Context build duration must be positive")


@dataclass(frozen=True)
class ContextRequest:
    task: str
    repository: RepositoryIndex
    task_metadata: str | None = None
    current_plan_step: str | None = None
    previously_modified_files: tuple[str, ...] = ()
    previous_context_selections: tuple[str, ...] = ()
    budget: int | None = None
    result_limit: int | None = None

    def validate(self, limits: ContextLimits) -> None:
        if not isinstance(self.task, str) or not self.task.strip() or len(self.task) > 16_384:
            raise ValueError("Context task must be non-empty text of at most 16384 characters")
        if not isinstance(self.repository, RepositoryIndex):
            raise ValueError("Context request requires a repository index")
        for label, value in (
            ("task metadata", self.task_metadata),
            ("current plan step", self.current_plan_step),
        ):
            if value is not None and (
                not isinstance(value, str) or len(value) > 4_096
            ):
                raise ValueError(f"Context {label} must be bounded text")
        for paths in (self.previously_modified_files, self.previous_context_selections):
            if not isinstance(paths, tuple) or any(
                not _safe_relative_path(path) for path in paths
            ):
                raise ValueError("Context paths must be workspace-relative")
        if self.budget is not None and (
            type(self.budget) is not int or not 1 <= self.budget <= limits.max_context_budget
        ):
            raise ValueError("Context budget is outside configured bounds")
        if self.result_limit is not None and (
            type(self.result_limit) is not int or not 1 <= self.result_limit <= limits.max_query_candidates
        ):
            raise ValueError("Context result limit is outside configured bounds")


@dataclass(frozen=True)
class ContextItem:
    kind: ContextKind
    path: str | None
    symbol: str | None
    start_line: int | None
    end_line: int | None
    content: str
    relevance_score: int
    reasons: tuple[str, ...]
    source: str
    resolution: str
    confidence: ContextConfidence
    estimated_cost: int
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["kind"] = self.kind.value
        result["confidence"] = self.confidence.value
        return result

    @classmethod
    def from_dict(cls, value: object) -> ContextItem:
        keys = {
            "kind", "path", "symbol", "start_line", "end_line", "content",
            "relevance_score", "reasons", "source", "resolution", "confidence",
            "estimated_cost", "limitations",
        }
        if not isinstance(value, dict) or set(value) != keys:
            raise ValueError("Invalid context item fields")
        try:
            result = cls(
                kind=ContextKind(value["kind"]),
                path=value["path"],
                symbol=value["symbol"],
                start_line=value["start_line"],
                end_line=value["end_line"],
                content=value["content"],
                relevance_score=value["relevance_score"],
                reasons=tuple(value["reasons"]),
                source=value["source"],
                resolution=value["resolution"],
                confidence=ContextConfidence(value["confidence"]),
                estimated_cost=value["estimated_cost"],
                limitations=tuple(value["limitations"]),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid context item: {exc}") from exc
        if (
            not isinstance(result.content, str)
            or (result.path is not None and not isinstance(result.path, str))
            or (result.symbol is not None and not isinstance(result.symbol, str))
            or not isinstance(result.reasons, tuple)
            or not result.reasons
            or any(not isinstance(reason, str) for reason in result.reasons)
            or not isinstance(result.limitations, tuple)
            or any(not isinstance(item, str) for item in result.limitations)
            or type(result.estimated_cost) is not int
            or result.estimated_cost != len(result.content)
            or type(result.relevance_score) is not int
            or not isinstance(result.source, str) or not result.source
            or not isinstance(result.resolution, str) or not result.resolution
            or (result.path is not None and not _safe_relative_path(result.path))
            or any(value is not None and (type(value) is not int or value < 1)
                   for value in (result.start_line, result.end_line))
            or (result.start_line is not None and result.end_line is not None
                and result.end_line < result.start_line)
        ):
            raise ValueError("Invalid context item values")
        return result


@dataclass(frozen=True)
class ContextPackage:
    task: str
    items: tuple[ContextItem, ...]
    budget: int
    reserve: int
    used_budget: int
    remaining_budget: int
    truncated: bool
    truncation_reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    expansion_candidates: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "items": [item.to_dict() for item in self.items],
            "budget": self.budget,
            "reserve": self.reserve,
            "used_budget": self.used_budget,
            "remaining_budget": self.remaining_budget,
            "truncated": self.truncated,
            "truncation_reasons": list(self.truncation_reasons),
            "limitations": list(self.limitations),
            "expansion_candidates": [dict(item) for item in self.expansion_candidates],
        }

    @classmethod
    def from_dict(cls, value: object) -> ContextPackage:
        keys = {
            "task", "items", "budget", "reserve", "used_budget", "remaining_budget",
            "truncated", "truncation_reasons", "limitations", "expansion_candidates",
        }
        if not isinstance(value, dict) or set(value) != keys:
            raise ValueError("Invalid context package fields")
        try:
            package = cls(
                task=value["task"],
                items=tuple(ContextItem.from_dict(item) for item in value["items"]),
                budget=value["budget"],
                reserve=value["reserve"],
                used_budget=value["used_budget"],
                remaining_budget=value["remaining_budget"],
                truncated=value["truncated"],
                truncation_reasons=tuple(value["truncation_reasons"]),
                limitations=tuple(value["limitations"]),
                expansion_candidates=tuple(dict(item) for item in value["expansion_candidates"]),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid context package: {exc}") from exc
        if (
            not isinstance(package.task, str) or type(package.budget) is not int
            or type(package.reserve) is not int or type(package.used_budget) is not int
            or type(package.remaining_budget) is not int or type(package.truncated) is not bool
            or package.budget < 1 or package.reserve < 0 or package.used_budget < 0
            or package.used_budget != sum(item.estimated_cost for item in package.items)
            or package.used_budget + package.remaining_budget + package.reserve != package.budget
            or package.remaining_budget < 0 or package.reserve >= package.budget
            or (package.truncated and not package.truncation_reasons)
            or any(not isinstance(reason, str) for reason in package.truncation_reasons)
            or any(not isinstance(reason, str) for reason in package.limitations)
            or any(
                set(item) != {"kind", "path", "reason"}
                or any(not isinstance(value, str) or not value for value in item.values())
                or not _safe_relative_path(item["path"])
                for item in package.expansion_candidates
            )
        ):
            raise ValueError("Invalid context package values")
        return package


@dataclass(frozen=True)
class ContextExpansionRequest:
    target: str
    query: str
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None

    def validate(self) -> None:
        if self.target not in {"file", "symbol", "callers", "references", "tests", "imports"}:
            raise ValueError("Unsupported context expansion target")
        if not isinstance(self.query, str) or not self.query.strip() or len(self.query) > 512:
            raise ValueError("Context expansion query must be bounded and non-empty")
        if self.path is not None and not _safe_relative_path(self.path):
            raise ValueError("Context expansion path must be workspace-relative")
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("Context expansion line range must provide both endpoints")
        if self.start_line is not None and (
            type(self.start_line) is not int or type(self.end_line) is not int
            or self.start_line < 1 or self.end_line < self.start_line
        ):
            raise ValueError("Invalid context expansion line range")


@dataclass(frozen=True)
class _TaskTerms:
    paths: tuple[str, ...]
    filenames: tuple[str, ...]
    qualified_symbols: tuple[str, ...]
    identifiers: tuple[str, ...]
    words: tuple[str, ...]


class _ContextDeadline(Exception):
    pass


class _CombinedCancellation(threading.Event):
    def __init__(self, supplied: threading.Event | None, deadline: threading.Event) -> None:
        super().__init__()
        self.supplied, self.deadline = supplied, deadline

    def is_set(self) -> bool:
        return super().is_set() or self.deadline.is_set() or bool(
            self.supplied and self.supplied.is_set()
        )


SCORE_WEIGHTS: dict[str, int] = {
    "explicit_user_path": 120,
    "qualified_symbol_match": 115,
    "task_exact_symbol_match": 105,
    "task_filename_match": 90,
    "quoted_term_match": 85,
    "task_identifier_match": 70,
    "definition_of_matched_symbol": 60,
    "recently_modified": 35,
    "related_test": 48,
    "import_dependency": 44,
    "same_module": 30,
    "caller_of_matched_symbol": 24,
    "reference_to_matched_symbol": 20,
    "diagnostic_on_selected_file": 40,
    "context_expansion": 130,
    "broader_lexical_search": 10,
    "repository_metadata": 5,
}

_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into",
    "is", "it", "of", "on", "or", "the", "this", "that", "to", "with", "update",
    "add", "change", "fix", "implement", "write", "create", "make", "use", "related",
    "relevant", "test", "tests", "improve", "inspect", "review",
})
_PATH_PATTERN = re.compile(r"(?<![\w.-])(?:[\w.-]+/)+[\w.-]+(?:\.[A-Za-z0-9]+)?")
_QUOTED_PATTERN = re.compile(r"""["'`]([^"'`\n]{1,128})["'`]""")
_DOTTED_PATTERN = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b")
_IDENTIFIER_PATTERN = re.compile(r"\b[A-Za-z_]\w*\b")
_FILENAME_PATTERN = re.compile(
    r"\b[\w.-]+\.(?:pyi?|toml|ya?ml|json|md|rst|txt|cfg|ini|js|jsx|ts|tsx|go|rs|java|c|h|cpp|hpp|cs|rb|php|lua|sh|sql)\b",
    re.IGNORECASE,
)


def parse_task(task: str) -> _TaskTerms:
    if not isinstance(task, str) or not task.strip():
        raise ValueError("Task text must be non-empty")
    paths = tuple(sorted(set(match.rstrip(".,;:)") for match in _PATH_PATTERN.findall(task))))
    filenames = tuple(sorted(set(_FILENAME_PATTERN.findall(task))))
    qualified = tuple(sorted(
        set(_DOTTED_PATTERN.findall(task)) - set(filenames)
    ))
    identifiers = set(_IDENTIFIER_PATTERN.findall(task))
    quoted = _QUOTED_PATTERN.findall(task)
    for quoted_value in quoted:
        identifiers.update(_IDENTIFIER_PATTERN.findall(quoted_value))
    words = {
        token.lower() for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", task)
        if token.lower() not in _STOP_WORDS
    }
    return _TaskTerms(
        paths, filenames, qualified,
        tuple(sorted(identifiers | {value.rsplit(".", 1)[-1] for value in qualified})),
        tuple(sorted(words)),
    )


class ContextEngine:
    def __init__(self, limits: ContextLimits | None = None) -> None:
        self.limits = limits or ContextLimits()

    def build(
        self, request: ContextRequest, cancellation: threading.Event | None = None,
    ) -> ContextPackage:
        request.validate(self.limits)
        deadline = threading.Event()
        timer = threading.Timer(self.limits.max_build_seconds, deadline.set)
        timer.start()
        combined = _CombinedCancellation(cancellation, deadline)
        try:
            return self._build(request, combined)
        except InterruptedError:
            if not deadline.is_set():
                raise
            return self._deadline_package(request)
        except _ContextDeadline:
            return self._deadline_package(request)
        finally:
            timer.cancel()

    def _deadline_package(self, request: ContextRequest) -> ContextPackage:
        budget = request.budget or self.limits.default_budget
        reserve = int(budget * self.limits.reserve_fraction)
        content = request.task.strip()[:min(
            self.limits.max_snippet_characters, budget - reserve,
        )]
        task = ContextItem(
            ContextKind.TASK, None, None, None, None, content, 1000,
            ("task_text",), "user_task", "explicit", ContextConfidence.HIGH,
            len(content), ("Context discovery stopped at its configured time limit.",),
        )
        used = task.estimated_cost
        return ContextPackage(
            request.task, (task,), budget, reserve, used,
            max(0, budget - reserve - used), True,
            ("maximum_context_build_duration",),
            ("Context discovery stopped before repository evidence could be fully collected.",),
            (),
        )

    def _build(
        self, request: ContextRequest, cancellation: threading.Event | None,
    ) -> ContextPackage:
        started = time.monotonic()
        index = request.repository
        terms = parse_task(request.task)
        budget = request.budget or self.limits.default_budget
        result_limit = request.result_limit or self.limits.max_query_candidates
        reserve = int(budget * self.limits.reserve_fraction)
        selection_budget = budget - reserve
        raw_candidates: list[ContextItem] = []
        limitations: set[str] = set()
        truncation: set[str] = set()
        expansion: dict[tuple[str, str], dict[str, str]] = {}
        known_paths: set[str] = set()
        selected_symbols: list[dict[str, Any]] = []
        selected_tests: list[dict[str, Any]] = []
        query_count = 0

        def query(operation: str, arguments: dict[str, str] | None = None) -> dict[str, Any]:
            nonlocal query_count
            self._check_limits(started, cancellation)
            if query_count >= self.limits.max_intelligence_queries:
                truncation.add("maximum_context_queries")
                return {"ok": True, "results": [], "limitations": [], "truncated": True}
            query_count += 1
            result = index.query(operation, arguments or {}, cancellation)
            limitations.update(result.get("limitations", []))
            if result.get("truncated"):
                truncation.update(result.get("truncation_reasons") or ["repository_intelligence_truncated"])
            return result

        task_text = request.task.strip()
        if request.task_metadata:
            task_text += f"\nTask metadata: {request.task_metadata.strip()}"
        if request.current_plan_step:
            task_text += f"\nCurrent step: {request.current_plan_step.strip()}"
        task_text = task_text[:min(self.limits.max_snippet_characters, selection_budget)]
        if len(task_text) < len(request.task.strip()):
            truncation.add("task_context_clipped")
        raw_candidates.append(self._item(
            ContextKind.TASK, None, None, None, None, task_text, 1000,
            ("task_text",), "user_task", "explicit", ContextConfidence.HIGH,
        ))

        structure = query("get_project_structure")
        file_paths = {
            row["path"] for row in structure.get("results", [])
            if row.get("kind") == "file" and isinstance(row.get("path"), str)
        }
        known_paths.update(file_paths)
        if structure.get("truncated"):
            truncation.add("repository_structure_truncated")
        raw_candidates.append(self._item(
            ContextKind.METADATA, None, None, None, None,
            f"Indexed workspace: {structure.get('index', {}).get('indexed_files', len(file_paths))} files; "
            f"bounded structure lists {len(file_paths)} files; scan "
            f"{'complete' if structure.get('index', {}).get('scan_complete') else 'incomplete'}.",
            SCORE_WEIGHTS["repository_metadata"], ("repository_metadata",),
            "repository_intelligence.get_project_structure", "deterministic", ContextConfidence.HIGH,
        ))
        if not structure.get("index", {}).get("scan_complete", True):
            limitations.add("Repository index is incomplete; not all workspace paths may be represented.")

        quoted_identifiers = {
            identifier for quoted in _QUOTED_PATTERN.findall(request.task)
            for identifier in _IDENTIFIER_PATTERN.findall(quoted)
        }
        qualified_parts = {
            part for qualified in terms.qualified_symbols for part in qualified.split(".")
        }
        path_parts = {
            part for path in (*terms.paths, *terms.filenames)
            for part in (*Path(path).parts, Path(path).stem)
        }
        symbol_identifiers = {
            identifier for identifier in terms.identifiers
            if identifier in quoted_identifiers or identifier in qualified_parts
            or (
                identifier.lower() not in _STOP_WORDS
                and identifier.lower() not in {part.lower() for part in path_parts}
            )
        }
        query_names = list(dict.fromkeys((
            *terms.qualified_symbols, *sorted(symbol_identifiers),
        )))
        for name in query_names[:min(result_limit, self.limits.max_query_candidates)]:
            result = query("find_symbol", {"name": name})
            matches = result.get("results", [])
            if matches:
                selected_symbols.extend(matches[:min(
                    self.limits.max_selected_symbols, result_limit,
                )])
                simple_reason = (
                    "task_exact_symbol_match"
                    if name in terms.qualified_symbols or name in quoted_identifiers
                    or "_" in name or any(char.isupper() for char in name)
                    else "task_identifier_match"
                )
                if name in quoted_identifiers:
                    simple_reason = "quoted_term_match"
                for symbol in matches[:min(self.limits.max_selected_symbols, result_limit)]:
                    qualified_matches = [
                        qualified for qualified in terms.qualified_symbols
                        if symbol["qualified_name"].endswith(qualified)
                        or (
                            symbol["name"] == qualified.rsplit(".", 1)[-1]
                            and qualified.rsplit(".", 1)[0].split(".")[-1]
                            in symbol["qualified_name"].split(".")
                        )
                    ]
                    reason = "qualified_symbol_match" if qualified_matches else simple_reason
                    confidence = ContextConfidence.HIGH
                    if qualified_matches:
                        same_suffix = [
                            candidate for candidate in matches
                            if any(candidate["qualified_name"].endswith(qualified) for qualified in qualified_matches)
                        ]
                        if len(same_suffix) > 1:
                            confidence = ContextConfidence.MEDIUM
                            limitations.add(
                                f"Qualified symbol {qualified_matches[0]} is ambiguous; exact candidates are retained."
                            )
                    elif len(matches) > 1:
                        confidence = ContextConfidence.MEDIUM
                        limitations.add(
                            f"Symbol name {name} has multiple definitions; each candidate is retained."
                        )
                    raw_candidates.append(self._symbol_item(
                        index, symbol, (reason,), SCORE_WEIGHTS[reason], confidence,
                        cancellation,
                    ))

        for path in terms.paths:
            self._check_limits(started, cancellation)
            if not _safe_relative_path(path):
                limitations.add(f"Rejected unsafe explicit path: {path}")
                continue
            if path not in known_paths:
                snapshot = index.read_source(path, cancellation)
                if snapshot is not None:
                    known_paths.add(path)
            else:
                snapshot = index.read_source(path, cancellation)
            if snapshot is None:
                limitations.add(f"Explicit path not available in the validated workspace: {path}")
                continue
            raw_candidates.append(self._file_item(
                snapshot, "explicit_user_path", SCORE_WEIGHTS["explicit_user_path"],
                ContextConfidence.HIGH,
            ))

        for filename in _filename_mentions(request.task):
            for path in sorted(known_paths):
                if Path(path).name == filename:
                    snapshot = index.read_source(path, cancellation)
                    if snapshot:
                        raw_candidates.append(self._file_item(
                            snapshot, "task_filename_match", SCORE_WEIGHTS["task_filename_match"],
                            ContextConfidence.HIGH,
                        ))

        matched_names = sorted({
            item["name"] for item in selected_symbols if item.get("kind") != "module"
        })
        matched_names = matched_names[:self.limits.max_selected_symbols]
        symbols_by_path: dict[str, list[dict[str, Any]]] = {}
        for symbol in selected_symbols:
            symbols_by_path.setdefault(symbol["path"], []).append(symbol)
        if self.limits.max_dependency_depth >= 1:
            for source_path, source_symbols in sorted(symbols_by_path.items()):
                self._check_limits(started, cancellation)
                imports = index.imports_for_file(source_path, cancellation)
                for relation in imports[:result_limit]:
                    bound = relation["bound_name"]
                    if not any(re.search(rf"\b{re.escape(bound)}\b", symbol["qualified_name"])
                               or any(
                                   bound in item.content
                                   for item in raw_candidates
                                   if item.path == source_path and item.kind == ContextKind.SYMBOL
                               )
                               for symbol in source_symbols):
                        continue
                    imported = query("find_imports", {"query": bound})
                    confirmed = next((
                        record for record in imported.get("results", [])
                        if record["path"] == source_path and record["line"] == relation["line"]
                    ), None)
                    if confirmed is None:
                        continue
                    source = index.read_source(source_path, cancellation)
                    if source:
                        raw_candidates.append(self._line_item(
                            source, confirmed["line"], ContextKind.DEPENDENCY,
                            confirmed.get("name") or bound,
                            "import_dependency", SCORE_WEIGHTS["import_dependency"],
                            "repository_intelligence.find_imports", ContextConfidence.MEDIUM,
                            "syntactic_import",
                            ("Import relationship is based on source syntax; runtime import resolution is not performed.",),
                        ))
                        expansion[("dependency", source_path)] = {
                            "kind": "dependency", "path": source_path, "reason": "import_dependency",
                        }
                    module = confirmed.get("module")
                    imported_name = confirmed.get("name")
                    if isinstance(module, str) and isinstance(imported_name, str) and imported_name != "*":
                        target = query("find_definition", {"name": f"{module}.{imported_name}"})
                        for symbol in target.get("results", [])[:min(2, result_limit)]:
                            raw_candidates.append(self._symbol_item(
                                index, symbol,
                                ("definition_of_matched_symbol", "import_dependency"),
                                SCORE_WEIGHTS["definition_of_matched_symbol"]
                                + SCORE_WEIGHTS["import_dependency"],
                                ContextConfidence.MEDIUM, cancellation,
                                resolution="syntactic_import_definition",
                                limitations=("Imported definition is associated through static source import syntax.",),
                                kind=ContextKind.DEPENDENCY,
                            ))

        for symbol in selected_symbols:
            if symbol["kind"] != "class":
                continue
            implementations = query("find_implementations", {"name": symbol["qualified_name"]})
            for implementation in implementations.get("results", [])[:result_limit]:
                raw_candidates.append(self._symbol_item(
                    index, implementation,
                    ("definition_of_matched_symbol",), SCORE_WEIGHTS["definition_of_matched_symbol"],
                    ContextConfidence.HIGH, cancellation,
                    resolution="direct_static_inheritance",
                    kind=ContextKind.SYMBOL,
                ))

        for symbol_name in matched_names[:min(4, self.limits.max_query_candidates)]:
            callers = query("find_callers", {"name": symbol_name})
            for caller in callers.get("results", [])[:result_limit]:
                source = index.read_source(caller["path"], cancellation)
                if source:
                    raw_candidates.append(self._line_item(
                        source, caller["line"], ContextKind.SNIPPET, caller.get("scope"),
                        "caller_of_matched_symbol", SCORE_WEIGHTS["caller_of_matched_symbol"],
                        "repository_intelligence.find_callers", ContextConfidence.LOW,
                        "syntactic_name_match",
                        tuple(callers.get("limitations", [])),
                    ))
                    expansion[("callers", symbol_name)] = {
                        "kind": "callers", "path": caller["path"], "reason": "caller_of_matched_symbol",
                    }
            references = query("find_references", {"name": symbol_name})
            for reference in references.get("results", [])[:result_limit]:
                if reference.get("path", "").startswith("tests/") or "/tests/" in reference.get("path", ""):
                    source = index.read_source(reference["path"], cancellation)
                    if source:
                        raw_candidates.append(self._line_item(
                            source, reference["line"], ContextKind.TEST, reference["name"],
                            "related_test", SCORE_WEIGHTS["related_test"],
                            "repository_intelligence.find_references", ContextConfidence.LOW,
                            "lexical_ast_match", tuple(references.get("limitations", [])),
                        ))
                        expansion[("tests", reference["path"])] = {
                            "kind": "tests", "path": reference["path"], "reason": "related_test",
                        }

        tests_result = query("find_tests")
        all_tests = tests_result.get("results", [])
        for test in all_tests[:result_limit]:
            path = test.get("path")
            if not isinstance(path, str):
                continue
            source_path = Path(path)
            production_names = {Path(symbol["path"]).stem for symbol in selected_symbols}
            related_by_path = source_path.stem.removeprefix("test_").removesuffix("_test") in production_names
            related_by_symbol = any(
                test["name"].lower() == f"test_{name.lower()}"
                or (test["kind"] == "class" and name.lower() in test["name"].lower())
                for name in matched_names
            )
            explicit = any(test["name"] in quoted for quoted in _QUOTED_PATTERN.findall(request.task))
            previous = path in request.previous_context_selections
            if not (related_by_path or related_by_symbol or explicit or previous):
                continue
            source = index.read_source(path, cancellation)
            if source is None:
                continue
            reasons = ["related_test"]
            if explicit:
                reasons.append("quoted_term_match")
            if previous:
                reasons.append("context_expansion")
            selected_tests.append(test)
            raw_candidates.append(self._symbol_item(
                index, test, tuple(reasons), SCORE_WEIGHTS["related_test"],
                ContextConfidence.MEDIUM, cancellation, kind=ContextKind.TEST,
            ))
            expansion[("tests", path)] = {
                "kind": "tests", "path": path, "reason": "related_test",
            }
            if len(selected_tests) >= self.limits.max_selected_tests:
                truncation.add("maximum_selected_tests")
                break
        limitations.update(tests_result.get("limitations", []))

        selected_paths = {item.path for item in raw_candidates if item.path is not None}
        for modified in request.previously_modified_files:
            if modified not in selected_paths:
                continue
            source = index.read_source(modified, cancellation)
            if source:
                raw_candidates.append(self._with_reason(
                    self._file_item(source, "recently_modified", SCORE_WEIGHTS["recently_modified"],
                                    ContextConfidence.MEDIUM),
                    "recently_modified", SCORE_WEIGHTS["recently_modified"],
                ))

        # Diagnostics are attached only to explicitly or structurally selected source paths.
        diagnostics = query("get_diagnostics")
        for diagnostic in diagnostics.get("results", [])[:result_limit]:
            path = diagnostic.get("path")
            if path is None and diagnostic.get("kind") in {"index_incomplete", "index_truncated"}:
                limitations.add(f"Repository diagnostic: {diagnostic.get('message', '')}")
                continue
            if path not in selected_paths and path not in terms.paths:
                continue
            content = json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)
            raw_candidates.append(self._item(
                ContextKind.DIAGNOSTIC, path, None, diagnostic.get("line"), diagnostic.get("line"),
                content, SCORE_WEIGHTS["diagnostic_on_selected_file"],
                ("diagnostic_on_selected_file",), "repository_intelligence.get_diagnostics",
                "deterministic_diagnostic", ContextConfidence.HIGH,
            ))

        if len(selected_paths) < 2:
            for word in terms.words[:min(3, self.limits.max_query_candidates)]:
                search = query("search_code", {"query": word})
                for match in search.get("results", [])[:result_limit]:
                    path = match.get("path")
                    if not isinstance(path, str):
                        continue
                    source = index.read_source(path, cancellation)
                    if source:
                        raw_candidates.append(self._line_item(
                            source, match["line"], ContextKind.SNIPPET, None,
                            "broader_lexical_search", SCORE_WEIGHTS["broader_lexical_search"],
                            "repository_intelligence.search_code", ContextConfidence.LOW,
                            "literal_text_match", (),
                        ))

        for path in selected_paths:
            if path in request.previously_modified_files:
                raw_candidates = [
                    self._with_reason(candidate, "recently_modified", SCORE_WEIGHTS["recently_modified"])
                    if candidate.path == path else candidate
                    for candidate in raw_candidates
                ]

        self._check_limits(started, cancellation)
        candidates = self._deduplicate(raw_candidates)
        candidates.sort(key=self._sort_key)
        selected: list[ContextItem] = []
        dropped: list[ContextItem] = []
        used = 0
        file_count = symbol_count = test_count = snippet_count = 0
        selected_paths: set[str] = set()
        for candidate in candidates:
            new_path = candidate.path is not None and candidate.path not in selected_paths
            if new_path and file_count >= self.limits.max_selected_files:
                dropped.append(candidate)
                truncation.add("maximum_selected_files")
                continue
            if candidate.kind == ContextKind.SYMBOL and symbol_count >= self.limits.max_selected_symbols:
                dropped.append(candidate)
                truncation.add("maximum_selected_symbols")
                continue
            if candidate.kind == ContextKind.TEST and test_count >= self.limits.max_selected_tests:
                dropped.append(candidate)
                truncation.add("maximum_selected_tests")
                continue
            if candidate.kind == ContextKind.SNIPPET and snippet_count >= self.limits.max_snippets:
                dropped.append(candidate)
                truncation.add("maximum_snippets")
                continue
            if used + candidate.estimated_cost > selection_budget:
                dropped.append(candidate)
                truncation.add("context_budget_exhausted")
                continue
            selected.append(candidate)
            used += candidate.estimated_cost
            if new_path and candidate.path is not None:
                selected_paths.add(candidate.path)
                file_count += 1
            symbol_count += candidate.kind == ContextKind.SYMBOL
            test_count += candidate.kind == ContextKind.TEST
            snippet_count += candidate.kind == ContextKind.SNIPPET
        if len(candidates) > len(selected):
            for candidate in dropped:
                if candidate.path:
                    expansion[(candidate.kind.value, candidate.path)] = {
                        "kind": candidate.kind.value,
                        "path": candidate.path,
                        "reason": candidate.reasons[0],
                    }
        selected.sort(key=self._sort_key)
        return ContextPackage(
            task=request.task,
            items=tuple(selected),
            budget=budget,
            reserve=reserve,
            used_budget=used,
            remaining_budget=budget - reserve - used,
            truncated=bool(truncation),
            truncation_reasons=tuple(sorted(truncation)),
            limitations=tuple(sorted(limitations)),
            expansion_candidates=tuple(expansion[key] for key in sorted(expansion)),
        )

    def expand_context(
        self, package: ContextPackage, request: ContextExpansionRequest,
        repository: RepositoryIndex, cancellation: threading.Event | None = None,
    ) -> ContextPackage:
        request.validate()
        if not isinstance(package, ContextPackage) or not isinstance(repository, RepositoryIndex):
            raise ValueError("Context expansion requires a context package and repository index")
        if package.remaining_budget <= 0:
            return self._mark_truncated(package, "context_budget_exhausted")
        if request.target == "file":
            path = request.path or request.query
            if not _safe_relative_path(path):
                raise ValueError("Context expansion requires a safe workspace-relative path")
            snapshot = repository.read_source(path, cancellation)
            if snapshot is None:
                raise ValueError("Requested context file is unavailable in this workspace")
            start = request.start_line or 1
            end = request.end_line or len(snapshot.text.splitlines()) or 1
            item = self._range_item(
                snapshot, start, end, ContextKind.FILE, None, "context_expansion",
                SCORE_WEIGHTS["context_expansion"], ContextConfidence.HIGH,
                "repository_index.read_source",
            )
        else:
            arguments = {"name": request.query} if request.target in {
                "symbol", "callers", "references",
            } else {"query": request.query}
            operation = {
                "symbol": "find_definition",
                "callers": "find_callers",
                "references": "find_references",
                "tests": "find_tests",
                "imports": "find_imports",
            }[request.target]
            evidence = repository.query(operation, arguments, cancellation)
            results = evidence.get("results", [])
            if request.target == "tests":
                results = [
                    record for record in results
                    if request.query in {
                        record.get("path"), record.get("name"), record.get("qualified_name"),
                    }
                ]
            if not results:
                raise ValueError("Requested context expansion returned no repository evidence")
            record = results[0]
            path = record.get("path")
            if not isinstance(path, str):
                raise ValueError("Requested context expansion has no workspace-relative location")
            snapshot = repository.read_source(path, cancellation)
            if snapshot is None:
                raise ValueError("Requested context source changed or disappeared")
            if "line" in record:
                item = self._line_item(
                    snapshot, record["line"], ContextKind.SYMBOL,
                    record.get("qualified_name", record.get("scope")),
                    "context_expansion", SCORE_WEIGHTS["context_expansion"],
                    f"repository_intelligence.{operation}", ContextConfidence.MEDIUM,
                    record.get("resolution", "static_query_match"),
                    tuple(evidence.get("limitations", [])),
                )
            else:
                item = self._symbol_item(
                    repository, record, ("context_expansion",),
                    SCORE_WEIGHTS["context_expansion"], ContextConfidence.HIGH,
                    cancellation,
                )
            expansion_limitations = tuple(evidence.get("limitations", []))
        if request.target == "file":
            expansion_limitations = ()
        if item is None:
            raise ValueError("Requested context source changed or disappeared")
        expansion_truncated = False
        if item.estimated_cost > package.remaining_budget:
            clipped = self._clip_item(item, package.remaining_budget)
            if clipped is None:
                return self._mark_truncated(package, "context_budget_exhausted")
            item = clipped
            expansion_truncated = True
        retained = tuple(existing for existing in package.items if (
            existing.path, existing.symbol, existing.start_line, existing.kind
        ) != (item.path, item.symbol, item.start_line, item.kind))
        items = (*retained, item)
        used = sum(existing.estimated_cost for existing in items)
        return ContextPackage(
            task=package.task,
            items=tuple(items),
            budget=package.budget,
            reserve=package.reserve,
            used_budget=used,
            remaining_budget=package.budget - package.reserve - used,
            truncated=package.truncated or expansion_truncated,
            truncation_reasons=tuple(sorted(set(
                (*package.truncation_reasons, *(("context_expansion_clipped",) if expansion_truncated else ())),
            ))),
            limitations=tuple(sorted(set((*package.limitations, *expansion_limitations)))),
            expansion_candidates=package.expansion_candidates,
        )

    def _symbol_item(
        self, index: RepositoryIndex, symbol: dict[str, Any], reasons: tuple[str, ...],
        score: int, confidence: ContextConfidence, cancellation: threading.Event | None,
        *, resolution: str = "exact_ast_definition",
        limitations: tuple[str, ...] = (), kind: ContextKind = ContextKind.SYMBOL,
    ) -> ContextItem | None:
        path = symbol.get("path")
        qualified_name = symbol.get("qualified_name")
        if not isinstance(path, str) or not isinstance(qualified_name, str):
            return None
        fresh = next((
            (current, snapshot)
            for current, snapshot in index.read_symbol_source(qualified_name, cancellation)
            if current["path"] == path
        ), None)
        if fresh is None:
            return None
        current_symbol, snapshot = fresh
        symbol = current_symbol
        start = symbol.get("line") or 1
        end = symbol.get("end_line") or start
        return self._range_item(
            snapshot, start, end, kind, symbol.get("qualified_name"),
            reasons[0], score, confidence, "repository_intelligence.find_symbol",
            resolution=resolution, limitations=limitations, extra_reasons=reasons,
        )

    def _file_item(
        self, snapshot: SourceSnapshot, reason: str, score: int,
        confidence: ContextConfidence,
    ) -> ContextItem:
        lines = snapshot.text.splitlines()
        return self._range_item(
            snapshot, 1, max(1, min(len(lines), self.limits.max_snippet_lines)),
            ContextKind.FILE, None, reason, score, confidence,
            "repository_index.read_source",
            limitations=(("File context is clipped to configured line/character bounds.",)
                         if len(lines) > self.limits.max_snippet_lines else ()),
        )

    def _line_item(
        self, snapshot: SourceSnapshot, line: int, kind: ContextKind, symbol: str | None,
        reason: str, score: int, source: str, confidence: ContextConfidence,
        resolution: str, limitations: tuple[str, ...],
    ) -> ContextItem:
        lines = snapshot.text.splitlines()
        start = max(1, line - self.limits.surrounding_lines)
        end = min(len(lines) or 1, line + self.limits.surrounding_lines)
        return self._range_item(
            snapshot, start, end, kind, symbol, reason, score, confidence, source,
            resolution=resolution, limitations=limitations,
        )

    def _range_item(
        self, snapshot: SourceSnapshot, start: int, end: int, kind: ContextKind,
        symbol: str | None, reason: str, score: int, confidence: ContextConfidence,
        source: str, *, resolution: str = "static_source_range",
        limitations: tuple[str, ...] = (), extra_reasons: tuple[str, ...] = (),
    ) -> ContextItem:
        lines = snapshot.text.splitlines()
        start = max(1, min(start, max(len(lines), 1)))
        end = max(start, min(end, max(len(lines), 1)))
        if end - start + 1 > self.limits.max_snippet_lines:
            end = start + self.limits.max_snippet_lines - 1
        elif end - start + 1 < self.limits.max_snippet_lines:
            start = max(1, min(start, end - self.limits.max_snippet_lines + 1))
        raw = "\n".join(lines[start - 1:end])
        clipped = raw[:self.limits.max_snippet_characters]
        clipping = ("Source range clipped to configured context limits.",) if clipped != raw else ()
        all_reasons = tuple(sorted(set((reason, *extra_reasons))))
        return self._item(
            kind, snapshot.path, symbol, start, min(end, start + max(len(clipped.splitlines()) - 1, 0)),
            clipped, score, all_reasons, source, resolution, confidence,
            limitations=tuple(sorted(set((*limitations, *clipping)))),
        )

    @staticmethod
    def _item(
        kind: ContextKind, path: str | None, symbol: str | None,
        start_line: int | None, end_line: int | None, content: str, score: int,
        reasons: tuple[str, ...], source: str, resolution: str,
        confidence: ContextConfidence, *, limitations: tuple[str, ...] = (),
    ) -> ContextItem:
        return ContextItem(
            kind, path, symbol, start_line, end_line, content, score,
            tuple(sorted(set(reasons))), source, resolution, confidence,
            len(content), limitations,
        )

    @staticmethod
    def _with_reason(item: ContextItem, reason: str, score: int) -> ContextItem:
        return ContextItem(
            item.kind, item.path, item.symbol, item.start_line, item.end_line, item.content,
            item.relevance_score + score, tuple(sorted(set((*item.reasons, reason)))),
            item.source, item.resolution, item.confidence, item.estimated_cost, item.limitations,
        )

    @staticmethod
    def _deduplicate(items: list[ContextItem]) -> list[ContextItem]:
        unique: dict[tuple[Any, ...], ContextItem] = {}
        for item in items:
            key = (item.kind, item.path, item.symbol, item.start_line, item.end_line, item.content)
            old = unique.get(key)
            if old is None:
                unique[key] = item
                continue
            unique[key] = ContextItem(
                old.kind, old.path, old.symbol, old.start_line, old.end_line, old.content,
                max(old.relevance_score, item.relevance_score),
                tuple(sorted(set((*old.reasons, *item.reasons)))),
                min(old.source, item.source),
                old.resolution if old.resolution == item.resolution else "mixed_static_evidence",
                max((old.confidence, item.confidence), key=lambda value: list(ContextConfidence).index(value)),
                old.estimated_cost,
                tuple(sorted(set((*old.limitations, *item.limitations)))),
            )
        return list(unique.values())

    @staticmethod
    def _sort_key(item: ContextItem) -> tuple[Any, ...]:
        return (
            -item.relevance_score,
            _KIND_PRIORITY[item.kind],
            item.path or "",
            item.start_line or 0,
            item.symbol or "",
            item.source,
        )

    def _check_limits(
        self, started: float, cancellation: threading.Event | None,
    ) -> None:
        if cancellation and cancellation.is_set():
            raise InterruptedError("Context generation cancelled")
        if time.monotonic() - started > self.limits.max_build_seconds:
            raise _ContextDeadline

    @staticmethod
    def _clip_item(item: ContextItem, cost: int) -> ContextItem | None:
        if cost < 1:
            return None
        content = item.content[:cost]
        return ContextItem(
            item.kind, item.path, item.symbol, item.start_line, item.end_line,
            content, item.relevance_score, item.reasons, item.source, item.resolution,
            item.confidence, len(content),
            tuple(sorted(set((*item.limitations, "Item clipped to remaining context budget.")))),
        )

    @staticmethod
    def _mark_truncated(package: ContextPackage, reason: str) -> ContextPackage:
        return ContextPackage(
            package.task, package.items, package.budget, package.reserve,
            package.used_budget, package.remaining_budget, True,
            tuple(sorted(set((*package.truncation_reasons, reason)))),
            package.limitations, package.expansion_candidates,
        )


_KIND_PRIORITY = {
    ContextKind.TASK: 0,
    ContextKind.SYMBOL: 1,
    ContextKind.FILE: 2,
    ContextKind.DIFF: 3,
    ContextKind.TEST: 4,
    ContextKind.DEPENDENCY: 5,
    ContextKind.DIAGNOSTIC: 6,
    ContextKind.SNIPPET: 7,
    ContextKind.METADATA: 8,
}


def render_context(package: ContextPackage, *, max_characters: int = 48_000) -> str:
    if type(max_characters) is not int or max_characters < 1:
        raise ValueError("Renderer character limit must be a positive integer")
    blocks: list[str] = []
    for item in package.items:
        location = item.path or "task"
        if item.start_line is not None:
            location += f":{item.start_line}-{item.end_line}"
        title = f"[{item.kind.value}] {location}"
        if item.symbol:
            title += f" :: {item.symbol}"
        block = (
            f"{title}\n"
            f"reasons={','.join(item.reasons)} confidence={item.confidence.value} "
            f"resolution={item.resolution} cost={item.estimated_cost}\n"
            f"{item.content}"
        )
        if item.limitations:
            block += f"\nlimitations={'; '.join(item.limitations)}"
        blocks.append(block)
    rendered = "\n\n".join(blocks)
    if len(rendered) <= max_characters:
        return rendered
    return rendered[:max_characters]


def _safe_relative_path(path: str) -> bool:
    return (
        isinstance(path, str) and bool(path) and not Path(path).is_absolute()
        and "\\" not in path and "\x00" not in path and ".." not in Path(path).parts
    )


def _filename_mentions(task: str) -> tuple[str, ...]:
    return parse_task(task).filenames
