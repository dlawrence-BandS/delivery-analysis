"""
fetch_data.py — B&S Delivery Impact Analysis: BigQuery → JSON
------------------------------------------------------------
Pulls weekly GA4 data from BigQuery and writes JSON files for the
delivery-impact dashboard (index.html) to consume.

SETUP:
  pip install google-cloud-bigquery pandas

USAGE:
  python fetch_data.py --key path/to/service-account.json

  Or set GOOGLE_APPLICATION_CREDENTIALS env var and run without --key.

OUTPUT (all written to ./data/):
  category_weekly.json       — weekly sessions/revenue/cvr by item_category
  subcategory_weekly.json    — weekly sessions/revenue/cvr by item_category2
  product_weekly.json        — weekly sessions/revenue/cvr by item_name (top 500)
  funnel_weekly.json         — weekly checkout funnel step counts
  meta.json                  — last_updated timestamp, date range covered
"""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from google.cloud import bigquery
from google.oauth2 import service_account

# ─── CONFIG ──────────────────────────────────────────────────────────────────

PROJECT_ID  = "commanding-air-450109-p0"
DATASET     = "analytics_287404213"

# GA4 events table — uses the wildcard to cover all dates
EVENTS_TABLE = f"`{PROJECT_ID}.{DATASET}.events_*`"

# Output directory (relative to this script)
OUTPUT_DIR = Path(__file__).parent / "data"

# ─── DELIVERY CHANGE ANNOTATIONS (shared with dashboard via meta.json) ────────

DELIVERY_CHANGES = [
    {
        "date": "2023-08-01",
        "label": "New delivery structure",
        "detail": "Small courier £29 / Medium B&S £49 / Large B&S £79 / Dining ≤3 £9 / Dining ≥4 £19"
    },
    {
        "date": "2024-12-01",
        "label": "Dining chairs increase",
        "detail": "Dining ≥4 chairs £19→£29. DPD Freight items (oversized/heavy) also defaulted to £29 for ≤3 chairs."
    },
    {
        "date": "2025-04-01",
        "label": "Medium & Large increase",
        "detail": "Medium B&S £49→£59 / Large B&S £79→£89. DX 2-crew medium remains £49."
    },
    {
        "date": "2025-08-01",
        "label": "Small furniture increase",
        "detail": "Small furniture (incl. dining ≤3 chairs) £9→£15 via DPD"
    },
    {
        "date": "2026-03-27",
        "label": "Medium & Large increase",
        "detail": "Medium B&S £59→£69 / Large B&S £89→£99. Current structure: Large £99 / Medium B&S £69 / Medium DX £49 / Mattresses £29 / Small £15 / Accessories £6 / Large accessories £29"
    }
]

# Current delivery structure (post March 2026) for reference in dashboard
CURRENT_DELIVERY = [
    {"tier": "Large furniture",        "price": 99,  "carrier": "B&S",   "examples": "Sofas, sofa beds, wardrobes"},
    {"tier": "Medium furniture",        "price": 69,  "carrier": "B&S",   "examples": "Beds, dining tables, dining sets, sideboards, recliner chairs"},
    {"tier": "Medium furniture",        "price": 49,  "carrier": "DX 2-crew", "examples": "Armchairs, TV units, coffee tables, chest of drawers"},
    {"tier": "Mattresses",              "price": 29,  "carrier": "B&S",   "examples": "All mattresses"},
    {"tier": "Small furniture",         "price": 15,  "carrier": "DPD",   "examples": "Dining chairs (≤3), bar stools, small tables. 4+ dining chairs or heavy items: £29"},
    {"tier": "Small accessories",       "price": 6,   "carrier": "DPD",   "examples": "Table lamps, soft furnishings, clocks, home decor, plants"},
    {"tier": "Large accessories & rugs", "price": 29, "carrier": "DPD",   "examples": "Rugs, ceiling/floor lamps, mirrors, wall art over 1m/30kg"},
    {"tier": "Assisted delivery",       "price": 149, "carrier": "B&S",   "examples": "Items requiring 2+ person delivery due to size/weight"},
    {"tier": "Click & Collect",         "price": 0,   "carrier": "N/A",   "examples": "All furniture and accessories from 4 warehouses or 11 stores"},
]

# ─── QUERIES ─────────────────────────────────────────────────────────────────

# ISO week start (Monday) helper used in all queries
# GA4 event_date is YYYYMMDD string
WEEK_EXPR = "DATE_TRUNC(PARSE_DATE('%Y%m%d', event_date), WEEK(MONDAY))"

