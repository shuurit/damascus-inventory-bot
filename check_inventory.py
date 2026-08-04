#!/usr/bin/env python3
"""
Checks the Damascus Gaming MTG Singles (Beta) collection page for:
  - the total product count shown on the page
  - the list of items on page 1 (sorted newest-first)
and reports any changes to a Discord webhook.

Note: this storefront does not expose exact stock quantities anywhere
(collection page and product pages only show "Sold out" vs. in-stock,
and the public products.json API only exposes an `available` boolean).
So "items" tracks presence (new/removed listings), not quantity.
"""
import html
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

COLLECTION_URL = "https://damascusgaming.myshopify.com/collections/mtg-singles-beta?sort_by=created-descending"
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
STATE_FILE = Path(__file__).parent / "inventory_state.json"

HEADING_RE = re.compile(
    r'class="card__heading[^"]*"[^>]*>\s*<a\s+href="(/products/[^"]+)"[^>]*>\s*([^<]+?)\s*</a>',
    re.DOTALL,
)
COUNT_RE = re.compile(r'id="ProductCount">\s*([\d,]+)\s*products')


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def parse_page(page_html: str):
    count_match = COUNT_RE.search(page_html)
    if not count_match:
        raise RuntimeError("Could not find product count on page")
    product_count = int(count_match.group(1).replace(",", ""))

    items = {}
    for href, title in HEADING_RE.findall(page_html):
        items[html.unescape(title.strip())] = 1  # presence marker; no real quantity is exposed

    if not items:
        raise RuntimeError("Could not find any product listings on page")

    return product_count, items


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_checked": None, "product_count": None, "items": {}}


def post_to_discord(message: str):
    body = json.dumps({"content": message})
    subprocess.run(
        [
            "curl", "-s", "-X", "POST",
            "-H", "Content-Type: application/json",
            "-d", body,
            DISCORD_WEBHOOK_URL,
        ],
        check=True,
    )


def git(*args):
    subprocess.run(["git", *args], cwd=Path(__file__).parent, check=True)


def main():
    html = fetch_html(COLLECTION_URL)
    product_count, items = parse_page(html)

    state = load_state()
    is_first_run = not state.get("items") and state.get("product_count") is None

    old_items = state.get("items", {})
    old_count = state.get("product_count")

    new_titles = sorted(set(items) - set(old_items))
    removed_titles = sorted(set(old_items) - set(items))
    count_changed = (old_count is not None) and (old_count != product_count)

    lines = []
    if count_changed:
        delta = product_count - old_count
        arrow = "up" if delta > 0 else "down"
        lines.append(f"- Product count: {old_count} -> {product_count} ({arrow} {abs(delta)})")
    elif old_count is None:
        lines.append(f"- Product count: {product_count}")

    for title in new_titles:
        lines.append(f"- {title}: NEW listing")
    for title in removed_titles:
        lines.append(f"- {title}: REMOVED")

    should_post = bool(lines) and not is_first_run
    # Always post the product count as a baseline confirmation, even on
    # a run where item baselines were already established.
    if is_first_run and old_count is None:
        should_post = False

    if should_post:
        message = "\U0001F4E6 Inventory update\n" + "\n".join(lines)
        post_to_discord(message)
        print("Posted to Discord:\n" + message)
    else:
        print("No changes to report." if not is_first_run else "First run: baseline recorded, no post.")

    new_state = {
        "last_checked": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "product_count": product_count,
        "items": items,
    }
    STATE_FILE.write_text(json.dumps(new_state, indent=2) + "\n")

    git("add", "inventory_state.json")
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=Path(__file__).parent
    )
    if result.returncode != 0:
        git("commit", "-m", f"Update inventory state [{new_state['last_checked']}]")
        git("push")
    else:
        print("No file changes to commit.")


if __name__ == "__main__":
    main()
