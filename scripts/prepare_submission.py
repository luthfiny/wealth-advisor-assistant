"""Export a source snapshot and ZIP without Git history, credentials or runtime data."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tomllib
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {".git", ".venv", ".venv-mcp", "data", "output", "build", "dist", "__pycache__"}
ALLOWED = {
    ".github",
    ".gitignore",
    "LICENSE",
    "COPYRIGHT",
    "README.md",
    "pyproject.toml",
    "config",
    "docs",
    "plugins",
    "samples",
    "scripts",
    "tests",
    "wealth_advisor",
}


def main():
    paths = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
        )
        .decode("utf-8")
        .split("\0")
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    destination = ROOT / "output" / ("submission-" + stamp)
    snapshot = destination / "wealth-advisor-assistant"
    snapshot.mkdir(parents=True, exist_ok=False)
    files = []
    for name in sorted(set(paths) - {""}):
        relative = Path(name)
        if (
            relative.is_absolute()
            or relative.parts[0] not in ALLOWED
            or ".." in relative.parts
            or EXCLUDED.intersection(relative.parts)
            or any(part.startswith(".env") or part.endswith(".egg-info") for part in relative.parts)
        ):
            raise ValueError("Unexpected private or generated path in source selection")
        source = (ROOT / relative).resolve()
        if not source.is_relative_to(ROOT) or not source.is_file():
            raise ValueError("Source file is missing or resolves outside the checkout")
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        files.append(
            {"path": relative.as_posix(), "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
        )
    archive = destination / "wealth-advisor-assistant.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for item in files:
            bundle.write(snapshot / item["path"], "wealth-advisor-assistant/" + item["path"])
    project = tomllib.loads((snapshot / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    manifest = {
        "license": "PolyForm Free Trial License 1.0.0",
        "version": project["version"],
        "snapshot": str(snapshot),
        "archive": str(archive),
        "files": files,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "includes_git_history": False,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {k: v for k, v in manifest.items() if k != "files"} | {"file_count": len(files)},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
