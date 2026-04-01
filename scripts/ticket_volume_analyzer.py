#!/usr/bin/env python3
"""Ticket Volume Analyzer

Pulls ticket data from the Zendesk Search API for a configurable date range,
breaks it down by channel, brand, priority, and time period, then generates:
  - A multi-page PDF report with charts and summary tables
  - A CSV export of all ticket data

Usage:
    python -m scripts.ticket_volume_analyzer --start-date 2026-01-01 --end-date 2026-03-31
    python -m scripts.ticket_volume_analyzer --start-date 2026-01-01 --period monthly --output-dir ./reports
    python -m scripts.ticket_volume_analyzer --start-date 2026-03-01 --period daily -v
"""

import argparse
import logging
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib
matplotlib.use("Agg")  # Headless backend — must precede pyplot import
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from zendesk_admin import ZendeskClient, load_config
from zendesk_admin.cli import base_parser, setup_logging
from zendesk_admin.utils import write_csv

logger = logging.getLogger(__name__)

SEARCH_API_RESULT_LIMIT = 1000

CHANNEL_LABELS = {
    "email": "Email",
    "web": "Web Form",
    "api": "API",
    "chat": "Chat",
    "twitter": "Twitter",
    "facebook": "Facebook",
    "voice": "Phone",
    "mobile": "Mobile",
    "mobile_sdk": "Mobile SDK",
    "closed_ticket": "Closed Ticket",
    "any_channel": "Any Channel",
    "native_messaging": "Messaging",
    "sample_ticket": "Sample Ticket",
}

PRIORITY_COLORS = {
    "urgent": "#e53e3e",
    "high": "#ed8936",
    "normal": "#3182ce",
    "low": "#38a169",
    "none": "#a0aec0",
}

CSV_FIELDNAMES = [
    "ticket_id", "created_at", "subject", "channel", "brand_name",
    "priority", "status", "via_source",
]


# ---------------------------------------------------------------------------
# Argument parsing helpers
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> date:
    """Parse a YYYY-MM-DD string to a date object."""
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid date format: {s!r}. Use YYYY-MM-DD."
        )


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_brands(client: ZendeskClient) -> dict:
    """Fetch all brands and return a mapping of brand_id -> brand_name."""
    brand_map = {}
    for brand in client.paginate("/api/v2/brands", "brands"):
        brand_map[brand["id"]] = brand.get("name", f"Brand {brand['id']}")
    return brand_map


def fetch_tickets(
    client: ZendeskClient,
    start_date: date,
    end_date: date,
) -> list[dict]:
    """Fetch all tickets created in [start_date, end_date) via the Search API.

    Automatically splits the date range if the 1000-result API limit is hit,
    using recursive bisection. Deduplicates by ticket ID.

    Args:
        client: Authenticated ZendeskClient instance.
        start_date: Inclusive start date.
        end_date: Exclusive end date.

    Returns:
        Deduplicated list of ticket dicts.
    """
    seen_ids = set()
    results = []

    def _fetch_range(s: date, e: date):
        query = f"type:ticket created>={s} created<{e}"
        batch = []
        for ticket in client.paginate(
            "/api/v2/search.json", "results", params={"query": query}
        ):
            batch.append(ticket)

        logger.debug("Query '%s' returned %d results", query, len(batch))

        # If we hit the limit and can split further, bisect
        if len(batch) >= SEARCH_API_RESULT_LIMIT and (e - s).days > 1:
            mid = s + (e - s) // 2
            logger.info(
                "Hit %d-result limit for %s to %s. Bisecting at %s.",
                SEARCH_API_RESULT_LIMIT, s, e, mid,
            )
            _fetch_range(s, mid)
            _fetch_range(mid, e)
            return

        if len(batch) >= SEARCH_API_RESULT_LIMIT:
            logger.warning(
                "Date %s returned %d results — some tickets may be missing.",
                s, len(batch),
            )

        for ticket in batch:
            tid = ticket["id"]
            if tid not in seen_ids:
                seen_ids.add(tid)
                results.append(ticket)

    _fetch_range(start_date, end_date)
    return results


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------

