#!/usr/bin/env python3
"""User Permission Snapshotter

Exports a CSV of all Admin and Agent users with their last login date,
custom role name, and permissions for monthly security audits.

Usage:
    python -m scripts.user_permission_snapshotter
    python -m scripts.user_permission_snapshotter -o audit_march_2026.csv
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zendesk_admin import ZendeskClient, load_config
from zendesk_admin.cli import base_parser, setup_logging
from zendesk_admin.utils import write_csv

CSV_FIELDS = [
    "id",
    "name",
    "email",
    "role",
    "custom_role_id",
    "custom_role_name",
    "custom_role_permissions",
    "last_login_at",
    "two_factor_auth_enabled",
    "active",
    "suspended",
    "created_at",
    "updated_at",
]


def fetch_custom_roles(client: ZendeskClient) -> dict:
    """Fetch custom roles and return a lookup dict keyed by role ID.

    Custom roles are only available on Enterprise+ plans. Returns an
    empty dict if the endpoint is not accessible.
    """
    try:
        data = client.get("/api/v2/custom_roles")
        return {role["id"]: role for role in data.get("custom_roles", [])}
    except Exception:
        print("Note: Could not fetch custom roles (requires Enterprise+ plan).")
        return {}


def snapshot_users(client: ZendeskClient, role_map: dict) -> list[dict]:
    """Fetch all admin and agent users with their role details.

    Args:
        client: Authenticated ZendeskClient instance.
        role_map: Dict mapping custom role IDs to role detail dicts.

    Returns:
        List of user snapshot dicts ready for CSV export.
    """
    rows = []

    for user in client.paginate("/api/v2/users", "users", params={"role[]": ["admin", "agent"]}):
        custom_role_id = user.get("custom_role_id")
        custom_role = role_map.get(custom_role_id, {})

        # Extract permission names from the custom role configuration
        permissions = ""
        if custom_role.get("configuration"):
            config = custom_role["configuration"]
            enabled = [k for k, v in config.items() if v is True]
            permissions = "; ".join(sorted(enabled))

        rows.append({
            "id": user["id"],
            "name": user.get("name", ""),
            "email": user.get("email", ""),
            "role": user.get("role", ""),
            "custom_role_id": custom_role_id or "",
            "custom_role_name": custom_role.get("name", ""),
            "custom_role_permissions": permissions,
            "last_login_at": user.get("last_login_at", ""),
            "two_factor_auth_enabled": user.get("two_factor_auth_enabled", ""),
            "active": user.get("active", ""),
            "suspended": user.get("suspended", ""),
            "created_at": user.get("created_at", ""),
            "updated_at": user.get("updated_at", ""),
        })

    return rows


def main():
    parser = base_parser(
        "User Permission Snapshotter\n\n"
        "Exports a CSV of all Admin and Agent users with their\n"
        "last login date and custom role permissions for security audits."
    )
    parser.add_argument(
        "--output", "-o",
        default="user_permissions_snapshot.csv",
        help="Output CSV file path (default: user_permissions_snapshot.csv)",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    config = load_config(args.env_file)
    client = ZendeskClient(config)

    print("Fetching custom roles...")
    role_map = fetch_custom_roles(client)
    if role_map:
        print(f"Found {len(role_map)} custom role(s).")

    print("Fetching admin and agent users...")
    rows = snapshot_users(client, role_map)

    print(f"\nSnapshot summary:")
    admins = sum(1 for r in rows if r["role"] == "admin")
    agents = sum(1 for r in rows if r["role"] == "agent")
    print(f"  - Admins: {admins}")
    print(f"  - Agents: {agents}")
    print(f"  - Total:  {len(rows)}")

    never_logged_in = sum(1 for r in rows if not r["last_login_at"])
    if never_logged_in:
        print(f"  - Never logged in: {never_logged_in}")

    suspended = sum(1 for r in rows if r["suspended"] is True)
    if suspended:
        print(f"  - Suspended: {suspended}")

    write_csv(args.output, rows, fieldnames=CSV_FIELDS)


if __name__ == "__main__":
    main()
