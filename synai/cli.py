from __future__ import annotations

import argparse
from dataclasses import replace
from importlib.resources import files
from typing import Sequence

from synai import __version__


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="SynAI: Ollama coding agent with approved sandbox or opt-in host tools")
    parser.add_argument("--version", action="version", version=f"SynAI {__version__}")
    parser.add_argument("--print-editor-image-recipe", action="store_true",
                        help="Print the bundled editor sandbox Dockerfile and exit without building anything")
    parser.add_argument("--ollama-url")
    parser.add_argument("--history-dir", help=argparse.SUPPRESS)
    parser.add_argument("--runtime", choices=["docker", "podman"])
    parser.add_argument("--image")
    parser.add_argument("--request-timeout", type=float)
    parser.add_argument("--command-timeout", type=float)
    parser.add_argument("--output-bytes", type=int)
    parser.add_argument("--tool-budget", type=int)
    args = parser.parse_args(argv)
    if args.print_editor_image_recipe:
        print(files("synai.editor").joinpath("sandbox-editor.Dockerfile").read_text(encoding="utf-8"), end="")
        return
    del args.print_editor_image_recipe
    if args.history_dir is not None:
        parser.error("--history-dir is retired. Conversation storage is fixed at ~/.synai.")
    del args.history_dir
    from synai.config import Settings
    from synai.preferences import PreferencesStore, resolve_connection
    from synai.tui.application import CodingApp

    try:
        settings = replace(
            Settings(ollama_url="http://localhost:11434", request_timeout=1200),
            **{key: value for key, value in vars(args).items() if value is not None},
        )
        saved = PreferencesStore(settings.history_dir).load()
        settings, sources = resolve_connection(
            settings, saved, cli_url=args.ollama_url, cli_timeout=args.request_timeout,
        )
        settings.validate()
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    try:
        application = CodingApp(settings, preferences=saved, connection_sources=sources)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    application.run()


if __name__ == "__main__":
    main()
