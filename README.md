# B&S Delivery Impact Analysis Dashboard

Interactive weekly performance dashboard tracking the impact of delivery cost changes on site behaviour, checkout funnel, and category/product performance.

---

## Setup

### 1. Install Python dependencies

```bash
pip install google-cloud-bigquery pandas
```

### 2. Fetch data from BigQuery

```bash
python fetch_data.py --key path/to/your-service-account.json
```

Or if `GOOGLE_APPLICATION_CREDENTIALS` is already set:

```bash
python fetch_data.py
```

This creates a `data/` folder with five JSON files:
- `category_weekly.json`
- `subcategory_weekly.json`
- `product_weekly.json`
- `funnel_weekly.json`
- `meta.json` (includes delivery change annotations)

### 3. Open the dashboard

Open `index.html` in a browser. Because it loads local JSON files you'll need to serve it over HTTP rather than `file://`:

```bash
# Quick option — Python built-in server
python -m http.server 8080
# Then visit http://localhost:8080
```

Or just push to GitHub Pages — it will work there directly.

---

## GitHub Pages deployment

1. Push this folder to a GitHub repo (e.g. `dlawrence-bands/bs-delivery-impact`)
2. Go to **Settings → Pages → Source: main / root**
3. Run `fetch_data.py` locally whenever you want to refresh, commit the updated `data/*.json` files, and push

---

## Adding new changes to the timeline

Edit the `DELIVERY_CHANGES` list in `fetch_data.py`:

```python
DELIVERY_CHANGES = [
    {
        "date": "2026-09-01",          # ISO date — closest week will be found automatically
        "label": "Short label",         # Shown on chart annotation
        "detail": "Longer description"  # Shown in the legend
    },
    # ... existing entries
]
```

Re-run `fetch_data.py` and commit `data/meta.json`. No HTML changes needed.

---

## Dashboard sections

| Tab | What it shows |
|-----|---------------|
| **Overview** | Site-wide weekly revenue / transactions / sessions / CVR with change annotations |
| **By Category** | Weekly trend per GA4 `item_category`, filterable, with summary table |
| **By Subcategory** | Weekly trend per `item_category2`, filterable by parent category |
| **By Product** | Top 500 products by revenue; click any row to chart weekly trend |
| **Checkout Funnel** | begin_checkout volume, step drop-off rates, ATC→checkout and ship→purchase rates — all annotated |

---

## Data scope

- **Date range:** January 2023 → current date
- **Products:** Top 500 by total revenue (adjustable in `fetch_data.py` — change `LIMIT 500`)
- **BigQuery project:** `commanding-air-450109-p0` / dataset `analytics_287404213`
