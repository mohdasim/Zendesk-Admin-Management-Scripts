#!/usr/bin/env python3
"""Inactive API Token Auditor

Lists all OAuth access tokens and API tokens, then highlights those that
haven't been used within a configurable number of days, alerting the admin
to revoke them for security.

Audits two token types:
  - OAuth tokens via /api/v2/oauth/tokens (uses `used_at` field)
  - API tokens via /api/v2/api_tokens (uses `last_used_at` field)

Note: The /api/v2/api_tokens endpoint may not be available on all Zendesk
plans. If unavailable, the script falls back gracefully and reports only
OAuth tokens.

Usage:
    python -m scripts.inactive_api_token_auditor
    python -m scripts.inactive_api_token_auditor --inactive-days 30 -o token_audit.json
    python -m scripts.inactive_api_token_auditor --token-type api --format csv -o api_tokens.csv
"""

import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zendesk_admin import ZendeskClient, load_config
from zendesk_admin.cli import base_parser, setup_logging
from zendesk_admin.utils import print_json_report, write_csv

logger = logging.getLogger(__name__)


def fetch_oauth_tokens(client: ZendeskClient) -> list[dict]:
    """Fetch all OAuth access tokens.

    Args:
        client: Authenticated ZendeskClient instance.

    Returns:
        List of OAuth token dicts from the API.
    """
    return list(client.paginate("/api/v2/oauth/tokens", "tokens"))


def fetch_api_tokens(client: ZendeskClient) -> list[dict]:
    """Fetch all API tokens via /api/v2/api_tokens.json.

    This endpoint may not be available on all Zendesk plans. If the
    endpoint returns a 403 or 404, an empty list is returned with a
    warning logged.

    Args:
        client: Authenticated ZendeskClient instance.

    Returns:
        List of API token dicts, or empty list if endpoint is unavailable.
    """
    try:
        return list(client.paginate("/api/v2/api_tokens", "api_tokens"))
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status in (403, 404):
            logger.warning(
                "API tokens endpoint not available (HTTP %s). "
                "This endpoint may not be supported on your Zendesk plan.",
                status,
            )
            print(
                "  Warning: /api/v2/api_tokens returned HTTP "
                f"{status}. Endpoint may not be available on your plan."
            )
        else:
            logger.warning("Failed to fetch API tokens: %s", e)
            print(f"  Warning: Failed to fetch API tokens ({e})")
        return []


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


def _parse_timestamp(ts: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp string to a timezone-aware datetime."""
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _classify_by_usage(
    used_at_str: str | None,
    cutoff: datetime,
    now: datetime,
) -> tuple[str, int | None]:
    """Determine token status and days since last use.

    Returns:
        Tuple of (status, days_since_last_use).
        status is one of 'never_used', 'inactive', 'active'.
    """
    if not used_at_str:
        return "never_used", None

    used_dt = _parse_timestamp(used_at_str)
    if used_dt is None:
        return "never_used", None

    days = (now - used_dt).days
    if used_dt < cutoff:
        return "inactive", days
    return "active", days


def classify_oauth_tokens(
    tokens: list[dict],
    user_map: dict,
    inactive_days: int,
) -> list[dict]:
    """Classify OAuth tokens as active, inactive, or never-used.

    Args:
        tokens: List of OAuth token dicts.
        user_map: Dict mapping user IDs to user details.
        inactive_days: Number of days after which a token is considered inactive.

    Returns:
        List of classified token audit dicts.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=inactive_days)
    results = []

    for token in tokens:
        user_id = token.get("user_id")
        user = user_map.get(user_id, {})
        used_at = token.get("used_at")
        status, days_inactive = _classify_by_usage(used_at, cutoff, now)

        results.append({
            "token_type": "oauth",
            "token_id": token.get("id"),
            "status": status,
            "description": token.get("client_id", ""),
            "scopes": ", ".join(token.get("scopes", [])) if token.get("scopes") else "",
            "active": "",
            "created_at": token.get("created_at", ""),
            "last_used_at": used_at or "never",
            "days_since_last_use": days_inactive,
            "user_id": user_id,
            "user_name": user.get("name", ""),
            "user_email": user.get("email", ""),
            "user_role": user.get("role", ""),
            "user_active": user.get("active", ""),
            "user_last_login_at": user.get("last_login_at", ""),
        })

    return results


