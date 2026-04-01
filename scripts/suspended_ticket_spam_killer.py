#!/usr/bin/env python3
"""Suspended Ticket Spam-Killer

Bulk-deletes suspended tickets based on specific cause patterns (e.g.,
"Detected as spam", "Automated response mail") to keep the suspended
queue manageable.

Usage:
    python -m scripts.suspended_ticket_spam_killer --causes "Detected as spam"
    python -m scripts.suspended_ticket_spam_killer --causes "Detected as spam" "Automated response mail" --dry-run
    python -m scripts.suspended_ticket_spam_killer --older-than 30 --dry-run
"""

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zendesk_admin import ZendeskClient, load_config
from zendesk_admin.cli import base_parser, setup_logging
from zendesk_admin.utils import print_json_report

# Common suspended ticket causes in Zendesk
KNOWN_SPAM_CAUSES = [
    "Detected as spam",
    "Automated response mail",
    "Detected as spam by Zendesk",
]

BATCH_SIZE = 100  # Zendesk limit for destroy_many


def list_suspended_tickets(
    client: ZendeskClient,
    cause_patterns: list[str] | None = None,
    older_than_days: int | None = None,
) -> list[dict]:
    """Fetch suspended tickets matching the given cause patterns and age filter.

    Args:
        client: Authenticated ZendeskClient instance.
        cause_patterns: List of cause strings to match (case-insensitive substring).
                       If None, matches all suspended tickets.
        older_than_days: Only include tickets older than this many days.

    Returns:
        List of matching suspended ticket dicts.
    """
    cutoff = None
    if older_than_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)

    matches = []
    for ticket in client.paginate("/api/v2/suspended_tickets", "suspended_tickets"):
        cause = ticket.get("cause", "")

        # Filter by cause pattern if specified
        if cause_patterns:
            cause_lower = cause.lower()
            if not any(pattern.lower() in cause_lower for pattern in cause_patterns):
                continue

        # Filter by age if specified
        if cutoff:
            created = ticket.get("created_at", "")
            if created:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if created_dt >= cutoff:
                    continue

        matches.append({
            "id": ticket["id"],
            "subject": ticket.get("subject", "(no subject)"),
            "cause": cause,
            "author": ticket.get("author", {}).get("email", "unknown"),
            "created_at": ticket.get("created_at"),
            "recipient": ticket.get("recipient", ""),
        })

    return matches


def bulk_delete_suspended(client: ZendeskClient, ticket_ids: list[int]) -> int:
    """Delete suspended tickets in batches of 100.

    Args:
        client: Authenticated ZendeskClient instance.
        ticket_ids: List of suspended ticket IDs to delete.

    Returns:
        Total number of tickets deleted.
    """
    deleted = 0
    for i in range(0, len(ticket_ids), BATCH_SIZE):
        batch = ticket_ids[i : i + BATCH_SIZE]
        ids_param = ",".join(str(tid) for tid in batch)
        client.delete("/api/v2/suspended_tickets/destroy_many", params={"ids": ids_param})
        deleted += len(batch)
        print(f"  Deleted batch {i // BATCH_SIZE + 1}: {len(batch)} tickets")

    return deleted


def main():
    parser = base_parser(
        "Suspended Ticket Spam-Killer\n\n"
        "Bulk-deletes suspended tickets based on cause patterns\n"
        "(e.g., spam detection) to keep the suspended queue clean."
    )
    parser.add_argument(
        "--causes", "-c",
        nargs="+",
        help=(
            "Cause patterns to match (case-insensitive substring). "
            "Examples: \"Detected as spam\" \"Automated response mail\". "
            "If omitted, lists all suspended tickets."
        ),
    )
    parser.add_argument(
        "--older-than",
        type=int,
        metavar="DAYS",
        help="Only target suspended tickets older than N days",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Actually delete matched tickets (without this flag, runs in report-only mode)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview which tickets would be deleted (same as omitting --delete)",
    )
    parser.add_argument(
        "--output", "-o",
        help="Output JSON file path for the match report",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    config = load_config(args.env_file)
    client = ZendeskClient(config)

    # Build filter description for display
    filter_desc = []
    if args.causes:
        filter_desc.append(f"causes matching: {', '.join(repr(c) for c in args.causes)}")
    if args.older_than:
        filter_desc.append(f"older than {args.older_than} days")
    filter_str = " AND ".join(filter_desc) if filter_desc else "all suspended tickets"

    print(f"Scanning suspended tickets ({filter_str})...")
    matches = list_suspended_tickets(client, args.causes, args.older_than)

    # Group by cause for summary
    cause_counts = {}
    for m in matches:
        cause = m["cause"]
        cause_counts[cause] = cause_counts.get(cause, 0) + 1

    print(f"\nFound {len(matches)} matching suspended ticket(s):")
    for cause, count in sorted(cause_counts.items(), key=lambda x: -x[1]):
        print(f"  - {cause}: {count}")

    if matches:
        print(f"\n{'ID':<20} {'Created':<22} {'Cause':<30} {'Subject'}")
        print("-" * 100)
        for m in matches[:25]:
            created = m["created_at"][:10] if m["created_at"] else "unknown"
            print(f"{m['id']:<20} {created:<22} {m['cause']:<30} {m['subject'][:40]}")
        if len(matches) > 25:
            print(f"  ... and {len(matches) - 25} more")

    if args.delete and not args.dry_run and matches:
        print(f"\nDeleting {len(matches)} suspended ticket(s)...")
        ticket_ids = [m["id"] for m in matches]
        deleted = bulk_delete_suspended(client, ticket_ids)
        print(f"\nSuccessfully deleted {deleted} suspended ticket(s).")
    elif matches:
        print(f"\n[REPORT-ONLY MODE] No tickets deleted.")
        print(f"Use --delete to permanently remove these tickets.")
        if args.dry_run:
            print(f"(--dry-run flag is active)")

    if args.output:
        print_json_report(matches, args.output)


if __name__ == "__main__":
    main()
