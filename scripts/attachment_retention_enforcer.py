#!/usr/bin/env python3
"""Attachment Retention Enforcer

Identifies tickets older than a configurable number of years and redacts
their attachments while preserving conversation text. Helps manage storage
costs and comply with data retention policies.

Zendesk replaces redacted attachments with an empty "redacted.txt" file.
This action is permanent and cannot be undone.

Usage:
    python -m scripts.attachment_retention_enforcer --older-than-years 2 --dry-run
    python -m scripts.attachment_retention_enforcer --older-than-years 3 --redact
    python -m scripts.attachment_retention_enforcer --older-than-years 1 --status closed --dry-run
"""

import sys
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zendesk_admin import ZendeskClient, load_config
from zendesk_admin.cli import base_parser, setup_logging
from zendesk_admin.utils import print_json_report

logger = logging.getLogger(__name__)


def search_old_tickets(
    client: ZendeskClient,
    older_than_years: int,
    status: str | None = None,
) -> list[dict]:
    """Search for tickets older than the specified number of years.

    Uses the Zendesk Search API with date filters.

    Args:
        client: Authenticated ZendeskClient instance.
        older_than_years: Number of years. Tickets created before this are matched.
        status: Optional ticket status filter (e.g. 'closed', 'solved').

    Returns:
        List of ticket dicts from search results.
    """
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=older_than_years * 365)
    cutoff_str = cutoff_date.strftime("%Y-%m-%d")

    query = f"type:ticket created<{cutoff_str}"
    if status:
        query += f" status:{status}"

    tickets = []
    for result in client.paginate("/api/v2/search.json", "results", params={"query": query}):
        tickets.append(result)

    return tickets


def get_ticket_attachments(client: ZendeskClient, ticket_id: int) -> list[dict]:
    """Get all attachments from a ticket's comments.

    Args:
        client: Authenticated ZendeskClient instance.
        ticket_id: The ticket ID to inspect.

    Returns:
        List of dicts with attachment and comment details.
    """
    attachments = []

    for comment in client.paginate(
        f"/api/v2/tickets/{ticket_id}/comments", "comments"
    ):
        for attachment in comment.get("attachments", []):
            attachments.append({
                "ticket_id": ticket_id,
                "comment_id": comment["id"],
                "attachment_id": attachment["id"],
                "file_name": attachment.get("file_name", "unknown"),
                "content_type": attachment.get("content_type", ""),
                "size": attachment.get("size", 0),
            })

    return attachments


def redact_attachment(
    client: ZendeskClient,
    ticket_id: int,
    comment_id: int,
    attachment_id: int,
) -> bool:
    """Redact a single attachment from a ticket comment.

    The attachment is replaced with an empty "redacted.txt" file by Zendesk.
    The comment text is preserved.

    Args:
        client: Authenticated ZendeskClient instance.
        ticket_id: The ticket ID.
        comment_id: The comment ID containing the attachment.
        attachment_id: The attachment ID to redact.

    Returns:
        True if redaction was successful.
    """
    endpoint = (
        f"/api/v2/tickets/{ticket_id}/comments/{comment_id}"
        f"/attachments/{attachment_id}/redact"
    )
    try:
        client.put(endpoint)
        return True
    except Exception as e:
        logger.warning(
            "Failed to redact attachment %d on ticket %d: %s",
            attachment_id, ticket_id, e,
        )
        return False


