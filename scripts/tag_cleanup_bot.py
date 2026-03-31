#!/usr/bin/env python3
"""Tag Cleanup Bot

Identifies orphan tags -- tags present on tickets but not referenced in
any Trigger, Automation, or View condition or action. Generates a report
for tag consolidation.

Usage:
    python -m scripts.tag_cleanup_bot
    python -m scripts.tag_cleanup_bot --min-tickets 5 -o orphans.json
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zendesk_admin import ZendeskClient, load_config
from zendesk_admin.cli import base_parser, setup_logging
from zendesk_admin.utils import print_json_report

# Zendesk field names that contain tag references
TAG_CONDITION_FIELDS = {"current_tags", "tag"}
TAG_ACTION_FIELDS = {"set_tags", "current_tags", "remove_tags"}


def extract_tags_from_item(item: dict) -> set:
    """Extract all tag references from a trigger, automation, or view.

    Examines both conditions (all/any groups) and actions for tag-related
    fields. Handles both space-separated strings and list values.

    Args:
        item: A trigger, automation, or view dict from the Zendesk API.

    Returns:
        Set of tag name strings found in the item.
    """
    tags = set()

    # Extract from conditions (all/any groups)
    conditions = item.get("conditions", {})
    for group in [conditions.get("all", []), conditions.get("any", [])]:
        for condition in group:
            if condition.get("field") in TAG_CONDITION_FIELDS:
                value = condition.get("value", "")
                if isinstance(value, str):
                    tags.update(t.strip() for t in value.split() if t.strip())
                elif isinstance(value, list):
                    tags.update(str(v).strip() for v in value if str(v).strip())

    # Extract from actions
    for action in item.get("actions", []):
        if action.get("field") in TAG_ACTION_FIELDS:
            value = action.get("value", "")
            if isinstance(value, str):
                tags.update(t.strip() for t in value.split() if t.strip())
            elif isinstance(value, list):
                tags.update(str(v).strip() for v in value if str(v).strip())

    return tags


def collect_referenced_tags(client: ZendeskClient) -> set:
    """Collect all tags referenced in triggers, automations, and views.

    Args:
        client: Authenticated ZendeskClient instance.

    Returns:
        Set of all tag names used in business rules.
    """
    referenced = set()

    sources = [
        ("/api/v2/triggers", "triggers", "triggers"),
        ("/api/v2/automations", "automations", "automations"),
        ("/api/v2/views", "views", "views"),
    ]

    for endpoint, key, label in sources:
        count = 0
        for item in client.paginate(endpoint, key):
            item_tags = extract_tags_from_item(item)
            referenced.update(item_tags)
            count += 1
        print(f"  Scanned {count} {label}")

    return referenced


def collect_ticket_tags(client: ZendeskClient) -> dict:
    """Collect all tags currently on tickets with their usage counts.

    Note: The Zendesk Tags API returns up to 20,000 most popular tags
    from the last 60 days.

    Args:
        client: Authenticated ZendeskClient instance.

    Returns:
        Dict mapping tag names to their ticket counts.
    """
    tag_counts = {}
    for tag in client.paginate("/api/v2/tags", "tags"):
        tag_counts[tag["name"]] = tag.get("count", 0)
    print(f"  Found {len(tag_counts)} tags on tickets")
    return tag_counts


def find_orphan_tags(
    ticket_tags: dict,
    referenced_tags: set,
    min_tickets: int = 1,
) -> list[dict]:
    """Identify tags on tickets that are not referenced in any business rule.

    Args:
        ticket_tags: Dict mapping tag names to ticket counts.
        referenced_tags: Set of tags referenced in triggers/automations/views.
        min_tickets: Minimum ticket count to include in the report.

    Returns:
        List of orphan tag dicts sorted by ticket count descending.
    """
    orphans = []
    for tag_name, count in ticket_tags.items():
        if tag_name not in referenced_tags and count >= min_tickets:
            orphans.append({
                "tag": tag_name,
                "ticket_count": count,
            })

    orphans.sort(key=lambda x: x["ticket_count"], reverse=True)
    return orphans


def main():
    parser = base_parser(
        "Tag Cleanup Bot\n\n"
        "Identifies orphan tags that exist on tickets but are not\n"
        "referenced in any Trigger, Automation, or View."
    )
    parser.add_argument(
        "--min-tickets",
        type=int,
        default=1,
        help="Only report orphan tags with at least N tickets (default: 1)",
    )
    parser.add_argument(
        "--output", "-o",
        help="Output JSON file path (default: print to stdout)",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    config = load_config(args.env_file)
    client = ZendeskClient(config)

    print("Step 1: Collecting tags from tickets...")
    ticket_tags = collect_ticket_tags(client)

    print("\nStep 2: Scanning business rules for tag references...")
    referenced_tags = collect_referenced_tags(client)
    print(f"\n  Total unique tags referenced in business rules: {len(referenced_tags)}")

    print("\nStep 3: Identifying orphan tags...")
    orphans = find_orphan_tags(ticket_tags, referenced_tags, args.min_tickets)

    print(f"\nResults:")
    print(f"  Tags on tickets:              {len(ticket_tags)}")
    print(f"  Tags in business rules:       {len(referenced_tags)}")
    print(f"  Orphan tags (>= {args.min_tickets} tickets):  {len(orphans)}")

    if orphans:
        print(f"\n{'Tag':<40} {'Ticket Count':>12}")
        print("-" * 54)
        for orphan in orphans[:50]:
            print(f"{orphan['tag']:<40} {orphan['ticket_count']:>12}")
        if len(orphans) > 50:
            print(f"  ... and {len(orphans) - 50} more (see full report with -o)")

    print_json_report(orphans, args.output)


if __name__ == "__main__":
    main()
