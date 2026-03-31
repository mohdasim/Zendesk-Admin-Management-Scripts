#!/usr/bin/env python3
"""Bulk Macro Content Search & Replace

Searches for specific text or URLs across all Zendesk macros and optionally
replaces them. Supports dry-run mode for safe previewing.

Usage:
    python -m scripts.bulk_macro_search_replace --search "old-url.com"
    python -m scripts.bulk_macro_search_replace --search "old-url.com" --replace "new-url.com" --dry-run
    python -m scripts.bulk_macro_search_replace --search "old-url.com" --replace "new-url.com"
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zendesk_admin import ZendeskClient, load_config
from zendesk_admin.cli import base_parser, setup_logging
from zendesk_admin.utils import print_json_report


def search_macros(client: ZendeskClient, search_text: str) -> list[dict]:
    """Search all macros for actions containing the specified text.

    Examines action values (both string and list types) for matches.

    Args:
        client: Authenticated ZendeskClient instance.
        search_text: Text to search for in macro action values.

    Returns:
        List of dicts with macro details and matched actions.
    """
    matches = []

    for macro in client.paginate("/api/v2/macros", "macros"):
        actions = macro.get("actions", [])
        matched_actions = []

        for action in actions:
            value = action.get("value", "")
            value_str = json.dumps(value) if not isinstance(value, str) else value

            if search_text in value_str:
                matched_actions.append({
                    "field": action.get("field"),
                    "value": value,
                })

        if matched_actions:
            matches.append({
                "id": macro["id"],
                "title": macro.get("title", "Untitled"),
                "active": macro.get("active", False),
                "matched_actions": matched_actions,
            })

    return matches


def replace_in_macro(client: ZendeskClient, macro_id: int, search_text: str, replace_text: str) -> dict:
    """Replace text in a single macro's action values.

    Args:
        client: Authenticated ZendeskClient instance.
        macro_id: ID of the macro to update.
        search_text: Text to find.
        replace_text: Replacement text.

    Returns:
        Updated macro response from the API.
    """
    macro_data = client.get(f"/api/v2/macros/{macro_id}")["macro"]
    updated_actions = []

    for action in macro_data.get("actions", []):
        value = action.get("value")

        if isinstance(value, str):
            action["value"] = value.replace(search_text, replace_text)
        elif isinstance(value, list):
            action["value"] = [
                v.replace(search_text, replace_text) if isinstance(v, str) else v
                for v in value
            ]

        updated_actions.append(action)

    return client.put(
        f"/api/v2/macros/{macro_id}",
        json={"macro": {"actions": updated_actions}},
    )


def main():
    parser = base_parser(
        "Bulk Macro Content Search & Replace\n\n"
        "Search for specific text or URLs across all Zendesk macros\n"
        "and optionally replace them in bulk."
    )
    parser.add_argument(
        "--search", "-s",
        required=True,
        help="Text or URL to search for in macro actions",
    )
    parser.add_argument(
        "--replace", "-r",
        help="Replacement text (omit for search-only mode)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without applying them (requires --replace)",
    )
    parser.add_argument(
        "--output", "-o",
        help="Output JSON file path for the match report",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    config = load_config(args.env_file)
    client = ZendeskClient(config)

    print(f"Searching all macros for: \"{args.search}\"...")
    matches = search_macros(client, args.search)
    print(f"Found {len(matches)} macro(s) containing \"{args.search}\"")

    if matches:
        print(f"\n{'ID':<20} {'Active':<8} {'Title'}")
        print("-" * 70)
        for m in matches:
            print(f"{m['id']:<20} {str(m['active']):<8} {m['title']}")

    if args.replace and matches:
        print()
        if args.dry_run:
            print("[DRY RUN] The following macros would be updated:")
            for m in matches:
                print(f"  - {m['title']} (ID: {m['id']})")
            print(f"\nRerun without --dry-run to apply changes.")
        else:
            print(f"Replacing \"{args.search}\" with \"{args.replace}\"...")
            updated = 0
            for m in matches:
                replace_in_macro(client, m["id"], args.search, args.replace)
                print(f"  Updated: {m['title']} (ID: {m['id']})")
                updated += 1
            print(f"\nSuccessfully updated {updated} macro(s).")

    if args.output:
        print_json_report(matches, args.output)


if __name__ == "__main__":
    main()
