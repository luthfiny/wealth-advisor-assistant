"""Build a local development marketplace wired to this checkout's optional MCP runtime."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def package(output: Path, wsl: str | None = None):
    output = output.resolve()
    target = output / "plugins/wealth-advisor"
    source = ROOT / "plugins/wealth-advisor"
    if target == source or source in target.parents:
        raise ValueError("Build output must be outside the plugin source directory")
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "plugins/wealth-advisor", target, dirs_exist_ok=True)
    shutil.copy2(ROOT / "LICENSE", target / "LICENSE")
    shutil.copy2(ROOT / "COPYRIGHT", target / "COPYRIGHT")
    runtime = ROOT / ".venv-mcp/bin/python"
    arguments = [
        str(runtime),
        "-m",
        "wealth_advisor.mcp_server",
        "--workspace",
        str(ROOT),
        "--data-dir",
        str(ROOT / "data/plugin"),
    ]
    if wsl:
        command, args = "wsl.exe", ["-d", wsl, "--cd", str(ROOT), "--", *arguments]
    else:
        command, args = str(runtime), arguments[1:]
    connection = {"type": "stdio", "command": command, "args": args}
    if not wsl:
        connection["cwd"] = str(ROOT)
    (target / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"wealth_advisor": connection}}, indent=2) + "\n",
        encoding="utf-8",
    )
    catalog = output / ".agents/plugins/marketplace.json"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        json.dumps(
            {
                "name": "wealth-advisor-local",
                "interface": {"displayName": "Wealth Advisor Local Demo"},
                "plugins": [
                    {
                        "name": "wealth-advisor",
                        "source": {"source": "local", "path": "./plugins/wealth-advisor"},
                        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                        "category": "Productivity",
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "output/plugin-marketplace")
    parser.add_argument(
        "--windows-wsl", help="Generate a Windows host launch command for this distro"
    )
    args = parser.parse_args()
    print(package(args.output, args.windows_wsl))


if __name__ == "__main__":
    main()