def extract_ticket_data(
    tickets: list[dict],
    brand_map: dict,
) -> list[dict]:
    """Extract and normalize relevant fields from raw ticket dicts."""
    rows = []
    for t in tickets:
        via = t.get("via", {})
        channel_raw = via.get("channel", "unknown")
        channel = CHANNEL_LABELS.get(channel_raw, channel_raw)

        brand_id = t.get("brand_id")
        brand_name = brand_map.get(brand_id, f"Unknown Brand (id={brand_id})")

        rows.append({
            "ticket_id": t["id"],
            "created_at": t.get("created_at", ""),
            "subject": t.get("subject", "(no subject)"),
            "channel": channel,
            "brand_name": brand_name,
            "priority": t.get("priority") or "none",
            "status": t.get("status", "unknown"),
            "via_source": channel_raw,
        })
    return rows


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate_by_field(data: list[dict], field: str) -> dict:
    """Count occurrences of each value for the given field, sorted desc."""
    counts = defaultdict(int)
    for row in data:
        counts[row[field]] += 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def _time_bucket(created_at: str, period: str) -> str:
    """Convert a created_at timestamp to a time bucket string."""
    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if period == "daily":
        return dt.strftime("%Y-%m-%d")
    elif period == "weekly":
        iso_year, iso_week, _ = dt.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    else:  # monthly
        return dt.strftime("%Y-%m")


def aggregate_by_time_and_field(
    data: list[dict],
    period: str,
    field: str,
) -> dict:
    """Group data by time bucket and field value.

    Returns:
        {time_bucket: {field_value: count}}, sorted chronologically.
    """
    result = defaultdict(lambda: defaultdict(int))
    for row in data:
        bucket = _time_bucket(row["created_at"], period)
        result[bucket][row[field]] += 1
    return dict(sorted(result.items()))


def compute_hourly_heatmap(data: list[dict]) -> list[list[int]]:
    """Build a 7x24 matrix (Monday..Sunday x 0..23 hour) of ticket counts."""
    matrix = [[0] * 24 for _ in range(7)]
    for row in data:
        if not row["created_at"]:
            continue
        dt = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        matrix[dt.weekday()][dt.hour] += 1
    return matrix


# ---------------------------------------------------------------------------
# Chart generation (matplotlib)
# ---------------------------------------------------------------------------

def _top_n_with_other(counts: dict, n: int = 8) -> dict:
    """Keep top N items, group the rest as 'Other'."""
    items = list(counts.items())
    if len(items) <= n:
        return counts
    top = dict(items[:n])
    other = sum(v for _, v in items[n:])
    if other > 0:
        top["Other"] = other
    return top