def classify_api_tokens(
    tokens: list[dict],
    user_map: dict,
    inactive_days: int,
) -> list[dict]:
    """Classify API tokens as active, inactive, or never-used.

    Uses `last_used_at` field for inactivity detection (distinct from
    OAuth tokens which use `used_at`).

    Args:
        tokens: List of API token dicts.
        user_map: Dict mapping user IDs to user details.
        inactive_days: Number of days after which a token is considered inactive.

    Returns:
        List of classified token audit dicts.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=inactive_days)
    results = []

    for token in tokens:
        user_id = token.get("user_id")
        user = user_map.get(user_id, {})
        last_used = token.get("last_used_at")
        status, days_inactive = _classify_by_usage(last_used, cutoff, now)

        results.append({
            "token_type": "api_token",
            "token_id": token.get("id"),
            "status": status,
            "description": token.get("description", ""),
            "scopes": "",
            "active": token.get("active", ""),
            "created_at": token.get("created_at", ""),
            "last_used_at": last_used or "never",
            "days_since_last_use": days_inactive,
            "user_id": user_id,
            "user_name": user.get("name", ""),
            "user_email": user.get("email", ""),
            "user_role": user.get("role", ""),
            "user_active": user.get("active", ""),
            "user_last_login_at": user.get("last_login_at", ""),
        })

    return results


def sort_results(results: list[dict]) -> list[dict]:
    """Sort results: never_used first, then inactive (most days first), then active."""
    status_order = {"never_used": 0, "inactive": 1, "active": 2}
    results.sort(key=lambda x: (
        status_order.get(x["status"], 3),
        -(x["days_since_last_use"] or 999999),
    ))
    return results


def main():
    parser = base_parser(
        "Inactive API Token Auditor\n\n"
        "Lists all OAuth access tokens and API tokens, then highlights\n"
        "those that haven't been used within a configurable number of\n"
        "days. Alerts the admin to revoke inactive tokens for security.\n\n"
        "Note: The /api/v2/api_tokens endpoint may not be available on\n"
        "all Zendesk plans. If unavailable, the script falls back\n"
        "gracefully and reports only OAuth tokens."
    )
    parser.add_argument(
        "--inactive-days",
        type=int,
        default=30,
        metavar="DAYS",
        help="Mark tokens as inactive if not used in N days (default: 30)",
    )
    parser.add_argument(
        "--token-type",
        choices=["all", "oauth", "api"],
        default="all",
        help="Token type to audit: all, oauth, or api (default: all)",
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

    oauth_tokens = []
    api_tokens = []

    # Fetch OAuth tokens
    if args.token_type in ("all", "oauth"):
        print("Fetching OAuth tokens...")
        oauth_tokens = fetch_oauth_tokens(client)
        print(f"  Found {len(oauth_tokens)} OAuth token(s).")

    # Fetch API tokens
    if args.token_type in ("all", "api"):
        print("Fetching API tokens...")
        api_tokens = fetch_api_tokens(client)
        print(f"  Found {len(api_tokens)} API token(s).")

    total = len(oauth_tokens) + len(api_tokens)
    if total == 0:
        print("\nNo tokens found.")
        if args.token_type == "api":
            print("The /api/v2/api_tokens endpoint may not be available on your plan.")
        return

    # Collect unique user IDs from both token types and fetch user details
    user_ids = set()
    for t in oauth_tokens:
        if t.get("user_id"):
            user_ids.add(t["user_id"])
    for t in api_tokens:
        if t.get("user_id"):
            user_ids.add(t["user_id"])

    if user_ids:
        print(f"Fetching details for {len(user_ids)} token owner(s)...")
        user_map = fetch_user_lookup(client, user_ids)
    else:
        user_map = {}

    # Classify both token types and merge
    results = []
    if oauth_tokens:
        results.extend(classify_oauth_tokens(oauth_tokens, user_map, args.inactive_days))
    if api_tokens:
        results.extend(classify_api_tokens(api_tokens, user_map, args.inactive_days))

    results = sort_results(results)

    # Summary
    never_used = [r for r in results if r["status"] == "never_used"]
    inactive = [r for r in results if r["status"] == "inactive"]
    active = [r for r in results if r["status"] == "active"]
    oauth_count = sum(1 for r in results if r["token_type"] == "oauth")
    api_count = sum(1 for r in results if r["token_type"] == "api_token")

    print(f"\nToken Audit Summary (inactive threshold: {args.inactive_days} days):")
    print(f"  Total tokens:    {len(results)}")
    if oauth_count:
        print(f"    OAuth tokens:  {oauth_count}")
    if api_count:
        print(f"    API tokens:    {api_count}")
    print(f"  Active:          {len(active)}")
    print(f"  Inactive:        {len(inactive)}")
    print(f"  Never used:      {len(never_used)}")

    # Display tokens requiring attention
    attention = never_used + inactive
    if attention:
        print(f"\nTokens requiring attention ({len(attention)}):")
        print(
            f"{'Type':<11} {'ID':<12} {'Status':<13} {'Last Used':<14} "
            f"{'Days':<6} {'User':<25} {'Description/Scopes'}"
        )
        print("-" * 110)
        for r in attention[:30]:
            days_str = str(r["days_since_last_use"]) if r["days_since_last_use"] is not None else "N/A"
            used_str = r["last_used_at"][:10] if r["last_used_at"] != "never" else "never"
            user_str = r["user_email"] or r["user_name"] or str(r.get("user_id", ""))
            desc = r["scopes"] or r["description"] or ""
            type_label = "OAuth" if r["token_type"] == "oauth" else "API"
            print(
                f"{type_label:<11} {r['token_id']:<12} {r['status']:<13} {used_str:<14} "
                f"{days_str:<6} {user_str[:25]:<25} {desc[:25]}"
            )
        if len(attention) > 30:
            print(f"  ... and {len(attention) - 30} more")

        print(f"\nRecommendation: Review and revoke inactive/unused tokens in")
        print(f"Admin Center > Apps and Integrations > APIs")
    else:
        print(f"\nAll tokens are active. No action required.")

    # Output
    if args.output:
        if args.format == "csv":
            write_csv(args.output, results)
        else:
            print_json_report(results, args.output)


if __name__ == "__main__":
    main()
