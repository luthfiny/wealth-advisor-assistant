"""Atomic report exports shared by single-client and batch commands."""

import os
import tempfile
from pathlib import Path

from .domain import dumps


def write_result_file(report, output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent, delete=False
        ) as handle:
            temporary = handle.name
            handle.write(dumps(report) + "\n")
        os.replace(temporary, output)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def write_result(report, output=None):
    if output:
        write_result_file(report, output)
    print(dumps(report))
