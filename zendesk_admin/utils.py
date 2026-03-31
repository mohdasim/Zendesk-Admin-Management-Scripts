import csv
import json
from pathlib import Path
from typing import Any


def write_csv(
    path: str,
    rows: list[dict],
    fieldnames: list[str] | None = None,
) -> None:
    """Write a list of dicts to a CSV file.

    Args:
        path: Output file path.
        rows: List of row dicts.
        fieldnames: Column headers. If None, uses keys from the first row.
    """
    if not rows:
        print("No data to write.")
        return

    fieldnames = fieldnames or list(rows[0].keys())

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows to {path}")


def print_json_report(data: Any, output_file: str | None = None) -> None:
    """Print data as formatted JSON, optionally writing to a file.

    Args:
        data: Data to serialize as JSON.
        output_file: If provided, write to this file instead of stdout.
    """
    formatted = json.dumps(data, indent=2, default=str)

    if output_file:
        Path(output_file).write_text(formatted, encoding="utf-8")
        print(f"Report written to {output_file}")
    else:
        print(formatted)