CATEGORY_QUERY = f"""
WITH item_events AS (
  SELECT
    {WEEK_EXPR} AS week,
    item.item_category AS category,
    event_name,
    ecommerce.transaction_id AS transaction_id,
    item.price * item.quantity AS item_revenue,
    user_pseudo_id
  FROM {EVENTS_TABLE}
  CROSS JOIN UNNEST(items) AS item
  WHERE _TABLE_SUFFIX BETWEEN '20230101' AND FORMAT_DATE('%Y%m%d', CURRENT_DATE())
    AND event_name IN ('purchase','add_to_cart','view_item','begin_checkout')
    AND item.item_category IS NOT NULL
    AND item.item_category != ''
),
purchases AS (
  SELECT week, category,
    COUNT(DISTINCT transaction_id) AS transactions,
    ROUND(SUM(item_revenue), 2)    AS revenue
  FROM item_events WHERE event_name = 'purchase'
  GROUP BY 1,2
),
atc AS (
  SELECT week, category, COUNT(*) AS add_to_cart
  FROM item_events WHERE event_name = 'add_to_cart'
  GROUP BY 1,2
),
views AS (
  SELECT week, category, COUNT(DISTINCT user_pseudo_id) AS sessions
  FROM item_events WHERE event_name = 'view_item'
  GROUP BY 1,2
),
checkout AS (
  SELECT week, category, COUNT(DISTINCT user_pseudo_id) AS began_checkout
  FROM item_events WHERE event_name = 'begin_checkout'
  GROUP BY 1,2
)
SELECT
  v.week,
  v.category,
  v.sessions,
  COALESCE(p.transactions, 0)   AS transactions,
  COALESCE(p.revenue, 0)        AS revenue,
  COALESCE(a.add_to_cart, 0)    AS add_to_cart,
  COALESCE(c.began_checkout, 0) AS began_checkout,
  ROUND(SAFE_DIVIDE(COALESCE(p.transactions,0), v.sessions) * 100, 3) AS cvr
FROM views v
LEFT JOIN purchases p USING (week, category)
LEFT JOIN atc       a USING (week, category)
LEFT JOIN checkout  c USING (week, category)
ORDER BY v.week, v.category
"""

SUBCATEGORY_QUERY = f"""
WITH item_events AS (
  SELECT
    {WEEK_EXPR} AS week,
    item.item_category  AS category,
    item.item_category2 AS subcategory,
    event_name,
    ecommerce.transaction_id AS transaction_id,
    item.price * item.quantity AS item_revenue,
    user_pseudo_id
  FROM {EVENTS_TABLE}
  CROSS JOIN UNNEST(items) AS item
  WHERE _TABLE_SUFFIX BETWEEN '20230101' AND FORMAT_DATE('%Y%m%d', CURRENT_DATE())
    AND event_name IN ('purchase','add_to_cart','view_item','begin_checkout')
    AND item.item_category2 IS NOT NULL
    AND item.item_category2 != ''
),
purchases AS (
  SELECT week, category, subcategory,
    COUNT(DISTINCT transaction_id) AS transactions,
    ROUND(SUM(item_revenue), 2)    AS revenue
  FROM item_events WHERE event_name = 'purchase'
  GROUP BY 1,2,3
),
atc AS (
  SELECT week, category, subcategory, COUNT(*) AS add_to_cart
  FROM item_events WHERE event_name = 'add_to_cart'
  GROUP BY 1,2,3
),
views AS (
  SELECT week, category, subcategory, COUNT(DISTINCT user_pseudo_id) AS sessions
  FROM item_events WHERE event_name = 'view_item'
  GROUP BY 1,2,3
),
checkout AS (
  SELECT week, category, subcategory, COUNT(DISTINCT user_pseudo_id) AS began_checkout
  FROM item_events WHERE event_name = 'begin_checkout'
  GROUP BY 1,2,3
)
SELECT
  v.week,
  v.category,
  v.subcategory,
  v.sessions,
  COALESCE(p.transactions, 0)   AS transactions,
  COALESCE(p.revenue, 0)        AS revenue,
  COALESCE(a.add_to_cart, 0)    AS add_to_cart,
  COALESCE(c.began_checkout, 0) AS began_checkout,
  ROUND(SAFE_DIVIDE(COALESCE(p.transactions,0), v.sessions) * 100, 3) AS cvr
FROM views v
LEFT JOIN purchases p USING (week, category, subcategory)
LEFT JOIN atc       a USING (week, category, subcategory)
LEFT JOIN checkout  c USING (week, category, subcategory)
ORDER BY v.week, v.category, v.subcategory
"""