def chart_volume_over_time(
    time_field_data: dict,
    period: str,
    temp_dir: str,
) -> str:
    """Line chart of total ticket volume per time bucket."""
    buckets = list(time_field_data.keys())
    totals = [sum(v.values()) for v in time_field_data.values()]

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(buckets, totals, marker="o", linewidth=2, color="#3182ce")
    ax.fill_between(range(len(buckets)), totals, alpha=0.1, color="#3182ce")
    ax.set_xticks(range(len(buckets)))
    ax.set_xticklabels(buckets, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Ticket Count")
    ax.set_title(f"Ticket Volume Over Time ({period.title()})")
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()

    path = str(Path(temp_dir) / "volume_over_time.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def chart_stacked_bar_time(
    time_field_data: dict,
    field_label: str,
    period: str,
    temp_dir: str,
    filename: str,
    color_map: dict | None = None,
) -> str:
    """Stacked bar chart showing volume per time bucket segmented by field."""
    buckets = list(time_field_data.keys())

    # Determine top categories across all buckets
    totals = defaultdict(int)
    for bucket_data in time_field_data.values():
        for k, v in bucket_data.items():
            totals[k] += v
    sorted_cats = sorted(totals, key=lambda k: -totals[k])
    top_cats = sorted_cats[:8]
    has_other = len(sorted_cats) > 8

    fig, ax = plt.subplots(figsize=(10, 5))
    x = range(len(buckets))
    bottom = [0] * len(buckets)

    for cat in top_cats:
        values = [time_field_data[b].get(cat, 0) for b in buckets]
        color = color_map.get(cat) if color_map else None
        ax.bar(x, values, bottom=bottom, label=cat, color=color, width=0.7)
        bottom = [b + v for b, v in zip(bottom, values)]

    if has_other:
        other_vals = []
        for b in buckets:
            other = sum(
                v for k, v in time_field_data[b].items() if k not in top_cats
            )
            other_vals.append(other)
        ax.bar(x, other_vals, bottom=bottom, label="Other", color="#a0aec0", width=0.7)

    ax.set_xticks(x)
    ax.set_xticklabels(buckets, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Ticket Count")
    ax.set_title(f"Tickets by {field_label} Over Time ({period.title()})")
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.legend(loc="upper left", fontsize=7, ncol=2)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()

    path = str(Path(temp_dir) / f"{filename}.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def chart_pie(counts: dict, title: str, temp_dir: str, filename: str) -> str:
    """Pie chart for a single dimension."""
    data = _top_n_with_other(counts, n=8)
    labels = list(data.keys())
    values = list(data.values())
    total = sum(values)

    fig, ax = plt.subplots(figsize=(7, 5))
    wedges, texts, autotexts = ax.pie(
        values,
        labels=None,
        autopct=lambda p: f"{p:.1f}%" if p >= 3 else "",
        startangle=90,
        pctdistance=0.8,
    )
    ax.legend(
        wedges,
        [f"{l} ({v:,})" for l, v in zip(labels, values)],
        loc="center left",
        bbox_to_anchor=(1, 0.5),
        fontsize=8,
    )
    ax.set_title(title)
    fig.tight_layout()

    path = str(Path(temp_dir) / f"{filename}.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def chart_bar(counts: dict, title: str, temp_dir: str, filename: str) -> str:
    """Horizontal bar chart for a single dimension."""
    # Show top 15 at most
    items = list(counts.items())[:15]
    labels = [l for l, _ in reversed(items)]
    values = [v for _, v in reversed(items)]

    fig, ax = plt.subplots(figsize=(8, max(3, len(labels) * 0.4)))
    bars = ax.barh(labels, values, color="#3182ce")
    ax.bar_label(bars, fmt="%d", padding=3, fontsize=8)
    ax.set_xlabel("Ticket Count")
    ax.set_title(title)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()

    path = str(Path(temp_dir) / f"{filename}.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def chart_heatmap(matrix: list[list[int]], temp_dir: str) -> str:
    """Heatmap of ticket creation by day-of-week and hour-of-day."""
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    hours = [f"{h:02d}:00" for h in range(24)]

    fig, ax = plt.subplots(figsize=(12, 4))
    im = ax.imshow(matrix, aspect="auto", cmap="YlOrRd")
    ax.set_xticks(range(24))
    ax.set_xticklabels(hours, rotation=45, ha="right", fontsize=7)
    ax.set_yticks(range(7))
    ax.set_yticklabels(days, fontsize=9)
    ax.set_title("Ticket Creation Heatmap (Day of Week x Hour of Day, UTC)")
    fig.colorbar(im, ax=ax, shrink=0.8, label="Ticket Count")
    fig.tight_layout()

    path = str(Path(temp_dir) / "heatmap.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


# ---------------------------------------------------------------------------
# PDF report generation (reportlab)
# ---------------------------------------------------------------------------

def _build_summary_table(counts: dict, label: str, total: int) -> Table:
    """Build a reportlab Table with header, rows, and percentage column."""
    header = [label, "Count", "%"]
    rows = [header]
    for name, count in counts.items():
        pct = f"{count / total * 100:.1f}%" if total else "0%"
        rows.append([name, f"{count:,}", pct])

    table = Table(rows, colWidths=[2.5 * inch, 1 * inch, 0.8 * inch])
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2d3748")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    # Alternating row colors
    for i in range(1, len(rows)):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f7fafc")))

    table.setStyle(TableStyle(style))
    return table


def generate_pdf_report(
    data: list[dict],
    aggregations: dict,
    chart_paths: dict,
    output_path: str,
    start_date: date,
    end_date: date,
    period: str,
):
    """Generate a multi-page PDF report with charts and summary tables."""
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle", parent=styles["Title"],
        fontSize=24, spaceAfter=20,
    )
    heading_style = ParagraphStyle(
        "ReportHeading", parent=styles["Heading2"],
        fontSize=16, spaceAfter=10, spaceBefore=15,
    )
    body_style = ParagraphStyle(
        "ReportBody", parent=styles["Normal"],
        fontSize=10, spaceAfter=8, leading=14,
    )
    metric_style = ParagraphStyle(
        "ReportMetric", parent=styles["Normal"],
        fontSize=12, spaceAfter=4, leading=16,
    )

    total = len(data)
    agg_channel = aggregations["channel"]
    agg_brand = aggregations["brand"]
    agg_priority = aggregations["priority"]
    agg_status = aggregations["status"]

    top_channel = next(iter(agg_channel), "N/A")
    top_brand = next(iter(agg_brand), "N/A")

    doc = SimpleDocTemplate(
        output_path,
        pagesize=landscape(letter),
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
    )

    flowables = []

    # --- Page 1: Title & Executive Summary ---
    flowables.append(Spacer(1, 0.5 * inch))
    flowables.append(Paragraph("Ticket Volume Analysis Report", title_style))
    flowables.append(Spacer(1, 0.15 * inch))
    flowables.append(Paragraph(
        f"Date Range: {start_date} to {end_date} &nbsp;|&nbsp; "
        f"Period: {period.title()} &nbsp;|&nbsp; "
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        body_style,
    ))
    flowables.append(Spacer(1, 0.3 * inch))

    flowables.append(Paragraph("Key Metrics", heading_style))
    metrics = [
        f"<b>Total Tickets:</b> {total:,}",
        f"<b>Channels:</b> {len(agg_channel)}",
        f"<b>Brands:</b> {len(agg_brand)}",
        f"<b>Date Range Span:</b> {(end_date - start_date).days} days",
        f"<b>Top Channel:</b> {top_channel} ({agg_channel.get(top_channel, 0):,} tickets, "
        f"{agg_channel.get(top_channel, 0) / total * 100:.1f}%)" if total else "",
        f"<b>Top Brand:</b> {top_brand} ({agg_brand.get(top_brand, 0):,} tickets, "
        f"{agg_brand.get(top_brand, 0) / total * 100:.1f}%)" if total else "",
    ]
    for m in metrics:
        if m:
            flowables.append(Paragraph(m, metric_style))

    flowables.append(PageBreak())

    # --- Page 2: Summary Tables ---
    flowables.append(Paragraph("Summary Tables", heading_style))
    flowables.append(Spacer(1, 0.1 * inch))

    flowables.append(Paragraph("Channel Breakdown", body_style))
    flowables.append(_build_summary_table(agg_channel, "Channel", total))
    flowables.append(Spacer(1, 0.25 * inch))

    flowables.append(Paragraph("Brand Breakdown", body_style))
    flowables.append(_build_summary_table(agg_brand, "Brand", total))
    flowables.append(Spacer(1, 0.25 * inch))

    flowables.append(Paragraph("Priority Breakdown", body_style))
    flowables.append(_build_summary_table(agg_priority, "Priority", total))
    flowables.append(Spacer(1, 0.25 * inch))

    flowables.append(Paragraph("Status Breakdown", body_style))
    flowables.append(_build_summary_table(agg_status, "Status", total))

    flowables.append(PageBreak())

    # --- Page 3: Volume Over Time ---
    flowables.append(Paragraph("Ticket Volume Over Time", heading_style))
    if "volume_over_time" in chart_paths:
        flowables.append(Image(chart_paths["volume_over_time"], width=9 * inch, height=4.2 * inch))

    flowables.append(PageBreak())

    # --- Page 4: Channel Analysis ---
    flowables.append(Paragraph("Channel Analysis", heading_style))
    if "channel_pie" in chart_paths:
        flowables.append(Image(chart_paths["channel_pie"], width=6.5 * inch, height=4 * inch))
    flowables.append(Spacer(1, 0.15 * inch))
    if "channel_time" in chart_paths:
        flowables.append(Image(chart_paths["channel_time"], width=9 * inch, height=4.2 * inch))

    flowables.append(PageBreak())

    # --- Page 5: Brand Analysis ---
    flowables.append(Paragraph("Brand Analysis", heading_style))
    if "brand_pie" in chart_paths:
        flowables.append(Image(chart_paths["brand_pie"], width=6.5 * inch, height=4 * inch))
    flowables.append(Spacer(1, 0.15 * inch))
    if "brand_time" in chart_paths:
        flowables.append(Image(chart_paths["brand_time"], width=9 * inch, height=4.2 * inch))

    flowables.append(PageBreak())

    # --- Page 6: Priority Analysis ---
    flowables.append(Paragraph("Priority Analysis", heading_style))
    if "priority_bar" in chart_paths:
        flowables.append(Image(chart_paths["priority_bar"], width=7 * inch, height=3.5 * inch))
    flowables.append(Spacer(1, 0.15 * inch))
    if "priority_time" in chart_paths:
        flowables.append(Image(chart_paths["priority_time"], width=9 * inch, height=4.2 * inch))

    flowables.append(PageBreak())

    # --- Page 7: Hourly Heatmap ---
    flowables.append(Paragraph("Ticket Creation Heatmap", heading_style))
    flowables.append(Paragraph(
        "Shows the distribution of ticket creation across days of the week "
        "and hours of the day. All times are in UTC.",
        body_style,
    ))
    if "heatmap" in chart_paths:
        flowables.append(Image(chart_paths["heatmap"], width=9.5 * inch, height=3.5 * inch))

    doc.build(flowables)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = base_parser(
        "Ticket Volume Analyzer\n\n"
        "Pulls ticket data for a date range and generates a multi-page\n"
        "PDF report with trend charts and summary tables, plus a CSV export."
    )
    parser.add_argument(
        "--start-date",
        required=True,
        type=_parse_date,
        help="Start date inclusive (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        type=_parse_date,
        help="End date exclusive (YYYY-MM-DD, default: today)",
    )
    parser.add_argument(
        "--period",
        choices=["daily", "weekly", "monthly"],
        default="weekly",
        help="Time bucketing period (default: weekly)",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for output files (default: current directory)",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    start = args.start_date
    end = args.end_date or date.today()

    if start >= end:
        parser.error("--start-date must be before --end-date")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    config = load_config(args.env_file)
    client = ZendeskClient(config)

    # 1. Fetch brands
    print("Fetching brands...")
    brand_map = fetch_brands(client)
    print(f"  Found {len(brand_map)} brand(s).")

    # 2. Fetch tickets
    print(f"Searching tickets from {start} to {end}...")
    raw_tickets = fetch_tickets(client, start, end)
    print(f"  Found {len(raw_tickets)} ticket(s).")

    date_stamp = datetime.now().strftime("%Y%m%d")
    csv_path = str(output_dir / f"ticket_volume_data_{date_stamp}.csv")

    if not raw_tickets:
        print("\nNo tickets found in the specified date range. Nothing to report.")
        write_csv(csv_path, [], CSV_FIELDNAMES)
        return

    # 3. Extract normalized data
    data = extract_ticket_data(raw_tickets, brand_map)

    # 4. Aggregate
    agg_channel = aggregate_by_field(data, "channel")
    agg_brand = aggregate_by_field(data, "brand_name")
    agg_priority = aggregate_by_field(data, "priority")
    agg_status = aggregate_by_field(data, "status")
    time_channel = aggregate_by_time_and_field(data, args.period, "channel")
    time_brand = aggregate_by_time_and_field(data, args.period, "brand_name")
    time_priority = aggregate_by_time_and_field(data, args.period, "priority")
    heatmap_matrix = compute_hourly_heatmap(data)

    # 5. Generate charts in temp dir, build PDF
    pdf_path = str(output_dir / f"ticket_volume_report_{date_stamp}.pdf")

    print("Generating charts and PDF report...")
    with tempfile.TemporaryDirectory() as tmp:
        chart_paths = {
            "volume_over_time": chart_volume_over_time(
                time_channel, args.period, tmp,
            ),
            "channel_pie": chart_pie(
                agg_channel, "Tickets by Channel", tmp, "channel_pie",
            ),
            "channel_time": chart_stacked_bar_time(
                time_channel, "Channel", args.period, tmp, "channel_time",
            ),
            "brand_pie": chart_pie(
                agg_brand, "Tickets by Brand", tmp, "brand_pie",
            ),
            "brand_time": chart_stacked_bar_time(
                time_brand, "Brand", args.period, tmp, "brand_time",
            ),
            "priority_bar": chart_bar(
                agg_priority, "Tickets by Priority", tmp, "priority_bar",
            ),
            "priority_time": chart_stacked_bar_time(
                time_priority, "Priority", args.period, tmp, "priority_time",
                color_map=PRIORITY_COLORS,
            ),
            "heatmap": chart_heatmap(heatmap_matrix, tmp),
        }

        generate_pdf_report(
            data=data,
            aggregations={
                "channel": agg_channel,
                "brand": agg_brand,
                "priority": agg_priority,
                "status": agg_status,
            },
            chart_paths=chart_paths,
            output_path=pdf_path,
            start_date=start,
            end_date=end,
            period=args.period,
        )

    print(f"  PDF report: {pdf_path}")

    # 6. Write CSV
    write_csv(csv_path, data, fieldnames=CSV_FIELDNAMES)

    # 7. Console summary
    total = len(data)
    top_channel = next(iter(agg_channel), "N/A")
    top_brand = next(iter(agg_brand), "N/A")

    print(f"\n--- Summary ---")
    print(f"  Date range:     {start} to {end}")
    print(f"  Period:         {args.period}")
    print(f"  Total tickets:  {total:,}")
    print(f"  Channels:       {len(agg_channel)}")
    print(f"  Brands:         {len(agg_brand)}")
    print(f"  Top channel:    {top_channel} ({agg_channel.get(top_channel, 0):,} tickets)")
    print(f"  Top brand:      {top_brand} ({agg_brand.get(top_brand, 0):,} tickets)")


if __name__ == "__main__":
    main()
