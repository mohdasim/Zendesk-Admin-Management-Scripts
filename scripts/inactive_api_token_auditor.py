#!/usr/bin/env python3
"""Inactive API Token Auditor

Lists all OAuth access tokens and highlights those that haven't been used
within a configurable number of days, alerting the admin to revoke them
for security.

Note: This audits OAuth tokens (which have a `used_at` field). Generic API
tokens created in the Admin Center are not queryable via the REST API.

Usage:
    python -m scripts.inactive_api_token_auditor
    python -m scripts.inactive_api_token_auditor --inactive-days 30 -o token_audit.json
"""

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zendesk_admin import ZendeskClient, load_config
from zendesk_admin.cli import base_parser, setup_logging
from zendesk_admin.utils import print_json_report, write_csv


def fetch_oauth_tokens(client: ZendeskClient) -> list[dict]:
    """Fetch all OAuth access tokens.

    Args:
        client: Authenticated ZendeskClient instance.

    Returns:
        List of OAuth token dicts from the API.
    """
    return list(client.paginate("/api/v2/oauth/tokens", "tokens"))


def fetch_user_lookup(client: ZendeskClient, user_ids: set) -> dict:
    """Fetch user details for a set of user IDs.

    Args:
        client: Authenticated ZendeskClient instance.
        user_ids: Set of user IDs to look up.

    Returns:
        Dict mapping user IDs to user detail dicts.
    """
    user_map = {}
    if not user_ids:
        return user_map

    # Fetch users in batches using show_many endpoint
    id_list = list(user_ids)
    batch_size = 100
    for i in range(0, len(id_list), batch_size):
        batch = id_list[i : i + batch_size]
        ids_param = ",".join(str(uid) for uid in batch)
        data = client.get("/api/v2/users/show_many", params={"ids": ids_param})
        for user in data.get("users", []):
            user_map[user["id"]] = user

    return user_map


def classify_tokens(
    tokens: list[dict],
    user_map: dict,
    inactive_days: int,
) -> list[dict]:
    """Classify tokens as active, inactive, or never-used.

    Args:
        tokens: List of OAuth token dicts.
        user_map: Dict mapping user IDs to user details.
        inactive_days: Number of days after which a token is considered inactive.

    Returns:
        List of classified token audit dicts.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=inactive_days)
    results = []

    for token in tokens:
        user_id = token.get("user_id")
        user = user_map.get(user_id, {})

        used_at = token.get("used_at")
        created_at = token.get("created_at", "")

        # Determine status
        if not used_at:
            status = "never_used"
            days_inactive = None
        else:
            used_dt = datetime.fromisoformat(used_at.replace("Z", "+00:00"))
            if used_dt < cutoff:
                status = "inactive"
                days_inactive = (datetime.now(timezone.utc) - used_dt).days
            else:
                status = "active"
                days_inactive = (datetime.now(timezone.utc) - used_dt).days

        results.append({
            "token_id": token.get("id"),
            "status": status,
            "client_id": token.get("client_id"),
            "scopes": ", ".join(token.get("scopes", [])) if token.get("scopes") else "",
            "created_at": created_at,
            "used_at": used_at or "never",
            "days_since_last_use": days_inactive,
            "user_id": user_id,
            "user_name": user.get("name", ""),
            "user_email": user.get("email", ""),
            "user_role": user.get("role", ""),
            "user_active": user.get("active", ""),
            "user_last_login_at": user.get("last_login_at", ""),
        })

    # Sort: never_used first, then inactive (most days first), then active
    status_order = {"never_used": 0, "inactive": 1, "active": 2}
    results.sort(key=lambda x: (
        status_order.get(x["status"], 3),
        -(x["days_since_last_use"] or 999999),
    ))

    return results


def main():
    parser = base_parser(
        "Inactive API Token Auditor\n\n"
        "Lists all OAuth access tokens and highlights those that haven't\n"
        "been used within a configurable number of days. Alerts the admin\n"
        "to revoke inactive tokens for security."
    )
    parser.add_argument(
        "--inactive-days",
        type=int,
        default=30,
        metavar="DAYS",
        help="Mark tokens as inactive if not used in N days (default: 30)",
    )
    parser.add_argument(
        "--format",
        choices=["json", "csv"],
        default="json",
        help="Output format (default: json)",
    )
    parser.add_argument(
        "--output", "-o",
        help="Output file path (default: print to stdout)",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    config = load_config(args.env_file)
    client = ZendeskClient(config)

    print("Fetching OAuth tokens...")
    tokens = fetch_oauth_tokens(client)
    print(f"Found {len(tokens)} OAuth token(s).")

    if not tokens:
        print("\nNo OAuth tokens found. Note: Generic API tokens created in")
        print("Admin Center are not queryable via the REST API.")
        return

    # Collect unique user IDs and fetch user details
    user_ids = {t["user_id"] for t in tokens if t.get("user_id")}
    print(f"Fetching details for {len(user_ids)} token owner(s)...")
    user_map = fetch_user_lookup(client, user_ids)

    # Classify tokens
    results = classify_tokens(tokens, user_map, args.inactive_days)

    # Summary
    never_used = [r for r in results if r["status"] == "never_used"]
    inactive = [r for r in results if r["status"] == "inactive"]
    active = [r for r in results if r["status"] == "active"]

    print(f"\nToken Audit Summary (inactive threshold: {args.inactive_days} days):")
    print(f"  Total tokens:    {len(results)}")
    print(f"  Active:          {len(active)}")
    print(f"  Inactive:        {len(inactive)}")
    print(f"  Never used:      {len(never_used)}")

    # Display tokens requiring attention
    attention = never_used + inactive
    if attention:
        print(f"\nTokens requiring attention ({len(attention)}):")
        print(f"{'ID':<12} {'Status':<13} {'Last Used':<14} {'Days':<6} {'User':<25} {'Scopes'}")
        print("-" * 95)
        for r in attention[:30]:
            days_str = str(r["days_since_last_use"]) if r["days_since_last_use"] is not None else "N/A"
            used_str = r["used_at"][:10] if r["used_at"] != "never" else "never"
            user_str = r["user_email"] or r["user_name"] or str(r["user_id"])
            print(
                f"{r['token_id']:<12} {r['status']:<13} {used_str:<14} "
                f"{days_str:<6} {user_str[:25]:<25} {r['scopes'][:20]}"
            )
        if len(attention) > 30:
            print(f"  ... and {len(attention) - 30} more")

        print(f"\nRecommendation: Review and revoke inactive/unused tokens in")
        print(f"Admin Center > Apps and Integrations > APIs > OAuth Tokens")
    else:
        print(f"\nAll tokens are active. No action required.")

    # Output
    if args.output:
        if args.format == "csv":
            write_csv(args.output, results)
        else:
            print_json_report(results, args.output)
    elif not args.output and attention:
        # Print full JSON to stdout only if no file output specified
        pass


if __name__ == "__main__":
    main()
