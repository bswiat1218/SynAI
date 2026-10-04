# Releasing SynAI

SynAI 0.3.0 is a Linux application requiring Python 3.12+. The wheel contains
Python code, TCSS, Lua configuration, and an editor sandbox image recipe. It
does not contain GTK/VTE, Neovim, mini.nvim, Ollama, or container runtimes.

## Prepare and verify locally

Work as a non-root Linux user. Use a clean checkout and a dedicated virtual
environment. Do not add generated data, credentials, personal prompts, or
conversations to source control.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[release]'
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m build
.venv/bin/python -m twine check --strict dist/*.whl dist/*.tar.gz
.venv/bin/python scripts/verify_release.py --tag v0.3.0 --run-tests
```

The build uses isolated Setuptools dependencies. Use a fresh `dist/` for each
version: verification requires exactly one wheel and source archive and will
reject unrelated files rather than deleting them. Move old artifacts aside
deliberately before building another release.

The verifier:

- Inspects metadata, license files, console entry point, namespace boundaries,
  bundled resources, source support files, and forbidden archive paths.
- Rebuilds a wheel from the actual source archive and compares runtime contents.
- Installs both wheels into clean temporary virtual environments outside the
  checkout, resolving runtime dependencies normally.
- Checks CLI output, no storage writes on early exits, generic import decoys,
  installed Textual resources, and real isolated host-helper file operations.
- With `--run-tests`, executes the complete normal unittest suite from the
  extracted source archive against the installed rebuilt wheel.
- Writes `dist/SHA256SUMS` only after all verification passes.

All temporary verification environments are removed. The checked distributions
and checksum file remain in `dist/`. Build and verification require access to
the configured Python package index; they do not publish anything.

The release version is defined once in `synai/__init__.py`. Update it before
building and require exact equality with the `vX.Y.Z` release tag. Review the
wheel/source contents, checksums, license identity, dependency bounds, and
README before publishing. Build again after any changes to included files.

For Python 3.13/3.14 compatibility checks, repeat clean installation and the
normal suite with those interpreters; the CI matrix does this against the
source-rebuilt installed wheel. Passing the CLI/resource smoke alone does not
replace a full matrix pass.

## Optional editor integration checks

Normal tests do not silently install editor dependencies, open desktop windows,
or create containers. After explicitly installing the pinned plugin, opt in:

```sh
SYNAI_TEST_MINI_PATH="$HOME/.local/share/synai/mini.nvim" \
  .venv/bin/python -m unittest discover -s tests -p 'test_editor*.py'

SYNAI_TEST_DESKTOP=1 SYNAI_TEST_MINI_PATH="$HOME/.local/share/synai/mini.nvim" \
  xvfb-run -a /usr/bin/python3 -m unittest discover -s tests -p 'test_editor_desktop.py'
```

Set `SYNAI_TEST_WINDOW_MANAGER=/usr/bin/xfwm4` to check actual maximization under
Xvfb. Only after approving test container creation, set
`SYNAI_TEST_EDITOR_IMAGE` to a locally built editor image. Those tests remove
only the containers they create.

To validate the installed GTK entry point, install the wheel into a temporary
application venv and run the editor child tests from the source archive outside
the checkout, with its test directory on `PYTHONPATH`. The application venv
does not need PyGObject: the child can use the GTK-capable system Python.

## GitHub setup

This checkout initially has no commits or remote. Repository creation,
committing, tagging, and pushing are explicit maintainer actions, not performed
by local build or verification.

1. Create/configure the real GitHub repository and push reviewed source and
   workflows. Add real repository/documentation URLs to package metadata when
   known; do not publish placeholder URLs.
2. Enable Actions. `ci.yml` tests clean installed distributions on Linux Python
   3.12, 3.13, and 3.14. All supported versions must pass before release. Local
   validation on one version is not evidence that the others passed.
3. Protect the release branch and release tags as appropriate.
4. Create the **`pypi`** GitHub environment with **required reviewers**, and
   restrict permitted deployment refs. Configure protection in GitHub settings:
   the YAML `environment: pypi` line alone does not require human approval.
5. Confirm that your repository plan supports the required environment
   protection features. If it does not, do not dispatch this publishing
   workflow until an equivalent approval gate is established.

Reusable actions are pinned to immutable commit SHAs. Review upstream changes
and update those pins deliberately; a version comment is descriptive, not a
floating dependency.

## PyPI Trusted Publishing

Check whether the `synai` PyPI project name can actually be registered. A 404
from the JSON API does not guarantee availability. If the name is unavailable,
choose an available distribution name and update packaging, tests, verifier,
workflow filenames/checks, and installation instructions before building.
The Python import namespace and CLI need not change solely because of an index
name conflict.

On PyPI, configure a pending Trusted Publisher for a new project, or a Trusted
Publisher on an existing project you control, with:

- Project name: `synai` (only if available).
- Actual GitHub owner and repository.
- Workflow filename: `publish.yml`.
- Environment name: `pypi`.

Do not put a PyPI API token in the repository or workflow. The publishing job
alone has `id-token: write`; PR tests and build jobs cannot publish. PyPI and
GitHub publisher/environment setup require maintainer access and cannot be
validated solely by building locally.

## Manually publish

After all CI jobs pass and configuration is complete:

1. Commit/review the final release source and create the matching version tag,
   for example `v0.3.0`, following your repository's policies.
2. Push that exact tag and confirm the commit to be released.
3. Dispatch **Manually publish a verified release** from the trusted default
   branch, providing the existing tag.
4. The build job checks the tag, builds and verifies the distribution, and runs
   the installed test suite without publishing credentials.
5. A reviewer inspects the run, tag commit, and uploaded artifacts, then approves
   the protected `pypi` environment.
6. The publishing job downloads that exact run's artifact ID, checks SHA-256
   sums, selects only the wheel/source pair, and publishes via OIDC.

No publish-on-push, tag-triggered publish, or PR-triggered publish is configured.
Treat workflow files and release tags as trusted maintainer-controlled code.
Do not approve a release built from unreviewed code.

PyPI versions cannot normally be overwritten. If publication partially succeeds,
inspect PyPI and the run before retrying; do not assume retries are idempotent.
Make a new version for corrected artifacts rather than rebuilding a published
version with different contents. Remote dispatch and actual upload must be
verified after the repository/publisher are configured.

## Installation and data retention

Before publication, users can install the verified local wheel:

```sh
pipx install --python python3.12 ./dist/synai-0.3.0-py3-none-any.whl
synai --version
synai
```

After publication, use `pipx install --python python3.12 synai==0.3.0`.
Package upgrades/uninstalls do not delete `~/.synai` conversations or settings.
No automated reset or source-history migration is part of distribution.