PRODUCT_QUERY = f"""
WITH item_events AS (
  SELECT
    {WEEK_EXPR} AS week,
    item.item_name        AS product_name,
    item.item_category    AS category,
    item.item_category2   AS subcategory,
    event_name,
    ecommerce.transaction_id   AS transaction_id,
    item.price * item.quantity AS item_revenue,
    item.quantity              AS quantity
  FROM {EVENTS_TABLE}
  CROSS JOIN UNNEST(items) AS item
  WHERE _TABLE_SUFFIX BETWEEN '20230101' AND FORMAT_DATE('%Y%m%d', CURRENT_DATE())
    AND event_name IN ('purchase','add_to_cart','view_item')
    AND item.item_name IS NOT NULL
),
purchases AS (
  SELECT week, product_name, category, subcategory,
    COUNT(DISTINCT transaction_id) AS transactions,
    ROUND(SUM(item_revenue), 2)    AS revenue,
    SUM(quantity)                  AS units_sold
  FROM item_events WHERE event_name = 'purchase'
  GROUP BY 1,2,3,4
),
atc AS (
  SELECT week, product_name, COUNT(*) AS add_to_cart
  FROM item_events WHERE event_name = 'add_to_cart'
  GROUP BY 1,2
),
views AS (
  SELECT week, product_name, COUNT(*) AS product_views
  FROM item_events WHERE event_name = 'view_item'
  GROUP BY 1,2
),
-- rank products by total revenue to keep dataset manageable
top_products AS (
  SELECT product_name
  FROM purchases
  GROUP BY product_name
  ORDER BY SUM(revenue) DESC
  LIMIT 500
)
SELECT
  p.week,
  p.product_name,
  p.category,
  p.subcategory,
  p.transactions,
  p.revenue,
  p.units_sold,
  COALESCE(a.add_to_cart, 0)   AS add_to_cart,
  COALESCE(v.product_views, 0) AS product_views,
  ROUND(SAFE_DIVIDE(p.transactions, NULLIF(v.product_views,0)) * 100, 3) AS pdp_cvr
FROM purchases p
JOIN top_products tp USING (product_name)
LEFT JOIN atc a USING (week, product_name)
LEFT JOIN views v USING (week, product_name)
ORDER BY p.week, p.revenue DESC
"""

FUNNEL_QUERY = f"""
WITH funnel_events AS (
  SELECT
    {WEEK_EXPR} AS week,
    event_name,
    user_pseudo_id,
    (SELECT value.int_value FROM UNNEST(event_params) WHERE key = 'ga_session_id') AS session_id
  FROM {EVENTS_TABLE}
  WHERE _TABLE_SUFFIX BETWEEN '20230101' AND FORMAT_DATE('%Y%m%d', CURRENT_DATE())
    AND event_name IN (
      'session_start',
      'view_item',
      'add_to_cart',
      'begin_checkout',
      'add_shipping_info',
      'add_payment_info',
      'purchase'
    )
)
SELECT
  week,
  COUNTIF(event_name = 'session_start')    AS sessions,
  COUNTIF(event_name = 'view_item')        AS product_views,
  COUNTIF(event_name = 'add_to_cart')      AS add_to_cart,
  COUNTIF(event_name = 'begin_checkout')   AS begin_checkout,
  COUNTIF(event_name = 'add_shipping_info') AS add_shipping_info,
  COUNTIF(event_name = 'add_payment_info') AS add_payment_info,
  COUNTIF(event_name = 'purchase')         AS purchase
FROM funnel_events
GROUP BY week
ORDER BY week
"""


AOV_QUERY = f"""
WITH purchases AS (
  SELECT
    {WEEK_EXPR} AS week,
    item.item_category AS category,
    ecommerce.transaction_id AS transaction_id,
    ecommerce.purchase_revenue AS order_revenue
  FROM {EVENTS_TABLE}
  CROSS JOIN UNNEST(items) AS item
  WHERE _TABLE_SUFFIX BETWEEN '20250101' AND FORMAT_DATE('%Y%m%d', CURRENT_DATE())
    AND event_name = 'purchase'
    AND item.item_category IS NOT NULL AND item.item_category != ''
),
deduped AS (
  SELECT week, category, transaction_id, MAX(order_revenue) AS order_revenue
  FROM purchases
  GROUP BY 1,2,3
)
SELECT
  week,
  category,
  COUNT(DISTINCT transaction_id) AS transactions,
  ROUND(SUM(order_revenue), 2) AS revenue,
  ROUND(AVG(order_revenue), 2) AS aov
FROM deduped
GROUP BY 1,2
ORDER BY 1,2
"""