def format_size(size_bytes: int) -> str:
    """Format byte count to human-readable size."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def main():
    parser = base_parser(
        "Attachment Retention Enforcer\n\n"
        "Identifies tickets older than X years and redacts their\n"
        "attachments while preserving conversation text. Helps manage\n"
        "storage costs and comply with data retention policies.\n\n"
        "WARNING: Redaction is PERMANENT and cannot be undone."
    )
    parser.add_argument(
        "--older-than-years",
        type=int,
        required=True,
        metavar="YEARS",
        help="Target tickets created more than N years ago",
    )
    parser.add_argument(
        "--status",
        choices=["new", "open", "pending", "hold", "solved", "closed"],
        help="Only target tickets with this status (default: all statuses)",
    )
    parser.add_argument(
        "--redact",
        action="store_true",
        help="Actually redact attachments (without this flag, runs in report-only mode)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview which attachments would be redacted (same as omitting --redact)",
    )
    parser.add_argument(
        "--max-tickets",
        type=int,
        default=100,
        help="Maximum number of tickets to process (default: 100, for safety)",
    )
    parser.add_argument(
        "--output", "-o",
        help="Output JSON file path for the attachment report",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    config = load_config(args.env_file)
    client = ZendeskClient(config)

    cutoff_date = datetime.now(timezone.utc) - timedelta(days=args.older_than_years * 365)
    status_str = f" with status '{args.status}'" if args.status else ""
    print(f"Searching for tickets created before {cutoff_date.strftime('%Y-%m-%d')}{status_str}...")

    tickets = search_old_tickets(client, args.older_than_years, args.status)
    print(f"Found {len(tickets)} ticket(s) matching criteria.")

    if len(tickets) > args.max_tickets:
        print(f"Limiting to first {args.max_tickets} tickets (use --max-tickets to adjust).")
        tickets = tickets[: args.max_tickets]

    # Collect all attachments from matched tickets
    all_attachments = []
    tickets_with_attachments = 0
    total_size = 0

    print(f"\nScanning {len(tickets)} ticket(s) for attachments...")
    for i, ticket in enumerate(tickets, 1):
        ticket_id = ticket["id"]
        attachments = get_ticket_attachments(client, ticket_id)

        if attachments:
            tickets_with_attachments += 1
            for att in attachments:
                att["ticket_subject"] = ticket.get("subject", "(no subject)")
                att["ticket_created_at"] = ticket.get("created_at", "")
                total_size += att["size"]
            all_attachments.extend(attachments)

        if i % 20 == 0 or i == len(tickets):
            print(f"  Scanned {i}/{len(tickets)} tickets ({len(all_attachments)} attachments found)")

    print(f"\nResults:")
    print(f"  Tickets scanned:           {len(tickets)}")
    print(f"  Tickets with attachments:  {tickets_with_attachments}")
    print(f"  Total attachments found:   {len(all_attachments)}")
    print(f"  Total attachment size:     {format_size(total_size)}")

    if all_attachments:
        print(f"\n{'Ticket':<12} {'Attachment':<14} {'Size':>10}  {'File Name'}")
        print("-" * 75)
        for att in all_attachments[:30]:
            print(
                f"{att['ticket_id']:<12} {att['attachment_id']:<14} "
                f"{format_size(att['size']):>10}  {att['file_name'][:35]}"
            )
        if len(all_attachments) > 30:
            print(f"  ... and {len(all_attachments) - 30} more (see full report with -o)")

    if args.redact and not args.dry_run and all_attachments:
        print(f"\nRedacting {len(all_attachments)} attachment(s)...")
        print("WARNING: This action is PERMANENT and cannot be undone.\n")
        redacted = 0
        failed = 0
        for att in all_attachments:
            success = redact_attachment(
                client, att["ticket_id"], att["comment_id"], att["attachment_id"]
            )
            if success:
                redacted += 1
            else:
                failed += 1

            if (redacted + failed) % 10 == 0:
                print(f"  Progress: {redacted + failed}/{len(all_attachments)} "
                      f"(redacted: {redacted}, failed: {failed})")

        print(f"\nRedaction complete:")
        print(f"  Successfully redacted: {redacted}")
        if failed:
            print(f"  Failed:                {failed}")
        print(f"  Storage freed (approx): {format_size(total_size)}")
    elif all_attachments:
        print(f"\n[REPORT-ONLY MODE] No attachments redacted.")
        print(f"Use --redact to permanently redact these attachments.")
        if args.dry_run:
            print(f"(--dry-run flag is active)")

    if args.output:
        report = {
            "cutoff_date": cutoff_date.isoformat(),
            "tickets_scanned": len(tickets),
            "tickets_with_attachments": tickets_with_attachments,
            "total_attachments": len(all_attachments),
            "total_size_bytes": total_size,
            "total_size_human": format_size(total_size),
            "attachments": all_attachments,
        }
        print_json_report(report, args.output)


if __name__ == "__main__":
    main()
