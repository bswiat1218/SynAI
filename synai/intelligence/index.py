from __future__ import annotations

import ast
import heapq
import hashlib
import json
import math
import os
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".cache",
    "dist", "build", "htmlcov", "coverage", "site-packages",
})
_TEXT_SUFFIXES = frozenset({
    ".py", ".pyi", ".md", ".rst", ".txt", ".toml", ".ini", ".cfg", ".yaml", ".yml",
    ".json", ".xml", ".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".sh", ".sql",
    ".go", ".rs", ".java", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".lua",
})
_TEXT_FILENAMES = frozenset({
    "Dockerfile", "Makefile", "GNUmakefile", "Justfile", "Procfile", ".gitignore",
    ".dockerignore", ".editorconfig", ".coveragerc",
})
_LANGUAGES = {
    ".py": "python", ".pyi": "python", ".md": "markdown", ".rst": "restructuredtext",
    ".toml": "toml", ".ini": "ini", ".cfg": "ini", ".yaml": "yaml", ".yml": "yaml",
    ".json": "json", ".xml": "xml", ".html": "html", ".css": "css", ".js": "javascript",
    ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript", ".sh": "shell",
    ".sql": "sql", ".go": "go", ".rs": "rust", ".java": "java", ".c": "c",
    ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".rb": "ruby", ".php": "php", ".lua": "lua",
}


@dataclass(frozen=True)
class IndexLimits:
    max_files: int = 10_000
    max_directory_entries: int = 20_000
    max_file_bytes: int = 1_048_576
    max_total_bytes: int = 67_108_864
    max_depth: int = 32
    max_search_results: int = 100
    max_symbol_results: int = 500
    max_reference_results: int = 500
    max_text_per_result: int = 500
    max_diagnostic_results: int = 200
    max_scan_seconds: float = 10.0
    max_output_bytes: int = 524_288

    def __post_init__(self) -> None:
        integer_limits = (
            self.max_files, self.max_directory_entries, self.max_file_bytes,
            self.max_total_bytes, self.max_depth,
            self.max_search_results, self.max_symbol_results, self.max_reference_results,
            self.max_text_per_result, self.max_diagnostic_results, self.max_output_bytes,
        )
        if any(type(value) is not int or value < 1 for value in integer_limits):
            raise ValueError("Repository intelligence limits must be positive integers")
        if (
            isinstance(self.max_scan_seconds, bool)
            or not isinstance(self.max_scan_seconds, (int, float))
            or not math.isfinite(self.max_scan_seconds)
            or self.max_scan_seconds <= 0
        ):
            raise ValueError("Repository intelligence scan duration must be positive")


@dataclass
class _IndexedFile:
    path: str
    size: int
    mtime_ns: int
    inode: int
    digest: str
    text: str
    symbols: list[dict[str, Any]] = field(default_factory=list)
    references: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    imports: list[dict[str, Any]] = field(default_factory=list)
    bases: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class SourceSnapshot:
    path: str
    text: str
    sha256: str
    size_bytes: int