UNITS_PER_TX_QUERY = f"""
WITH purchase_items AS (
  SELECT
    {WEEK_EXPR} AS week,
    item.item_name AS product_name,
    item.item_category AS category,
    item.item_category2 AS subcategory,
    ecommerce.transaction_id AS transaction_id,
    SUM(item.quantity) AS qty_in_tx
  FROM {EVENTS_TABLE}
  CROSS JOIN UNNEST(items) AS item
  WHERE _TABLE_SUFFIX BETWEEN '20250101' AND FORMAT_DATE('%Y%m%d', CURRENT_DATE())
    AND event_name = 'purchase'
    AND item.item_name IS NOT NULL
  GROUP BY 1,2,3,4,5
),
top_products AS (
  SELECT product_name
  FROM purchase_items
  GROUP BY product_name
  ORDER BY COUNT(DISTINCT transaction_id) DESC
  LIMIT 300
)
SELECT
  p.week,
  p.product_name,
  p.category,
  p.subcategory,
  COUNT(DISTINCT p.transaction_id) AS transactions,
  SUM(p.qty_in_tx) AS total_units,
  ROUND(AVG(p.qty_in_tx), 2) AS avg_units_per_tx,
  ROUND(SAFE_DIVIDE(
    COUNTIF(p.qty_in_tx >= 4),
    COUNT(DISTINCT p.transaction_id)
  ) * 100, 1) AS pct_tx_4plus_units
FROM purchase_items p
JOIN top_products tp USING (product_name)
GROUP BY 1,2,3,4
ORDER BY 1, transactions DESC
"""

CHANGE_IMPACT_QUERY = f"""
-- For each delivery change date, compute 4-week avg before and after per category
WITH weekly_cat AS (
  SELECT
    {WEEK_EXPR} AS week,
    item.item_category AS category,
    ecommerce.transaction_id AS transaction_id,
    ecommerce.purchase_revenue AS order_revenue,
    item.price * item.quantity AS item_revenue
  FROM {EVENTS_TABLE}
  CROSS JOIN UNNEST(items) AS item
  WHERE _TABLE_SUFFIX BETWEEN '20250101' AND FORMAT_DATE('%Y%m%d', CURRENT_DATE())
    AND event_name = 'purchase'
    AND item.item_category IS NOT NULL AND item.item_category != ''
),
weekly_agg AS (
  SELECT
    week,
    category,
    COUNT(DISTINCT transaction_id) AS transactions,
    ROUND(SUM(item_revenue), 2) AS revenue,
    ROUND(AVG(order_revenue), 2) AS aov
  FROM weekly_cat
  GROUP BY 1,2
),
change_dates AS (
  SELECT change_date, change_label FROM UNNEST([
    STRUCT(DATE('2025-04-01') AS change_date, 'Apr 2025: Medium & Large increase' AS change_label),
    STRUCT(DATE('2025-08-01'), 'Aug 2025: Small furniture increase'),
    STRUCT(DATE('2026-03-27'), 'Mar 2026: Medium & Large increase')
  ])
),
before_after AS (
  SELECT
    cd.change_label,
    wa.category,
    AVG(CASE WHEN wa.week < cd.change_date AND wa.week >= DATE_SUB(cd.change_date, INTERVAL 28 DAY)
             THEN wa.revenue END) AS avg_rev_before,
    AVG(CASE WHEN wa.week >= cd.change_date AND wa.week < DATE_ADD(cd.change_date, INTERVAL 28 DAY)
             THEN wa.revenue END) AS avg_rev_after,
    AVG(CASE WHEN wa.week < cd.change_date AND wa.week >= DATE_SUB(cd.change_date, INTERVAL 28 DAY)
             THEN wa.transactions END) AS avg_tx_before,
    AVG(CASE WHEN wa.week >= cd.change_date AND wa.week < DATE_ADD(cd.change_date, INTERVAL 28 DAY)
             THEN wa.transactions END) AS avg_tx_after,
    AVG(CASE WHEN wa.week < cd.change_date AND wa.week >= DATE_SUB(cd.change_date, INTERVAL 28 DAY)
             THEN wa.aov END) AS avg_aov_before,
    AVG(CASE WHEN wa.week >= cd.change_date AND wa.week < DATE_ADD(cd.change_date, INTERVAL 28 DAY)
             THEN wa.aov END) AS avg_aov_after
  FROM change_dates cd
  CROSS JOIN weekly_agg wa
  GROUP BY 1,2
)
SELECT
  change_label,
  category,
  ROUND(avg_rev_before, 2)  AS avg_rev_before,
  ROUND(avg_rev_after, 2)   AS avg_rev_after,
  ROUND(SAFE_DIVIDE(avg_rev_after - avg_rev_before, avg_rev_before) * 100, 1) AS rev_pct_change,
  ROUND(avg_tx_before, 1)   AS avg_tx_before,
  ROUND(avg_tx_after, 1)    AS avg_tx_after,
  ROUND(SAFE_DIVIDE(avg_tx_after - avg_tx_before, avg_tx_before) * 100, 1) AS tx_pct_change,
  ROUND(avg_aov_before, 2)  AS avg_aov_before,
  ROUND(avg_aov_after, 2)   AS avg_aov_after,
  ROUND(SAFE_DIVIDE(avg_aov_after - avg_aov_before, avg_aov_before) * 100, 1) AS aov_pct_change
FROM before_after
WHERE avg_rev_before IS NOT NULL AND avg_rev_after IS NOT NULL
ORDER BY change_label, avg_rev_after DESC
"""

