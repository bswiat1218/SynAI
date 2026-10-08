"""Explicitly download pinned editor assets for maintainers; never called at launch."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import urllib.request
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-directory", type=Path, help="Use already downloaded assets offline")
    args = parser.parse_args()
    destination = Path(__file__).resolve().parents[1] / "synai/editor/vendor"
    data = json.loads((destination / "runtime.json").read_text())
    with tempfile.TemporaryDirectory(prefix="synai-assets-") as temporary:
        staging = Path(temporary)
        for asset in data["assets"]:
            path = staging / asset["name"]
            if args.from_directory:
                shutil.copyfile(args.from_directory / asset["name"], path)
            else:
                print(f"Downloading {asset['url']}", flush=True)
                with urllib.request.urlopen(asset["url"], timeout=60) as source, path.open("wb") as output:
                    remaining = asset["size"]
                    while remaining:
                        chunk = source.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            raise ValueError(f"Truncated download: {asset['name']}")
                        remaining -= len(chunk)
                        output.write(chunk)
                    if source.read(1):
                        raise ValueError(f"Oversized download: {asset['name']}")
            if path.stat().st_size != asset["size"] or hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
                raise ValueError(f"Checksum or size mismatch: {asset['name']}")
        for asset in data["assets"]:
            shutil.copyfile(staging / asset["name"], destination / asset["name"])
    print("Pinned editor assets verified and prepared.")


if __name__ == "__main__":
    main()