class _SyntaxIndex(ast.NodeVisitor):
    def __init__(self, path: str, module: str) -> None:
        self.path, self.module = path, module
        self.scopes: list[tuple[str, str]] = []
        self.symbols: list[dict[str, Any]] = [{
            "name": module.rsplit(".", 1)[-1],
            "qualified_name": module,
            "kind": "module",
            "path": path,
            "line": 1,
            "column": 0,
            "end_line": None,
        }]
        self.references: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.imports: list[dict[str, Any]] = []
        self.bases: list[dict[str, Any]] = []

    def _qualified(self, name: str) -> str:
        return ".".join([self.module, *(scope for scope, _ in self.scopes), name])

    def _location(self, node: ast.AST) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": getattr(node, "lineno", 1),
            "column": getattr(node, "col_offset", 0),
            "end_line": getattr(node, "end_lineno", None),
        }

    def _owner(self) -> str:
        return self._qualified("")[:-1] if self.scopes else self.module

    def _symbol(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, kind: str) -> None:
        self.symbols.append({
            "name": node.name,
            "qualified_name": self._qualified(node.name),
            "kind": kind,
            **self._location(node),
        })

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._symbol(node, "class")
        for base in node.bases:
            base_name = _expression_name(base)
            if base_name:
                self.bases.append({
                    "name": base_name.rsplit(".", 1)[-1],
                    "expression": base_name,
                    "class": self._qualified(node.name),
                    **self._location(base),
                })
            self.visit(base)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for keyword in node.keywords:
            self.visit(keyword)
        self.scopes.append((node.name, "class"))
        for item in node.body:
            self.visit(item)
        self.scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, "async_function")

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, kind: str) -> None:
        if self.scopes and self.scopes[-1][1] == "class":
            kind = "async_method" if kind == "async_function" else "method"
        self._symbol(node, kind)
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        scope_name = node.name
        self.scopes.append((scope_name, kind))
        for item in node.body:
            self.visit(item)
        self.scopes.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append({
                "kind": "import",
                "module": alias.name,
                "name": None,
                "alias": alias.asname,
                "bound_name": alias.asname or alias.name.split(".", 1)[0],
                **self._location(node),
            })

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        resolved = _resolve_relative_module(self.module, node.module, node.level, self.path)
        for alias in node.names:
            self.imports.append({
                "kind": "from_import",
                "module": resolved,
                "name": alias.name,
                "alias": alias.asname,
                "bound_name": alias.asname or alias.name,
                "level": node.level,
                **self._location(node),
            })

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.references.append({
                "name": node.id, "expression": node.id, "scope": self._owner(),
                "reference_kind": "name", **self._location(node),
            })

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            expression = _expression_name(node)
            if expression:
                self.references.append({
                    "name": node.attr, "expression": expression, "scope": self._owner(),
                    "reference_kind": "attribute", **self._location(node),
                })
        self.visit(node.value)

    def visit_Call(self, node: ast.Call) -> None:
        target = _expression_name(node.func)
        if target:
            self.calls.append({
                "name": target.rsplit(".", 1)[-1],
                "expression": target,
                "scope": self._owner(),
                "resolution": "syntactic_name_match",
                **self._location(node),
            })
        self.generic_visit(node)


