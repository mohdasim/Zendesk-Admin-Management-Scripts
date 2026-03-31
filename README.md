# Zendesk Admin Management Scripts

A modular collection of Python scripts for automating common Zendesk administration tasks. Built for support teams that need to audit, maintain, and bulk-manage their Zendesk instance beyond what the native UI offers.

## Scripts

| Script | Purpose |
|--------|---------|
| [Zombie Trigger Auditor](#1-zombie-trigger-auditor) | Find triggers/automations with zero usage |
| [Bulk Macro Search & Replace](#2-bulk-macro-content-search--replace) | Search and replace text across all macros |
| [User Permission Snapshotter](#3-user-permission-snapshotter) | Export admin/agent users to CSV for audits |
| [Tag Cleanup Bot](#4-tag-cleanup-bot) | Identify orphan tags for consolidation |

---

## Architecture

All scripts share a common library (`zendesk_admin/`) that handles authentication, API requests, pagination, and rate limiting. This makes it easy to add new scripts without duplicating boilerplate.

```mermaid
graph TD
    subgraph "scripts/"
        A[zombie_trigger_auditor.py]
        B[bulk_macro_search_replace.py]
        C[user_permission_snapshotter.py]
        D[tag_cleanup_bot.py]
        E[your_new_script.py]
    end

    subgraph "zendesk_admin/"
        F["config.py<br/>(load credentials)"]
        G["client.py<br/>(API client)"]
        H["cli.py<br/>(argument parsing)"]
        I["utils.py<br/>(CSV & JSON output)"]
    end

    A & B & C & D & E --> F & G & H & I

    G -->|"HTTP requests<br/>with auth"| J[Zendesk REST API v2]
    G -->|"handles"| K["Rate Limiting<br/>(429 + Retry-After)"]
    G -->|"handles"| L["Pagination<br/>(cursor + offset)"]
```

### Request Flow

```mermaid
sequenceDiagram
    participant Script
    participant ZendeskClient
    participant ZendeskAPI

    Script->>ZendeskClient: paginate("/api/v2/triggers", "triggers")
    loop Each page
        ZendeskClient->>ZendeskAPI: GET /api/v2/triggers?page[size]=100
        alt 200 OK
            ZendeskAPI-->>ZendeskClient: {triggers: [...], meta: {has_more}, links: {next}}
            ZendeskClient-->>Script: yield individual triggers
        else 429 Rate Limited
            ZendeskAPI-->>ZendeskClient: 429 + Retry-After: 30
            Note over ZendeskClient: Sleep for Retry-After seconds
            ZendeskClient->>ZendeskAPI: Retry same request
        end
    end
```

---

## Quick Start

### Prerequisites

- Python 3.9+
- A Zendesk account with **Admin** access
- An API token (Admin Center → Apps and Integrations → APIs → Zendesk API)

### Installation

```bash
# Clone the repository
git clone https://github.com/mohdasim/Zendesk-Admin-Management-Scripts.git
cd Zendesk-Admin-Management-Scripts

# Install dependencies
pip install -r requirements.txt

# Configure credentials
cp .env.example .env
# Edit .env with your Zendesk subdomain, email, and API token
```

### Configuration

Edit the `.env` file with your Zendesk credentials:

```env
ZENDESK_SUBDOMAIN=yourcompany        # yourcompany.zendesk.com
ZENDESK_EMAIL=admin@yourcompany.com  # Admin email address
ZENDESK_API_TOKEN=your_api_token     # From Admin Center
```

All scripts read credentials from the `.env` file by default. Use `--env-file` to specify a different path.

---

## Script Details

### 1. Zombie Trigger Auditor

Identifies triggers and automations with **zero usage** over a configurable time period. Uses Zendesk's built-in usage statistics (`usage_1h`, `usage_24h`, `usage_7d`, `usage_30d`) for accurate detection.

```mermaid
flowchart TD
    A[Start] --> B[Load Configuration]
    B --> C[Fetch Triggers<br/>with usage stats]
    C --> D[Fetch Automations<br/>with usage stats]
    D --> E{Check usage for<br/>selected period}
    E -->|usage == 0| F[Add to zombie list]
    E -->|usage > 0| G[Skip - actively firing]
    F --> H[Generate Report]
    G --> H
    H --> I[Output JSON report<br/>+ console summary]
```

#### Usage

```bash
# Find triggers/automations with zero usage in the last 7 days (default)
python -m scripts.zombie_trigger_auditor

# Check for zero usage in the last 30 days
python -m scripts.zombie_trigger_auditor --period 30d

# Include disabled triggers in the report
python -m scripts.zombie_trigger_auditor --include-inactive

# Save report to file
python -m scripts.zombie_trigger_auditor --period 30d -o zombies.json

# Enable debug logging
python -m scripts.zombie_trigger_auditor -v
```

#### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--period` | `7d` | Usage period: `1h`, `24h`, `7d`, or `30d` |
| `--include-inactive` | off | Include disabled triggers/automations |
| `--output`, `-o` | stdout | Save JSON report to file |
| `--verbose`, `-v` | off | Enable debug logging |
| `--env-file` | `.env` | Path to credentials file |

#### Sample Output

```
Auditing triggers and automations with zero usage in the last 7 days...

Found 3 zombie items (zero usage in 7 days):
  - Triggers: 2
  - Automations: 1

Type         ID                   Active   Title
--------------------------------------------------------------------------------
trigger      39147977549975       True     Auto-Close Walmart Notifications
trigger      25960167241239       True     Close Ticket
automation   21290448620695       True     Resolve stale pending tickets
```

---

### 2. Bulk Macro Content Search & Replace

Searches for specific text or URLs across all macro action values and optionally replaces them. Supports **dry-run mode** for safe previewing before making changes.

```mermaid
flowchart TD
    A[Start] --> B[Load Configuration]
    B --> C[Fetch all Macros]
    C --> D[Search action values<br/>for --search text]
    D --> E{Matches found?}
    E -->|No| F[Report: 0 matches]
    E -->|Yes| G{--replace provided?}
    G -->|No| H[Report matches<br/>search-only mode]
    G -->|Yes| I{--dry-run?}
    I -->|Yes| J[Preview changes<br/>no API writes]
    I -->|No| K[PUT updated macros<br/>via API]
    K --> L[Report updated macros]
```

#### Usage

```bash
# Search only - find macros containing a URL
python -m scripts.bulk_macro_search_replace --search "help.oldcompany.com"

# Preview replacements (dry run)
python -m scripts.bulk_macro_search_replace \
  --search "help.oldcompany.com" \
  --replace "help.newcompany.com" \
  --dry-run

# Apply replacements
python -m scripts.bulk_macro_search_replace \
  --search "help.oldcompany.com" \
  --replace "help.newcompany.com"

# Save match report to file
python -m scripts.bulk_macro_search_replace --search "old-brand" -o matches.json
```

#### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--search`, `-s` | *(required)* | Text or URL to find in macros |
| `--replace`, `-r` | *(none)* | Replacement text (omit for search-only) |
| `--dry-run` | off | Preview changes without applying |
| `--output`, `-o` | stdout | Save match report to JSON file |
| `--verbose`, `-v` | off | Enable debug logging |
| `--env-file` | `.env` | Path to credentials file |

---

### 3. User Permission Snapshotter

Exports a CSV of all **Admin** and **Agent** users with their last login date, custom role name, and permissions. Designed for monthly security audits.

```mermaid
flowchart TD
    A[Start] --> B[Load Configuration]
    B --> C[Fetch Custom Roles<br/>into lookup dict]
    C --> D[Fetch Users<br/>filtered: admin + agent]
    D --> E[Join user with<br/>custom role details]
    E --> F[Extract permission<br/>names from role config]
    F --> G[Write CSV file]
    G --> H[Print summary:<br/>admins, agents, never logged in]
```

#### Usage

```bash
# Export to default file (user_permissions_snapshot.csv)
python -m scripts.user_permission_snapshotter

# Export to custom file path
python -m scripts.user_permission_snapshotter -o audit_march_2026.csv
```

#### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--output`, `-o` | `user_permissions_snapshot.csv` | Output CSV file path |
| `--verbose`, `-v` | off | Enable debug logging |
| `--env-file` | `.env` | Path to credentials file |

#### CSV Columns

| Column | Description |
|--------|-------------|
| `id` | Zendesk user ID |
| `name` | Full name |
| `email` | Email address |
| `role` | `admin` or `agent` |
| `custom_role_id` | Custom role ID (Enterprise+) |
| `custom_role_name` | Custom role name |
| `custom_role_permissions` | Semicolon-separated list of enabled permissions |
| `last_login_at` | Last login timestamp (ISO 8601) |
| `two_factor_auth_enabled` | Whether 2FA is enabled |
| `active` | Whether the user is active |
| `suspended` | Whether the user is suspended |
| `created_at` | Account creation date |
| `updated_at` | Last profile update date |

---

### 4. Tag Cleanup Bot

Identifies **orphan tags** -- tags that exist on tickets but are not referenced in any Trigger, Automation, or View. Generates a report for tag consolidation.

```mermaid
flowchart TD
    A[Start] --> B[Load Configuration]
    B --> C[Step 1: Collect tags<br/>from tickets via Tags API]
    C --> D[Step 2: Scan Triggers<br/>for tag references]
    D --> E[Step 2: Scan Automations<br/>for tag references]
    E --> F[Step 2: Scan Views<br/>for tag references]
    F --> G[Step 3: Compare<br/>ticket tags vs referenced tags]
    G --> H{Tag in any<br/>business rule?}
    H -->|Yes| I[Referenced - skip]
    H -->|No| J[Orphan tag]
    J --> K[Filter by<br/>--min-tickets threshold]
    K --> L[Sort by ticket<br/>count descending]
    L --> M[Output report]
```

#### Usage

```bash
# Find all orphan tags
python -m scripts.tag_cleanup_bot

# Only report orphan tags on 5+ tickets
python -m scripts.tag_cleanup_bot --min-tickets 5

# Save report to file
python -m scripts.tag_cleanup_bot -o orphan_tags.json

# Verbose output to see API calls
python -m scripts.tag_cleanup_bot -v
```

#### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--min-tickets` | `1` | Minimum ticket count to include in report |
| `--output`, `-o` | stdout | Save JSON report to file |
| `--verbose`, `-v` | off | Enable debug logging |
| `--env-file` | `.env` | Path to credentials file |

#### Sample Output

```
Step 1: Collecting tags from tickets...
  Found 342 tags on tickets

Step 2: Scanning business rules for tag references...
  Scanned 21 triggers
  Scanned 5 automations
  Scanned 15 views

  Total unique tags referenced in business rules: 28

Step 3: Identifying orphan tags...

Results:
  Tags on tickets:              342
  Tags in business rules:       28
  Orphan tags (>= 1 tickets):   314

Tag                                      Ticket Count
------------------------------------------------------
legacy_import                                     1847
old_category_electronics                           523
temp_migration_batch2                              201
...
```

---

## Adding a New Script

The project is designed for easy extension. To add a new script:

1. **Create a new file** in `scripts/`:

```python
#!/usr/bin/env python3
"""Description of your new script."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zendesk_admin import ZendeskClient, load_config
from zendesk_admin.cli import base_parser, setup_logging
from zendesk_admin.utils import print_json_report  # or write_csv


def main():
    parser = base_parser("Your Script Description")
    # Add script-specific arguments
    parser.add_argument("--your-flag", help="...")

    args = parser.parse_args()
    setup_logging(args.verbose)

    config = load_config(args.env_file)
    client = ZendeskClient(config)

    # Use client.get(), client.put(), or client.paginate()
    for item in client.paginate("/api/v2/endpoint", "items"):
        # Process items
        pass


if __name__ == "__main__":
    main()
```

2. **Run it**:
```bash
python -m scripts.your_new_script --help
```

The shared `ZendeskClient` handles authentication, pagination, and rate limiting automatically.

### Available Client Methods

| Method | Description |
|--------|-------------|
| `client.get(endpoint, params)` | GET request, returns JSON dict |
| `client.put(endpoint, json)` | PUT request, returns JSON dict |
| `client.paginate(endpoint, key, params)` | Yields individual records from paginated endpoint |

---

## API Rate Limits

Zendesk enforces rate limits on API usage. The `ZendeskClient` handles this automatically:

- **Detection**: Monitors for HTTP `429 Too Many Requests` responses
- **Backoff**: Waits for the duration specified in the `Retry-After` header
- **Retry**: Retries up to 5 times before raising a `RateLimitError`
- **Pagination**: Uses cursor-based pagination (preferred) with offset fallback

### Rate Limit Guidelines

| Plan | Limit |
|------|-------|
| Team | 200 requests/minute |
| Professional | 400 requests/minute |
| Enterprise | 700 requests/minute |

For large Zendesk instances, consider running scripts during off-peak hours.

---

## Limitations

### Zombie Trigger Auditor
- Usage statistics (`usage_1h`, `usage_24h`, `usage_7d`, `usage_30d`) are provided by Zendesk and may not be available on all plan tiers.
- The `usage_30d` field only covers the last 30 days. A trigger that fires once every 60 days would still appear as a zombie.
- Automations may not support the `include=usage_*` parameter on all Zendesk plans.

### Bulk Macro Search & Replace
- Searches action values only (not macro titles or descriptions).
- Macro action values can be strings, lists of strings, or nested structures. The script handles strings and lists but not deeply nested structures.
- No undo mechanism. Always use `--dry-run` first and keep backups.

### User Permission Snapshotter
- Custom roles and their permissions require an **Enterprise+** plan. On lower plans, the `custom_role_name` and `custom_role_permissions` columns will be empty.
- The `role[]` filter fetches admins and agents only. End-users are excluded by design.
- The `two_factor_auth_enabled` field may not be available on all plans.

### Tag Cleanup Bot
- The Tags API returns **up to 20,000 most popular tags from the last 60 days**. Rarely-used tags or tags older than 60 days may not appear.
- Tags referenced only in **Macros** or **SLA policies** are not checked (only Triggers, Automations, and Views are scanned).
- Tag names are compared as exact strings (case-sensitive).

### General
- All scripts require Admin-level API access.
- API token authentication only (OAuth not supported).
- Rate limits vary by Zendesk plan tier (see [Rate Limits](#api-rate-limits)).
- Scripts run synchronously. Large instances with thousands of triggers, macros, or users may take several minutes.

---

## Project Structure

```
Zendesk-Admin-Management-Scripts/
├── .env.example            # Template for credentials
├── .gitignore              # Python, IDE, and output file exclusions
├── LICENSE                 # MIT License
├── README.md               # This file
├── requirements.txt        # Python dependencies
├── zendesk_admin/          # Shared library
│   ├── __init__.py         # Package exports
│   ├── client.py           # ZendeskClient (auth, pagination, rate limiting)
│   ├── cli.py              # Shared CLI argument parser
│   ├── config.py           # Configuration loading from .env
│   └── utils.py            # CSV and JSON output helpers
└── scripts/                # Runnable admin scripts
    ├── __init__.py
    ├── zombie_trigger_auditor.py
    ├── bulk_macro_search_replace.py
    ├── user_permission_snapshotter.py
    └── tag_cleanup_bot.py
```

---

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
