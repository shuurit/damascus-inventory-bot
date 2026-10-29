#!/usr/bin/env python3
"""
Checks the Damascus Gaming MTG Singles (Beta) collection page for:
  - the total product count shown on the page
  - the list of items on page 1 (sorted newest-first)
and reports any changes to a Discord webhook.

Note: this storefront does not expose exact stock quantities anywhere
(collection page and product pages only show "Sold out" vs. in-stock,
and the public products.json API only exposes an `available` boolean).
So "items" tracks presence to detect new listings, not quantity. Removed
listings are still recorded in state (so they don't reappear as "new" if
re-listed later at the same title) but are not reported to Discord.
"""
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

SITE_URL = "https://damascusgaming.myshopify.com"
COLLECTION_URL = f"{SITE_URL}/collections/mtg-singles-beta?sort_by=created-descending"
SCRYFALL_API = "https://api.scryfall.com/cards/named"
# Scryfall asks API clients to send a descriptive User-Agent and an Accept header.
SCRYFALL_HEADERS = {"User-Agent": "damascus-inventory-bot/1.0", "Accept": "application/json"}
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
STATE_FILE = Path(__file__).parent / "inventory_state.json"

HEADING_RE = re.compile(
    r'class="card__heading[^"]*"[^>]*>\s*<a\s+href="(/products/[^"]+)"[^>]*>\s*([^<]+?)\s*</a>',
    re.DOTALL,
)
COUNT_RE = re.compile(r'id="ProductCount">\s*([\d,]+)\s*products')
# Set codes in listing titles look like "(2X2)", "(MH2)", "(LTC)".
SET_CODE_RE = re.compile(r"\(([A-Za-z0-9]{2,5})\)")


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
    urls = {}
    for href, title in HEADING_RE.findall(page_html):
        title = html.unescape(title.strip())
        items[title] = 1  # presence marker; no real quantity is exposed
        urls[title] = SITE_URL + html.unescape(href)

    if not items:
        raise RuntimeError("Could not find any product listings on page")

    return product_count, items, urls


def fetch_and_parse(url: str, attempts: int = 4, delay_seconds: float = 15):
    # Shopify/Cloudflare occasionally serves a bot-check or empty page to
    # cloud-runner IPs; a short retry clears most of these transient blips.
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            page_html = fetch_html(url)
            return parse_page(page_html)
        except Exception as e:
            last_error = e
            print(f"Attempt {attempt}/{attempts} failed: {e}", file=sys.stderr)
            if attempt < attempts:
                time.sleep(delay_seconds)
    raise last_error


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_checked": None, "product_count": None, "items": {}}


def parse_listing_title(title: str):
    """Split a listing title like "Elenda, the Dusk Rose (2X2)- Lightly Played"
    into candidate card names (most specific first) and a set code."""
    set_codes = [c for c in SET_CODE_RE.findall(title) if c.lower() != "showcase"]
    set_code = set_codes[-1].lower() if set_codes else None

    name = title.split("(", 1)[0].strip(" -")
    name = re.sub(r"\s+", " ", name)
    candidates = [name]
    # Variant suffixes like "Dina, Soul Steeper - Halo Foil" -- but some real
    # card names contain " - " too, so keep the full name as the first try.
    if " - " in name:
        candidates.append(name.split(" - ", 1)[0].strip())
    return candidates, set_code


def scryfall_search_url(card_name: str) -> str:
    return "https://scryfall.com/search?q=" + urllib.parse.quote(f'!"{card_name}"')


def fetch_scryfall_card(params: dict):
    url = SCRYFALL_API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=SCRYFALL_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"Scryfall lookup failed for {params}: {e}", file=sys.stderr)
        return None
    finally:
        time.sleep(0.1)  # Scryfall asks for 50-100ms between requests


def card_image_url(card: dict):
    images = card.get("image_uris") or (card.get("card_faces") or [{}])[0].get("image_uris") or {}
    return images.get("normal") or images.get("large") or images.get("small")


def lookup_card(title: str):
    """Best-effort Scryfall lookup; returns the card JSON or None."""
    candidates, set_code = parse_listing_title(title)
    # Set-scoped lookups first: they're far less likely to fuzzy-match the wrong card.
    attempts = [{"fuzzy": name, "set": set_code} for name in candidates if set_code]
    attempts += [{"fuzzy": name} for name in candidates]
    for params in attempts:
        card = fetch_scryfall_card(params)
        if card and card.get("object") == "card":
            return card
    return None


def build_listing_embed(title: str, url: str):
    card = lookup_card(title)
    if card:
        card_name = card["name"]
    else:
        card_name = parse_listing_title(title)[0][-1]

    embed = {
        "title": title[:256],
        "url": url,
        "description": f"[View on site]({url}) \u2022 [Scryfall search]({scryfall_search_url(card_name)})",
        "color": 0x2B6CB0,
    }
    image = card_image_url(card) if card else None
    if image:
        embed["image"] = {"url": image}
    return embed


def post_to_discord(message: str, embeds=None):
    # Discord allows at most 10 embeds per message, so split into batches.
    embeds = embeds or []
    batches = [embeds[i:i + 10] for i in range(0, len(embeds), 10)] or [[]]
    for i, batch in enumerate(batches):
        payload = {"content": message if i == 0 else ""}
        if batch:
            payload["embeds"] = batch
        send_discord_payload(payload)


def send_discord_payload(payload: dict):
    body = json.dumps(payload)
    subprocess.run(
        [
            "curl", "-sS", "--fail-with-body", "-X", "POST",
            "-H", "Content-Type: application/json",
            "-d", body,
            DISCORD_WEBHOOK_URL,
        ],
        check=True,
    )


def git(*args):
    subprocess.run(["git", *args], cwd=Path(__file__).parent, check=True)


def main():
    product_count, items, urls = fetch_and_parse(COLLECTION_URL)

    state = load_state()
    is_first_run = not state.get("items") and state.get("product_count") is None

    old_items = state.get("items", {})
    old_count = state.get("product_count")

    new_titles = sorted(set(items) - set(old_items))
    count_changed = (old_count is not None) and (old_count != product_count)

    lines = []
    if count_changed:
        delta = product_count - old_count
        arrow = "up" if delta > 0 else "down"
        lines.append(f"- Product count: {old_count} -> {product_count} ({arrow} {abs(delta)})")
    elif old_count is None:
        lines.append(f"- Product count: {product_count}")

    if new_titles:
        # Each new listing gets its own embed (site link, Scryfall, image).
        lines.append(f"- {len(new_titles)} NEW listing{'s' if len(new_titles) != 1 else ''}:")

    should_post = bool(lines) and not is_first_run
    # Always post the product count as a baseline confirmation, even on
    # a run where item baselines were already established.
    if is_first_run and old_count is None:
        should_post = False

    if should_post:
        message = "\U0001F4E6 Inventory update\n" + "\n".join(lines)
        embeds = [build_listing_embed(title, urls[title]) for title in new_titles]
        post_to_discord(message, embeds)
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