# ─── HELPERS ─────────────────────────────────────────────────────────────────

def run_query(client: bigquery.Client, sql: str, label: str) -> list[dict]:
    print(f"  → Running: {label} ... ", end="", flush=True)
    job = client.query(sql)
    rows = [dict(r) for r in job.result()]
    # Convert date objects to ISO strings
    for row in rows:
        for k, v in row.items():
            if hasattr(v, 'isoformat'):
                row[k] = v.isoformat()
    print(f"{len(rows):,} rows")
    return rows


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    kb = path.stat().st_size / 1024
    print(f"  ✓ Written: {path.name} ({kb:.1f} KB)")


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch B&S delivery impact data from BigQuery")
    parser.add_argument("--key", help="Path to service account JSON key file", default=None)
    args = parser.parse_args()

    # Auth
    if args.key:
        creds = service_account.Credentials.from_service_account_file(
            args.key,
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        client = bigquery.Client(project=PROJECT_ID, credentials=creds)
    else:
        # Falls back to GOOGLE_APPLICATION_CREDENTIALS or ADC
        client = bigquery.Client(project=PROJECT_ID)

    print(f"\nConnected to BigQuery project: {PROJECT_ID}\n")

    print("Fetching category data...")
    category_data = run_query(client, CATEGORY_QUERY, "category weekly")
    write_json(OUTPUT_DIR / "category_weekly.json", category_data)

    print("\nFetching subcategory data...")
    subcat_data = run_query(client, SUBCATEGORY_QUERY, "subcategory weekly")
    write_json(OUTPUT_DIR / "subcategory_weekly.json", subcat_data)

    print("\nFetching product data (top 500)...")
    product_data = run_query(client, PRODUCT_QUERY, "product weekly")
    write_json(OUTPUT_DIR / "product_weekly.json", product_data)

    print("\nFetching checkout funnel data...")
    funnel_data = run_query(client, FUNNEL_QUERY, "funnel weekly")
    write_json(OUTPUT_DIR / "funnel_weekly.json", funnel_data)

    print("\nFetching AOV by category...")
    aov_data = run_query(client, AOV_QUERY, "AOV weekly")
    write_json(OUTPUT_DIR / "aov_weekly.json", aov_data)

    print("\nFetching units per transaction...")
    units_data = run_query(client, UNITS_PER_TX_QUERY, "units per tx weekly")
    write_json(OUTPUT_DIR / "units_per_tx.json", units_data)

    print("\nFetching change impact summary...")
    impact_data = run_query(client, CHANGE_IMPACT_QUERY, "change impact")
    write_json(OUTPUT_DIR / "change_impact.json", impact_data)

    print("\nWriting meta...")
    meta = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "project_id": PROJECT_ID,
        "delivery_changes": DELIVERY_CHANGES,
        "current_delivery": CURRENT_DELIVERY
    }
    write_json(OUTPUT_DIR / "meta.json", meta)

    print("\n✅ All done. Refresh index.html to see updated data.\n")


if __name__ == "__main__":
    main()
