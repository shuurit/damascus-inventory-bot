# damascus-inventory-bot

Watches the [Damascus Gaming MTG Singles (Beta)](https://damascusgaming.myshopify.com/collections/mtg-singles-beta?sort_by=created-descending)
Shopify collection page and posts a Discord notification when something changes.

## What it tracks

Shopify's storefront doesn't expose exact stock quantities anywhere publicly —
the collection page and product pages only ever show "Sold out" vs. in-stock,
and the public `products.json` API only exposes an `available` boolean.
So instead of quantities, this tracks:

- **Total product count** on the collection (the "N products" figure shown on the page)
- **New listings** on page 1 of the collection (sorted newest-first) — removed
  listings are tracked internally to avoid re-flagging re-listed items, but
  aren't reported to Discord

## How it runs

[`check_inventory.py`](check_inventory.py) does the whole cycle in one pass:

1. Fetches the collection page
2. Parses out the product count and the current listing titles
3. Diffs against [`inventory_state.json`](inventory_state.json) (the last known state)
4. If anything changed, posts a summary to a Discord webhook
5. Writes the new state back to `inventory_state.json` and commits/pushes it

On the very first run (no prior state), it just records the baseline without
posting to Discord — there's nothing to compare against yet.

Scheduling is handled **externally by [cron-job.org](https://cron-job.org)**,
which calls the `workflow_dispatch` API on [`.github/workflows/inventory-check.yml`](.github/workflows/inventory-check.yml)
at 1:33 PM and 8:07 PM America/Denver daily. It can also be triggered manually
from the Actions tab.

GitHub Actions' own native `schedule:` cron trigger was tried first but
confirmed unreliable for this repo — it never fired once, even 24+ hours after
being configured — so scheduling now lives outside GitHub entirely.

## Setup

The workflow needs one repository secret:

| Secret | Description |
|---|---|
| `DISCORD_WEBHOOK_URL` | Discord webhook URL to post inventory updates to |

Set it under **Settings → Secrets and variables → Actions → New repository secret**.

## Running locally

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python check_inventory.py
```

Requires `git` and `curl` on PATH, and push access to the repo (the script
commits and pushes `inventory_state.json` itself after each run).