def _expression_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _expression_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def _module_name(path: str) -> str:
    parts = list(Path(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or "__init__"


def _resolve_relative_module(
    current_module: str, target_module: str | None, level: int, path: str,
) -> str | None:
    if level == 0:
        return target_module
    package_parts = current_module.split(".") if Path(path).name == "__init__.py" else current_module.split(".")[:-1]
    remove_count = level - 1
    if remove_count > len(package_parts):
        return None
    parts = package_parts[:len(package_parts) - remove_count]
    if target_module:
        parts.extend(target_module.split("."))
    return ".".join(parts) or None


class RepositoryIndex:
    """A bounded, in-memory index rooted at one already-validated workspace."""

    def __init__(self, workspace: Path, limits: IndexLimits | None = None) -> None:
        self.root = workspace.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Repository intelligence requires an existing workspace directory")
        self.limits = limits or IndexLimits()
        self._files: dict[str, _IndexedFile] = {}
        self._directories: set[str] = set()
        self._diagnostics: list[dict[str, Any]] = []
        self._truncation_reasons: list[str] = []
        self._indexed_bytes = 0
        self._scan_complete = True
        self._lock = threading.Lock()

    def query(
        self, operation: str, arguments: dict[str, str],
        cancellation: threading.Event | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._refresh(cancellation)
            if cancellation and cancellation.is_set():
                raise InterruptedError("Repository intelligence query cancelled")
            handlers = {
                "get_project_structure": self._project_structure,
                "find_symbol": self._find_symbol,
                "find_definition": self._find_definition,
                "find_references": self._find_references,
                "find_callers": self._find_callers,
                "find_implementations": self._find_implementations,
                "find_imports": self._find_imports,
                "find_tests": self._find_tests,
                "search_code": self._search_code,
                "get_diagnostics": self._get_diagnostics,
            }
            handler = handlers.get(operation)
            if handler is None:
                raise ValueError(f"Unknown repository intelligence operation: {operation}")
            return self._response(operation, *handler(arguments))

    def read_source(
        self, path: str, cancellation: threading.Event | None = None,
    ) -> SourceSnapshot | None:
        """Read a bounded file already admitted by this index, without accepting host paths."""
        return self.read_sources((path,), cancellation)[path]

    def read_file_bytes(
        self,
        path: str,
        *,
        max_bytes: int | None = None,
        cancellation: threading.Event | None = None,
    ) -> bytes | None:
        """Read one bounded regular file using the index's no-follow, identity-checked reader."""
        maximum = self.limits.max_file_bytes if max_bytes is None else max_bytes
        if (
            not isinstance(path, str) or not path or "\\" in path or "\x00" in path
            or Path(path).is_absolute() or ".." in Path(path).parts
            or type(maximum) is not int or not 1 <= maximum <= self.limits.max_file_bytes
        ):
            raise ValueError("Source snapshot path or byte limit is invalid")
        if cancellation and cancellation.is_set():
            raise InterruptedError("Source snapshot capture cancelled")
        components = Path(path).parts
        parent = "." if len(components) == 1 else str(Path(*components[:-1]))
        try:
            directory_fd = _open_workspace_directory(self.root, parent)
        except FileNotFoundError:
            return None
        try:
            try:
                expected_info = os.stat(
                    components[-1], dir_fd=directory_fd, follow_symlinks=False,
                )
            except FileNotFoundError:
                return None
            if not stat.S_ISREG(expected_info.st_mode):
                raise OSError("Source snapshot target is not a regular file")
            if expected_info.st_size > maximum:
                raise OSError("Source snapshot target exceeds the configured byte limit")
        finally:
            os.close(directory_fd)
        data = _read_workspace_file(
            self.root, path, maximum, expected_info=expected_info,
        )
        if data is None:
            raise OSError("Source snapshot target is not a safe regular file")
        if len(data) > maximum:
            raise OSError("Source snapshot target exceeds the configured byte limit")
        if cancellation and cancellation.is_set():
            raise InterruptedError("Source snapshot capture cancelled")
        return data

    def read_sources(
        self,
        paths: tuple[str, ...],
        cancellation: threading.Event | None = None,
    ) -> dict[str, SourceSnapshot | None]:
        """Read bounded indexed files from one refreshed workspace snapshot."""
        if (
            not isinstance(paths, tuple) or not 1 <= len(paths) <= 128
            or any(
                not isinstance(path, str) or not path or "\\" in path or "\x00" in path
                or Path(path).is_absolute() or ".." in Path(path).parts
                for path in paths
            )
            or len(set(paths)) != len(paths)
        ):
            raise ValueError("Source paths must be a bounded set of safe workspace-relative paths")
        with self._lock:
            self._refresh(cancellation, refresh_paths=frozenset(paths))
            if cancellation and cancellation.is_set():
                raise InterruptedError("Repository intelligence query cancelled")
            result: dict[str, SourceSnapshot | None] = {}
            for path in paths:
                item = self._files.get(path)
                result[path] = (
                    SourceSnapshot(item.path, item.text, item.digest, item.size)
                    if item is not None else None
                )
            return result

    def read_symbol_source(
        self, qualified_name: str, cancellation: threading.Event | None = None,
    ) -> list[tuple[dict[str, Any], SourceSnapshot]]:
        """Return current exact definitions and their source from one refreshed index view."""
        if not isinstance(qualified_name, str) or not qualified_name or len(qualified_name) > 512:
            raise ValueError("Symbol name must be bounded non-empty text")
        with self._lock:
            self._refresh(cancellation)
            if cancellation and cancellation.is_set():
                raise InterruptedError("Repository intelligence query cancelled")
            matches: list[tuple[dict[str, Any], SourceSnapshot]] = []
            for item in sorted(self._files.values(), key=lambda indexed: indexed.path):
                for symbol in item.symbols:
                    if symbol["qualified_name"] == qualified_name:
                        matches.append((
                            dict(symbol),
                            SourceSnapshot(item.path, item.text, item.digest, item.size),
                        ))
            return matches

    def imports_for_file(
        self, path: str, cancellation: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        """Return imports already indexed for one safe relative source path."""
        snapshot = self.read_source(path, cancellation)
        if snapshot is None:
            return []
        with self._lock:
            item = self._files.get(path)
            return [dict(record) for record in item.imports] if item else []

    def _refresh(
        self,
        cancellation: threading.Event | None,
        *,
        refresh_paths: frozenset[str] = frozenset(),
    ) -> None:
        started = time.monotonic()
        old_files = self._files
        current: dict[str, _IndexedFile] = {}
        directories: set[str] = set()
        diagnostics: list[dict[str, Any]] = []
        reasons: list[str] = []
        indexed_bytes = 0
        scanned_bytes = 0
        processed = 0
        visited_directories = 0
        complete = True
        queue: list[tuple[str, int]] = [(".", 0)]

        while queue:
            if cancellation and cancellation.is_set():
                raise InterruptedError("Repository intelligence query cancelled")
            visited_directories += 1
            if visited_directories > self.limits.max_files:
                reasons.append("maximum_indexed_paths")
                complete = False
                break
            if time.monotonic() - started > self.limits.max_scan_seconds:
                reasons.append("maximum_scan_duration")
                complete = False
                break
            relative_dir, depth = queue.pop()
            try:
                children, entries_truncated, scan_timed_out = _workspace_entries(
                    self.root, relative_dir, self.limits.max_directory_entries,
                    started + self.limits.max_scan_seconds, cancellation,
                )
            except OSError as exc:
                diagnostics.append(self._diagnostic(relative_dir, "unreadable_directory", str(exc)))
                continue
            if entries_truncated:
                reasons.append("maximum_directory_entries")
                complete = False
            if scan_timed_out:
                reasons.append("maximum_scan_duration")
                complete = False
            subdirectories: list[tuple[str, int]] = []
            for name, entry_kind, info, error in children:
                if time.monotonic() - started > self.limits.max_scan_seconds:
                    reasons.append("maximum_scan_duration")
                    complete = False
                    break
                if name in EXCLUDED_DIRECTORIES:
                    continue
                relative = name if relative_dir == "." else f"{relative_dir}/{name}"
                if error is not None:
                    diagnostics.append(self._diagnostic(relative, "unreadable_file", error))
                    continue
                if entry_kind == "directory":
                    directories.add(relative)
                    if depth >= self.limits.max_depth:
                        reasons.append("maximum_directory_depth")
                        complete = False
                    else:
                        subdirectories.append((relative, depth + 1))
                    continue
                if entry_kind != "file" or info is None:
                    continue

                if not _is_text_file(name):
                    continue
                processed += 1
                if processed > self.limits.max_files:
                    reasons.append("maximum_indexed_files")
                    complete = False
                    break
                if info.st_size > self.limits.max_file_bytes:
                    diagnostics.append(self._diagnostic(
                        relative, "file_size_limit", "File exceeds maximum indexed file size",
                    ))
                    continue
                if scanned_bytes + info.st_size > self.limits.max_total_bytes:
                    reasons.append("maximum_total_indexed_bytes")
                    complete = False
                    break
                cached = old_files.get(relative)
                if cached and (
                    cached.size == info.st_size
                    and cached.mtime_ns == info.st_mtime_ns
                    and cached.inode == info.st_ino
                ) and relative not in refresh_paths:
                    current[relative] = cached
                    indexed_bytes += cached.size
                    scanned_bytes += cached.size
                    diagnostics.extend(cached.diagnostics)
                    continue
                try:
                    data = _read_workspace_file(
                        self.root,
                        relative,
                        self.limits.max_file_bytes,
                        expected_info=info,
                    )
                except OSError as exc:
                    diagnostics.append(self._diagnostic(relative, "unreadable_file", str(exc)))
                    continue
                if data is None:
                    diagnostics.append(self._diagnostic(
                        relative, "invalid_file", "File is not a regular, non-symlink workspace file",
                    ))
                    continue
                if len(data) > self.limits.max_file_bytes:
                    diagnostics.append(self._diagnostic(
                        relative, "file_size_limit", "File exceeds maximum indexed file size",
                    ))
                    continue
                if scanned_bytes + len(data) > self.limits.max_total_bytes:
                    reasons.append("maximum_total_indexed_bytes")
                    complete = False
                    break
                scanned_bytes += len(data)
                if b"\x00" in data:
                    continue
                try:
                    source = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    diagnostics.append(self._diagnostic(relative, "unreadable_file", f"Invalid UTF-8: {exc}"))
                    continue
                indexed_bytes += len(data)
                digest = hashlib.sha256(data).hexdigest()
                if cached and cached.digest == digest:
                    current[relative] = cached
                    diagnostics.extend(cached.diagnostics)
                else:
                    current[relative] = self._parse_file(relative, source, len(data), info, digest, diagnostics)
                    current[relative].diagnostics = [
                        item for item in diagnostics if item["path"] == relative
                    ]
            else:
                queue.extend(reversed(subdirectories))
                continue
            break

        self._files = current
        self._directories = directories
        self._diagnostics = diagnostics[:self.limits.max_diagnostic_results]
        self._truncation_reasons = sorted(set(reasons))
        if len(diagnostics) > self.limits.max_diagnostic_results:
            self._truncation_reasons = sorted(set((*self._truncation_reasons, "maximum_diagnostic_results")))
        self._indexed_bytes = indexed_bytes
        self._scan_complete = complete

    def _parse_file(
        self, path: str, source: str, size: int, info: os.stat_result, digest: str,
        diagnostics: list[dict[str, Any]],
    ) -> _IndexedFile:
        item = _IndexedFile(
            path, size, info.st_mtime_ns, info.st_ino, digest, source,
        )
        if not path.endswith((".py", ".pyi")):
            return item
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as exc:
            item.symbols = [{
                "name": _module_name(path).rsplit(".", 1)[-1],
                "qualified_name": _module_name(path),
                "kind": "module",
                "path": path,
                "line": 1,
                "column": 0,
                "end_line": None,
            }]
            diagnostics.append(self._diagnostic(
                path, "python_syntax_error", exc.msg, line=exc.lineno, column=exc.offset,
            ))
            return item
        visitor = _SyntaxIndex(path, _module_name(path))
        visitor.visit(tree)
        item.symbols = visitor.symbols
        item.references = visitor.references
        item.calls = visitor.calls
        item.imports = visitor.imports
        item.bases = visitor.bases
        return item

    @staticmethod
    def _diagnostic(
        path: str, kind: str, message: str, *, line: int | None = None,
        column: int | None = None,
    ) -> dict[str, Any]:
        return {
            "kind": kind, "path": path, "message": message[:500],
            "line": line, "column": column,
        }

    def _symbols(self) -> list[dict[str, Any]]:
        return sorted(
            (symbol for item in self._files.values() for symbol in item.symbols),
            key=lambda symbol: (symbol["path"], symbol["line"], symbol["column"], symbol["qualified_name"]),
        )

    def _project_structure(self, _: dict[str, str]) -> tuple[list[dict[str, Any]], list[str], int]:
        records = [
            {"path": path, "kind": "directory"} for path in sorted(self._directories)
        ] + [
            {
                "path": path, "kind": "file", "language": _language(path),
                "size_bytes": item.size,
            }
            for path, item in sorted(self._files.items())
        ]
        records.sort(key=lambda record: (record["path"], record["kind"]))
        return records, [], self.limits.max_search_results

    def _find_symbol(self, arguments: dict[str, str]) -> tuple[list[dict[str, Any]], list[str], int]:
        query = arguments["name"]
        matches = [symbol for symbol in self._symbols() if symbol["name"] == query or symbol["qualified_name"] == query]
        return matches, [], self.limits.max_symbol_results

    def _find_definition(self, arguments: dict[str, str]) -> tuple[list[dict[str, Any]], list[str], int]:
        records, limitations, limit = self._find_symbol(arguments)
        return records, limitations, limit

    def _find_references(self, arguments: dict[str, str]) -> tuple[list[dict[str, Any]], list[str], int]:
        query = arguments["name"]
        name = query.rsplit(".", 1)[-1]
        records = [
            reference
            for item in self._files.values()
            for reference in item.references
            if reference["name"] == name and (
                "." not in query or reference["expression"] == query
            )
        ]
        return sorted(records, key=_location_key), [
            "References are lexical AST name/attribute matches; Python binding and dynamic dispatch are not inferred.",
        ], self.limits.max_reference_results

    def _find_callers(self, arguments: dict[str, str]) -> tuple[list[dict[str, Any]], list[str], int]:
        query = arguments["name"]
        name = query.rsplit(".", 1)[-1]
        records = [
            call
            for item in self._files.values()
            for call in item.calls
            if call["name"] == name and ("." not in query or call["expression"] == query)
        ]
        return sorted(records, key=_location_key), [
            "Callers are syntactic call-expression matches, not binding-aware Python call resolution.",
        ], self.limits.max_reference_results

    def _find_implementations(self, arguments: dict[str, str]) -> tuple[list[dict[str, Any]], list[str], int]:
        query = arguments["name"]
        classes = [
            symbol for symbol in self._symbols()
            if symbol["kind"] == "class"
            and (symbol["name"] == query or symbol["qualified_name"] == query)
        ]
        if len(classes) != 1:
            return [], [
                "Implementation lookup requires one unambiguous class definition.",
                "Only statically identifiable class inheritance is considered.",
            ], self.limits.max_symbol_results
        target = classes[0]
        all_classes = [symbol for symbol in self._symbols() if symbol["kind"] == "class"]
        names: dict[str, list[dict[str, Any]]] = {}
        for symbol in all_classes:
            names.setdefault(symbol["name"], []).append(symbol)
            names.setdefault(symbol["qualified_name"], []).append(symbol)
        records = []
        for item in self._files.values():
            for base in item.bases:
                candidates = names.get(base["expression"], [])
                if "." not in base["expression"]:
                    candidates = names.get(base["name"], [])
                if len(candidates) == 1 and candidates[0]["qualified_name"] == target["qualified_name"]:
                    records.append({
                        "name": base["class"].rsplit(".", 1)[-1],
                        "qualified_name": base["class"],
                        "kind": "class",
                        "path": base["path"],
                        "line": base["line"],
                        "column": base["column"],
                        "end_line": next((
                            symbol["end_line"] for symbol in all_classes
                            if symbol["qualified_name"] == base["class"]
                        ), None),
                        "relationship": "direct_subclass",
                        "base": target["qualified_name"],
                    })
        return sorted(records, key=_location_key), [
            "Only direct class inheritance with a unique static base-name resolution is reported.",
        ], self.limits.max_symbol_results

    def _find_imports(self, arguments: dict[str, str]) -> tuple[list[dict[str, Any]], list[str], int]:
        query = arguments["query"]
        records = [
            imported
            for item in self._files.values()
            for imported in item.imports
            if query in {imported.get("module"), imported.get("name"), imported.get("alias"), imported["bound_name"]}
        ]
        return sorted(records, key=_location_key), [
            "Import records reflect source syntax; runtime import resolution is not performed.",
        ], self.limits.max_reference_results

    def _find_tests(self, _: dict[str, str]) -> tuple[list[dict[str, Any]], list[str], int]:
        records: list[dict[str, Any]] = []
        for item in self._files.values():
            path = Path(item.path)
            in_tests_dir = "tests" in path.parts
            conventional_file = path.name.startswith("test_") or path.name.endswith("_test.py")
            for symbol in item.symbols:
                test_symbol = (
                    symbol["kind"] in {"function", "async_function", "method", "async_method"}
                    and symbol["name"].startswith("test_")
                ) or (symbol["kind"] == "class" and symbol["name"].startswith("Test"))
                if (conventional_file or in_tests_dir) and symbol["kind"] == "module":
                    records.append({**symbol, "test_kind": "test_file"})
                elif test_symbol:
                    records.append({**symbol, "test_kind": "test_symbol"})
        return sorted(records, key=_location_key), [
            "Test discovery uses Python file and symbol naming conventions; it does not establish code coverage.",
        ], self.limits.max_symbol_results

    def _search_code(self, arguments: dict[str, str]) -> tuple[list[dict[str, Any]], list[str], int]:
        query = arguments["query"]
        if not query:
            return [], ["Search query must not be empty."], self.limits.max_search_results
        records: list[dict[str, Any]] = []
        for path, item in sorted(self._files.items()):
            for line_number, line in enumerate(item.text.splitlines(), start=1):
                column = line.find(query)
                if column < 0:
                    continue
                text_limit = self.limits.max_text_per_result
                start = max(0, column - max(0, (text_limit - len(query)) // 2))
                snippet = line[start:start + text_limit]
                records.append({
                    "path": path, "line": line_number, "column": column,
                    "snippet": snippet,
                    "truncated": start > 0 or start + len(snippet) < len(line),
                })
                if len(records) >= self.limits.max_search_results:
                    records.append({"path": path, "line": line_number, "column": column, "snippet": "", "truncated": True})
                    return records, [], self.limits.max_search_results
        return records, [], self.limits.max_search_results

    def _get_diagnostics(self, _: dict[str, str]) -> tuple[list[dict[str, Any]], list[str], int]:
        records = list(self._diagnostics)
        if self._truncation_reasons:
            records.extend({
                "kind": "index_truncated", "path": None, "message": reason,
                "line": None, "column": None,
            } for reason in self._truncation_reasons)
        if not self._scan_complete:
            records.append({
                "kind": "index_incomplete", "path": None,
                "message": "Repository index is incomplete because one or more resource limits were reached.",
                "line": None, "column": None,
            })
        return records, [], self.limits.max_diagnostic_results

    def _response(
        self, operation: str, records: list[dict[str, Any]], limitations: list[str], limit: int,
    ) -> dict[str, Any]:
        capped = records[:limit]
        truncated = len(capped) < len(records) or bool(self._truncation_reasons)
        result: dict[str, Any] = {
            "ok": True,
            "operation": operation,
            "workspace": ".",
            "results": capped,
            "result_count": len(capped),
            "truncated": truncated,
            "truncation_reasons": list(self._truncation_reasons),
            "limitations": limitations,
            "index": {
                "indexed_files": len(self._files),
                "indexed_bytes": self._indexed_bytes,
                "scan_complete": self._scan_complete,
            },
        }
        maximum = self.limits.max_output_bytes
        while len(json.dumps(result, ensure_ascii=True).encode("utf-8")) > maximum and result["results"]:
            result["results"].pop()
            result["result_count"] = len(result["results"])
            result["truncated"] = True
            if not result["truncation_reasons"]:
                result["truncation_reasons"].append("maximum_output_bytes")
        if len(json.dumps(result, ensure_ascii=True).encode("utf-8")) > maximum:
            return {
                "ok": False,
                "error": "Repository intelligence metadata exceeds configured output limit",
            }
        return result


def _is_text_file(name: str) -> bool:
    return name in _TEXT_FILENAMES or Path(name).suffix.lower() in _TEXT_SUFFIXES


def _language(path: str) -> str:
    name = Path(path).name
    if name in {"Dockerfile", "Makefile", "GNUmakefile", "Justfile"}:
        return "text"
    return _LANGUAGES.get(Path(path).suffix.lower(), "text")


def _location_key(value: dict[str, Any]) -> tuple[str, int, int, str]:
    return value.get("path") or "", value.get("line") or 0, value.get("column") or 0, value.get(
        "qualified_name", value.get("expression", value.get("name", "")),
    )


class _DescendingName:
    def __init__(self, value: str) -> None:
        self.value = value

    def __lt__(self, other: _DescendingName) -> bool:
        return self.value > other.value


def _workspace_entries(
    root: Path, relative: str, maximum: int, deadline: float,
    cancellation: threading.Event | None,
) -> tuple[list[tuple[str, str, os.stat_result | None, str | None]], bool, bool]:
    directory_fd = _open_workspace_directory(root, relative)
    try:
        entries = os.scandir(directory_fd)
    except OSError:
        os.close(directory_fd)
        raise
    selected: list[tuple[
        _DescendingName, str, str, os.stat_result | None, str | None,
    ]] = []
    count = 0
    timed_out = False
    with entries:
        for entry in entries:
            if cancellation and cancellation.is_set():
                raise InterruptedError("Repository intelligence query cancelled")
            if time.monotonic() > deadline:
                timed_out = True
                break
            count += 1
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    row = (entry.name, "directory", None, None)
                elif entry.is_file(follow_symlinks=False):
                    row = (entry.name, "file", entry.stat(follow_symlinks=False), None)
                else:
                    continue
            except OSError as exc:
                row = (entry.name, "invalid", None, str(exc))
            decorated = (_DescendingName(entry.name), *row)
            if len(selected) < maximum:
                heapq.heappush(selected, decorated)
            elif entry.name < selected[0][1]:
                heapq.heapreplace(selected, decorated)
    result = sorted((row[1:] for row in selected), key=lambda row: row[0])
    return result, count > maximum, timed_out


def _open_workspace_directory(root: Path, relative: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(root, directory_flags)
    if relative == ".":
        return directory_fd
    try:
        for component in Path(relative).parts:
            if component in {"", ".", ".."}:
                raise OSError("Invalid workspace-relative directory")
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except OSError:
        os.close(directory_fd)
        raise


def _read_workspace_file(
    root: Path,
    relative: str,
    maximum: int,
    *,
    expected_info: os.stat_result | None = None,
) -> bytes | None:
    components = Path(relative).parts
    if not components or Path(relative).is_absolute() or ".." in components:
        return None
    directory_fd: int | None = None
    file_fd: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        parent = "." if len(components) == 1 else str(Path(*components[:-1]))
        directory_fd = _open_workspace_directory(root, parent)
        file_fd = os.open(
            components[-1], flags | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd,
        )
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        if expected_info is not None and not _same_file_state(info, expected_info):
            raise OSError("Workspace file changed before safe open")
        with os.fdopen(file_fd, "rb") as handle:
            file_fd = None
            data = handle.read(maximum + 1)
            final_info = os.fstat(handle.fileno())
            current_path_info = os.stat(
                components[-1], dir_fd=directory_fd, follow_symlinks=False,
            )
            if (
                not _same_file_state(info, final_info)
                or not _same_file_state(info, current_path_info)
            ):
                raise OSError("Workspace file changed during safe read")
            return data
    except OSError:
        raise
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _same_file_state(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
    )
