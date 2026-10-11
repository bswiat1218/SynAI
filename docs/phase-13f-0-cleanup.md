# Phase 13F-0 Cleanup Acceptance Record

This record covers PR #1 on `cleanup/remove-legacy-tui-neovim`. It completes
the retired local UI/editor cleanup and does not begin Phase 13F-B.

## Removed files and packaging surfaces

The retired launcher and interface implementation, bundled editor, installer,
and interface-only tests were removed:

```text
app.py
scripts/prepare_editor_assets.py
synai/__main__.py
synai/cli.py
synai/tui/__init__.py
synai/tui/activity.py
synai/tui/application.py
synai/tui/directory_picker.py
synai/tui/menu.py
synai/tui/navigation.py
synai/tui/palette.py
synai/tui/theme.tcss
synai/tui/theme_picker.py
synai/editor/__init__.py
synai/editor/bundle.py
synai/editor/desktop.py
synai/editor/environment.py
synai/editor/init.lua
synai/editor/manager.py
synai/editor/neovim.py
synai/editor/protocol.py
synai/editor/sandbox-editor.Dockerfile
synai/editor/supervisor.py
synai/editor/theme.lua
synai/editor/vendor/__init__.py
synai/editor/vendor/mini.nvim.tar.gz
synai/editor/vendor/neovim-LICENSE.txt
synai/editor/vendor/nvim-linux-x86_64.tar.gz
synai/editor/vendor/runtime.json
tests/test_activity.py
tests/test_cli.py
tests/test_conversation_browser.py
tests/test_directory_picker.py
tests/test_editor.py
tests/test_editor_bundle.py
tests/test_editor_desktop.py
tests/test_editor_neovim.py
tests/test_editor_sandbox.py
tests/test_global_ui.py
tests/test_help_overlay.py
tests/test_keyboard_dashboard.py
tests/test_menu.py
tests/test_menu_navigation.py
tests/test_native_navigation.py
tests/test_text_readability.py
tests/test_theme_picker.py
tests/test_ui_consistency.py
```

The unsupported direct-host backend and its tests were also removed:

```text
synai/host.py
tests/test_host.py
```

The TUI-only test classes were removed from retained
`tests/test_agent.py` and `tests/test_storage.py`; shared backend and sandbox
tests remain. The TUI-specific `save_managed` helper was removed from
`tests/support.py`.

The interface-specific prompt fixture and superseded historical plan copies
were removed:

```text
prompts/CUSTOM1.txt
docs/architecture/history/Phase13A-WebApplicationMigrationPlan.md
docs/architecture/history/Phase13ArchitectureRealignment.md
docs/architecture/history/README.md
```

The former ADR `docs/architecture/adr/0007-legacy-tui-and-memory-compatibility.md`
is replaced by
`docs/architecture/adr/0007-data-and-memory-compatibility.md`.

Packaging no longer declares `textual>=1.0,<9`, the `synai` TUI console script,
or setuptools package-data patterns for `synai.tui` themes and
`synai.editor` assets. The supported scripts remain `synai-web`,
`synai-client`, and `synai-broker`.

## Retained behavior and execution boundaries

The reusable `ExecutionBackend` protocol, `Tools` dispatcher, coding-agent
policy and lifecycle logic, approvals, verification/review, checkpoints,
memory, and historical conversation/task formats remain. The local
container-backed `Sandbox` and its helper remain separate from the Core web
request path; the helper no longer has a direct-host mode. There is no shipped
`HostExecution` implementation. The standalone restricted Broker remains
independent, and Browser Chat still supplies no tools. The Linux Client Agent
retains its separate CLI, pairing, consent, and read-only snapshot behavior.

New data-root lock acquisitions are web-only. Old `tui` owner metadata remains
readable solely to identify a process holding an existing lock. Historical
host-mode task metadata remains parseable, but the package no longer provides
a direct host-execution backend.

## OpenAPI consistency

The Python 3.12-only schema mismatch came from the standard-library HTTP
reason phrases used for statuses 413 and 422: Python 3.12 emits the older
“Request Entity Too Large” and “Unprocessable Entity” phrases, while Python
3.13 and 3.14 emit “Content Too Large” and “Unprocessable Content”. FastAPI
and Pydantic versions are the same across the tested runtimes. OpenAPI
generation now canonicalizes only those known equivalent descriptions. The
complete generated-schema equality check remains intact, the checked-in
contract is unchanged, and frontend API types did not need regeneration.

## Verification

Full Python unittest discovery passed on each supported runtime:

| Python | Tests | Result |
|---|---:|---|
| 3.12.11 | 460 | Passed; 4 skipped |
| 3.13.5 | 460 | Passed; 4 skipped |
| 3.14.4 | 460 | Passed; 4 skipped |

The four skips are the opt-in disposable-container test, container-isolation
fixture, container memory-identity test requiring a configured service image,
and Ollama smoke test requiring an explicitly configured endpoint/model.

Frontend verification passed: 10 unit tests, TypeScript typecheck, production
build, 3 mocked browser tests, and 3 live browser integration tests.
`git diff --check` and local Markdown-link validation passed.

A wheel and sdist built successfully (66 wheel entries, 108 sdist entries).
Neither archive contains Textual, Neovim, TUI/editor paths, or the removed
interface tests; wheel metadata has no Textual requirement and contains only
the three supported console scripts. A fresh Python 3.12 virtual environment
installed the wheel. `synai-web` served its health endpoint, `synai-client`
reported help/version, and `synai-broker` served its health endpoint over a
Unix socket with runtime execution intentionally unavailable in the smoke
test. The obsolete `synai` script was absent.

The package build emitted non-fatal warnings that the configured `LICENSE`
and `NOTICE` patterns do not match files in the source tree. The disposable
container and Ollama tests above were not executed beyond their reported
skips; no production Podman-isolation claim is made by this record.

## Remaining references

No active source, entry point, test fixture, or documentation advertises the
removed interfaces as supported. The only remaining `tui` identifier is the
old persisted lock-owner label and its compatibility test. Architecture
documentation retains a brief historical note that the interfaces were
removed. This cleanup does not implement or authorize Phase 13F-B.
