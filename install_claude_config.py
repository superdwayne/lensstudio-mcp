#!/usr/bin/env python3
"""Auto-install the lens-studio MCP server into Claude Desktop config."""
import json
import os
import sys
from pathlib import Path


def build_lens_entry(repo_root: Path) -> dict:
    venv_python = repo_root / ".venv/bin/python"
    command = str(venv_python) if venv_python.exists() else "python3"
    return {
        "command": command,
        "args": [
            str(repo_root / "server.py"),
            "--transport", "stdio",
        ],
        "env": {
            "PYTHONUNBUFFERED": "1",
        },
    }


def main():
    repo_root = Path(__file__).resolve().parent
    target = Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"

    if target.exists():
        try:
            with target.open("r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    else:
        cfg = {}

    cfg.setdefault("mcpServers", {})
    cfg["mcpServers"]["lens-studio"] = build_lens_entry(repo_root)

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    tmp.replace(target)

    print("Updated:", target)
    print("Entry 'lens-studio':")
    print(json.dumps(cfg["mcpServers"]["lens-studio"], indent=2))


if __name__ == "__main__":
    sys.exit(main() or 0)
