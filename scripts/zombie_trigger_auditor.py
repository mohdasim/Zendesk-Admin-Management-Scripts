#!/usr/bin/env python3
"""Zombie Trigger Auditor

Identifies triggers and automations with zero usage over a configurable
time period using Zendesk's built-in usage statistics (usage_1h, usage_24h,
usage_7d, usage_30d).

Usage:
    python -m scripts.zombie_trigger_auditor --period 7d
    python -m scripts.zombie_trigger_auditor --period 30d --include-inactive -o zombies.json
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zendesk_admin import ZendeskClient, load_config
from zendesk_admin.cli import base_parser, setup_logging
from zendesk_admin.utils import print_json_report

USAGE_FIELDS = ["usage_1h", "usage_24h", "usage_7d", "usage_30d"]

PERIOD_LABELS = {
    "1h": "1 hour",
    "24h": "24 hours",
    "7d": "7 days",
    "30d": "30 days",
}


def audit_zombies(client: ZendeskClient, period: str, include_inactive: bool) -> list[dict]:
    """Find triggers and automations with zero usage in the given period.

    Args:
        client: Authenticated ZendeskClient instance.
        period: One of '1h', '24h', '7d', '30d'.
        include_inactive: If True, also report inactive (disabled) items.

    Returns:
        List of zombie trigger/automation dicts.
    """
    usage_key = f"usage_{period}"
    include_param = ",".join(USAGE_FIELDS)
    zombies = []

    resources = [
        ("trigger", "/api/v2/triggers", "triggers"),
        ("automation", "/api/v2/automations", "automations"),
    ]

    for resource_type, endpoint, key in resources:
        params = {"include": include_param}
        if not include_inactive:
            params["active"] = "true"

        for item in client.paginate(endpoint, key, params=params):
            if not include_inactive and not item.get("active", True):
                continue

            usage = item.get(usage_key, 0)
            if usage == 0:
                zombies.append({
                    "type": resource_type,
                    "id": item["id"],
                    "title": item.get("title", item.get("raw_title", "Untitled")),
                    "active": item.get("active", False),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "usage_1h": item.get("usage_1h", 0),
                    "usage_24h": item.get("usage_24h", 0),
                    "usage_7d": item.get("usage_7d", 0),
                    "usage_30d": item.get("usage_30d", 0),
                    "description": item.get("description", ""),
                    "category_id": item.get("category_id"),
                })

    return zombies


def main():
    parser = base_parser(
        "Zombie Trigger Auditor\n\n"
        "Identifies triggers and automations with zero usage over a\n"
        "configurable time period using Zendesk's usage statistics."
    )
    parser.add_argument(
        "--period",
        choices=["1h", "24h", "7d", "30d"],
        default="7d",
        help="Usage period to check (default: 7d)",
    )
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="Include disabled triggers/automations in the report",
    )
    parser.add_argument(
        "--output", "-o",
        help="Output JSON file path (default: print to stdout)",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    config = load_config(args.env_file)
    client = ZendeskClient(config)

    print(f"Auditing triggers and automations with zero usage in the last {PERIOD_LABELS[args.period]}...")
    zombies = audit_zombies(client, args.period, args.include_inactive)

    print(f"\nFound {len(zombies)} zombie items (zero usage in {PERIOD_LABELS[args.period]}):")
    print(f"  - Triggers: {sum(1 for z in zombies if z['type'] == 'trigger')}")
    print(f"  - Automations: {sum(1 for z in zombies if z['type'] == 'automation')}")

    if zombies:
        print(f"\n{'Type':<12} {'ID':<20} {'Active':<8} {'Title'}")
        print("-" * 80)
        for z in zombies:
            print(f"{z['type']:<12} {z['id']:<20} {str(z['active']):<8} {z['title']}")

    print_json_report(zombies, args.output)


if __name__ == "__main__":
    main()
