#!/usr/bin/env python3
"""
Ammo Price Alert
Checks Ammoseek, Reddit r/gundeals, BulkAmmo, TargetSportsUSA, and MidwayUSA
for 9mm ammo under $0.25/round WITH free shipping. Sends an HTML email if deals found.

Usage:
  python ammo_alert.py           # normal run (sends email if deals found)
  python ammo_alert.py --test    # dry run: prints deals, no email sent
"""

import argparse
import json
import os
import re
import smtplib
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup

# ── Constants ────────────────────────────────────────────────────────────────

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
MAX_CPR = 0.25  # max cost-per-round in dollars

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ── Helpers ──────────────────────────────────────────────────────────────────

def parse_cpr(text: str) -> float | None:
    """Extract a cost-per-round dollar value from a string.
    Handles formats like: $0.24, 24cpr, 24¢, .24/rd, 24 cents
    Returns a float in dollars, or None if not found.
    """
    text = text.replace(",", "")
    # Already-dollar formats: $0.24, $0.249, .24
    m = re.search(r"\$\s*0?\.(\d+)", text)
    if m:
        return float(f"0.{m.group(1)}")
    # Cents formats: 24cpr, 24¢, 24 cents, 24/rd
    # Only divide by 100 for plain integers (e.g. "24cpr"); decimals like "1.00 per round" are already dollars
    m = re.search(r"\b(\d{1,2}(?:\.\d+)?)\s*(?:cpr|¢|cents?|/rd|per\s*round)", text, re.I)
    if m:
        val = float(m.group(1))
        if val >= 1 and "." not in m.group(1):
            return val / 100
        return val
    return None


def compute_cpr(name: str, price_text: str) -> float | None:
    """Compute CPR from a product name (contains quantity) and a price string."""
    qty_m = re.search(r"\b(\d{2,4})\s*(?:rounds?|rds?|ct|count)\b", name, re.I)
    price_m = re.search(r"\$\s*(\d+\.?\d*)", price_text)
    if qty_m and price_m:
        qty = int(qty_m.group(1))
        price = float(price_m.group(1))
        if qty > 0:
            return price / qty
    return None


def has_free_shipping(text: str) -> bool:
    return bool(re.search(r"free\s*ship", text, re.I))


_CASE_RE = [
    (re.compile(r"\bsteel\b",                 re.I), "steel"),
    (re.compile(r"\baluminum\b|\baluminium\b", re.I), "aluminum"),
]


def detect_grain(name: str) -> str:
    """Extract grain weight from a product name, e.g. '115gr', '124 grain', '147GR'.
    Returns the grain string (e.g. '115') or 'other' if not found.
    """
    m = re.search(r"\b(\d{2,3})\s*(?:gr(?:ain)?s?)\b", name, re.I)
    return m.group(1) if m else "other"


def detect_case_type(name: str) -> str:
    """Infer case material from a product name.
    Returns 'steel', 'aluminum', or 'brass' (default for unlabeled / nickel-plated / etc.)
    """
    for pattern, case_type in _CASE_RE:
        if pattern.search(name):
            return case_type
    return "brass"


# ── Scrapers ─────────────────────────────────────────────────────────────────

def scrape_ammoseek() -> list[dict]:
    """
    Ammoseek aggregates dozens of retailers. We filter by free shipping up front.
    URL: https://ammoseek.com/ammo/9mm-luger?fs=1
    """
    deals = []
    url = "https://ammoseek.com/ammo/9mm-luger?fs=1"
    try:
        r = SESSION.get(url, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Ammoseek renders a <table> with class "product-table" or similar
        rows = soup.select("tr[data-product], tr.offer, tr.result")
        if not rows:
            # Fallback: any table row with a price-looking cell
            rows = soup.select("table tbody tr")

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            row_text = row.get_text(" ", strip=True)
            cpr = parse_cpr(row_text)
            if cpr is None:
                continue
            if cpr > MAX_CPR:
                continue

            name_el = row.select_one("td.name, td:first-child, .product-name")
            link_el = row.select_one("a[href]")
            retailer_el = row.select_one(".merchant, .retailer, td.store")

            _name = name_el.get_text(strip=True) if name_el else row_text[:80]
            deals.append({
                "source":    "Ammoseek",
                "retailer":  retailer_el.get_text(strip=True) if retailer_el else "via Ammoseek",
                "name":      _name,
                "cpr":       cpr,
                "url":       link_el["href"] if link_el else url,
                "free_shipping": True,  # we filtered by fs=1
                "case_type": detect_case_type(_name),
                "grain":     detect_grain(_name),
            })

        print(f"  [Ammoseek] {len(deals)} deal(s) found")
    except Exception as e:
        print(f"  [Ammoseek] Error: {e}")
    return deals


def scrape_reddit_gundeals() -> list[dict]:
    """
    r/gundeals Reddit JSON API. Free, no auth needed.
    Looks for posts mentioning 9mm with a parseable CPR and 'free shipping' / 'FS'.
    """
    deals = []
    url = "https://www.reddit.com/r/gundeals/search.json"
    params = {"q": "9mm", "restrict_sr": 1, "sort": "new", "limit": 50, "t": "day"}
    try:
        r = SESSION.get(url, params=params, timeout=20,
                        headers={"User-Agent": "AmmoAlert/1.0 (ammo price monitor)"})
        r.raise_for_status()
        posts = r.json().get("data", {}).get("children", [])

        for post in posts:
            p = post["data"]
            title = p.get("title", "")
            permalink = "https://reddit.com" + p.get("permalink", "")
            link_url = p.get("url", permalink)

            # Must mention 9mm
            if "9mm" not in title.lower():
                continue

            # Must have free shipping signal
            if not re.search(r"\bfs\b|free\s*ship", title, re.I):
                continue

            cpr = parse_cpr(title)
            if cpr is None:
                continue
            if cpr > MAX_CPR:
                continue

            deals.append({
                "source":    "Reddit r/gundeals",
                "retailer":  "r/gundeals",
                "name":      title,
                "cpr":       cpr,
                "url":       link_url,
                "free_shipping": True,
                "case_type": detect_case_type(title),
                "grain":     detect_grain(title),
            })

        print(f"  [Reddit r/gundeals] {len(deals)} deal(s) found")
    except Exception as e:
        print(f"  [Reddit r/gundeals] Error: {e}")
    return deals


def scrape_bulkammo() -> list[dict]:
    """BulkAmmo.com — 9mm category page."""
    deals = []
    url = "https://www.bulkammo.com/handgun/bulk-9mm-ammo"
    try:
        r = SESSION.get(url, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        products = soup.select(".product-item, li.product, .product_list_item")
        for item in products:
            name_el = item.select_one(".product-name, h2, h3, .name")
            price_el = item.select_one(".price, .product-price, [class*='price']")
            link_el = item.select_one("a[href]")
            if not (name_el and price_el):
                continue

            name = name_el.get_text(strip=True)
            item_text = item.get_text(" ", strip=True)

            cpr = parse_cpr(item_text) or compute_cpr(name, price_el.get_text())
            if cpr is None or cpr > MAX_CPR:
                continue

            if not has_free_shipping(item_text):
                continue

            href = link_el["href"] if link_el else url
            if href.startswith("/"):
                href = "https://www.bulkammo.com" + href

            deals.append({
                "source":    "BulkAmmo.com",
                "retailer":  "BulkAmmo.com",
                "name":      name,
                "cpr":       cpr,
                "url":       href,
                "free_shipping": True,
                "case_type": detect_case_type(name),
                "grain":     detect_grain(name),
            })

        print(f"  [BulkAmmo] {len(deals)} deal(s) found")
    except Exception as e:
        print(f"  [BulkAmmo] Error: {e}")
    return deals


def scrape_targetsports() -> list[dict]:
    """TargetSportsUSA — 9mm Luger category. Free shipping with PRIME or on qualifying orders."""
    deals = []
    url = "https://www.targetsportsusa.com/9mm-luger-ammo-c-51.aspx"
    try:
        r = SESSION.get(url, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        products = soup.select(".product-item, .product_item, li.product, [class*='product']")
        for item in products:
            name_el = item.select_one("h2, h3, .product-name, .name")
            price_el = item.select_one(".price, [class*='price']")
            link_el = item.select_one("a[href]")
            if not name_el:
                continue

            name = name_el.get_text(strip=True)
            item_text = item.get_text(" ", strip=True)

            cpr = parse_cpr(item_text)
            if cpr is None and price_el:
                cpr = compute_cpr(name, price_el.get_text())
            if cpr is None or cpr > MAX_CPR:
                continue

            if not has_free_shipping(item_text):
                continue

            href = link_el["href"] if link_el else url
            if href.startswith("/"):
                href = "https://www.targetsportsusa.com" + href

            deals.append({
                "source":    "TargetSportsUSA",
                "retailer":  "TargetSportsUSA",
                "name":      name,
                "cpr":       cpr,
                "url":       href,
                "free_shipping": True,
                "case_type": detect_case_type(name),
                "grain":     detect_grain(name),
            })

        print(f"  [TargetSportsUSA] {len(deals)} deal(s) found")
    except Exception as e:
        print(f"  [TargetSportsUSA] Error: {e}")
    return deals


def scrape_midwayusa() -> list[dict]:
    """MidwayUSA — 9mm Luger via Algolia search API (bypasses Akamai bot protection).
    Free shipping on orders >= $49; we check bulk case price to confirm eligibility."""
    deals = []
    try:
        from curl_cffi import requests as cf_requests
    except ImportError:
        print("  [MidwayUSA] curl_cffi not installed -- skipping")
        return deals

    ALGOLIA_APP_ID = "UQIWQHWTGQ"
    ALGOLIA_API_KEY = "ba4f024807ab1f7f9d863f7d7ee61e7d"
    ALGOLIA_INDEX   = "p_product_price_per_unit_asc"
    url = f"https://{ALGOLIA_APP_ID}.algolia.net/1/indexes/{ALGOLIA_INDEX}/query"

    try:
        r = cf_requests.post(
            url,
            impersonate="chrome",
            timeout=20,
            headers={
                "X-Algolia-Application-Id": ALGOLIA_APP_ID,
                "X-Algolia-API-Key":        ALGOLIA_API_KEY,
                "Content-Type":             "application/json",
                "Referer":                  "https://www.midwayusa.com/",
            },
            json={
                "query":        "",
                "hitsPerPage":  100,
                "facetFilters": [
                    "Cartridge:9mm Luger",
                    "categoryIds:691",       # Handgun Ammunition
                    "Availability:In Stock",
                ],
                "numericFilters":        [f"retail.sortPricePerUnit <= {MAX_CPR}"],
                "attributesToRetrieve":  ["name", "retail", "saleItemId"],
            },
        )
        r.raise_for_status()
        for hit in r.json().get("hits", []):
            retail = hit.get("retail", {})
            cpr = retail.get("sortPricePerUnit") or (retail.get("pricePerUnit") or {}).get("low")
            if not cpr or cpr > MAX_CPR:
                continue
            # MidwayUSA ships free on orders >= $49; bulk case price covers that
            bulk_price = (retail.get("ourPrice") or {}).get("high", 0)
            if bulk_price < 49:
                continue
            item_id = hit.get("saleItemId")
            _mw_name = hit.get("name", "")
            deals.append({
                "source":        "MidwayUSA",
                "retailer":      "MidwayUSA",
                "name":          _mw_name,
                "cpr":           cpr,
                "url":           f"https://www.midwayusa.com/product/{item_id}",
                "free_shipping": True,
                "case_type":     detect_case_type(_mw_name),
                "grain":         detect_grain(_mw_name),
            })
        print(f"  [MidwayUSA] {len(deals)} deal(s) found")
    except Exception as e:
        print(f"  [MidwayUSA] Error: {e}")
    return deals


def scrape_sgammo() -> list[dict]:
    """SGAmmo.com — 9mm Luger catalog. Free shipping label appears per product."""
    deals = []
    url = "https://sgammo.com/catalog/pistol-ammo-sale/9mm-luger-ammo/"
    try:
        r = SESSION.get(url, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        products = soup.select("tr.sgammo-content-product-item")
        for item in products:
            name_el = item.select_one("h4.sgammo-content-product-item__title")
            link_el = item.select_one("a[href]")
            if not name_el:
                continue

            name = name_el.get_text(strip=True)
            item_text = item.get_text(" ", strip=True)

            cpr = parse_cpr(item_text)
            if cpr is None or cpr > MAX_CPR:
                continue

            if not has_free_shipping(item_text):
                continue

            href = link_el["href"] if link_el else url

            deals.append({
                "source":    "SGAmmo",
                "retailer":  "SGAmmo",
                "name":      name,
                "cpr":       cpr,
                "url":       href,
                "free_shipping": True,
                "case_type": detect_case_type(name),
                "grain":     detect_grain(name),
            })

        print(f"  [SGAmmo] {len(deals)} deal(s) found")
    except Exception as e:
        print(f"  [SGAmmo] Error: {e}")
    return deals


# ── Email ────────────────────────────────────────────────────────────────────

def build_email_html(deals: list[dict]) -> str:
    rows = ""
    for d in sorted(deals, key=lambda x: x["cpr"]):
        rows += f"""
        <tr>
          <td style="padding:10px 8px;border-bottom:1px solid #e0e0e0;">{d['source']}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #e0e0e0;">{d['name'][:90]}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #e0e0e0;color:#2e7d32;font-weight:bold;">
            ${d['cpr']:.3f}
          </td>
          <td style="padding:10px 8px;border-bottom:1px solid #e0e0e0;color:#2e7d32;">✓ Free</td>
          <td style="padding:10px 8px;border-bottom:1px solid #e0e0e0;">
            <a href="{d['url']}" style="color:#1565c0;">Buy Now →</a>
          </td>
        </tr>"""

    return f"""
<html>
<body style="font-family:Arial,sans-serif;color:#333;max-width:820px;margin:0 auto;">
  <div style="background:#1b5e20;padding:20px;border-radius:8px 8px 0 0;">
    <h2 style="color:#fff;margin:0;">🎯 9mm Ammo Alert</h2>
    <p style="color:#c8e6c9;margin:4px 0 0;">{len(deals)} deal(s) under $0.25/rd with free shipping</p>
  </div>
  <div style="background:#f9f9f9;padding:16px;border-radius:0 0 8px 8px;border:1px solid #e0e0e0;">
    <p style="margin:0 0 12px;font-size:13px;color:#666;">
      Checked: {datetime.now().strftime("%A, %B %d %Y at %I:%M %p")}
    </p>
    <table style="width:100%;border-collapse:collapse;background:#fff;border-radius:6px;overflow:hidden;">
      <thead>
        <tr style="background:#e8f5e9;">
          <th style="padding:10px 8px;text-align:left;font-size:13px;">Source</th>
          <th style="padding:10px 8px;text-align:left;font-size:13px;">Product</th>
          <th style="padding:10px 8px;text-align:left;font-size:13px;">Per Round</th>
          <th style="padding:10px 8px;text-align:left;font-size:13px;">Shipping</th>
          <th style="padding:10px 8px;text-align:left;font-size:13px;">Link</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    <p style="font-size:11px;color:#999;margin-top:16px;">
      Sent by Ammo Price Alert &nbsp;|&nbsp; Threshold: $0.25/rd &nbsp;|&nbsp; Free shipping required
    </p>
  </div>
</body>
</html>"""


def send_email(deals: list[dict], config: dict) -> None:
    cfg = config["email"]
    html = build_email_html(deals)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎯 9mm Ammo Deal: {len(deals)} listing(s) under $0.25/rd + free shipping"
    msg["From"] = cfg["sender"]
    msg["To"] = cfg["recipient"]
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(cfg["sender"], cfg["app_password"])
        server.sendmail(cfg["sender"], cfg["recipient"], msg.as_string())

    print(f"  Email sent to {cfg['recipient']} — {len(deals)} deal(s)")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="9mm Ammo Price Alert")
    parser.add_argument(
        "--test", action="store_true",
        help="Dry run: print deals to console, do not send email"
    )
    args = parser.parse_args()

    print(f"\n{'='*55}")
    print(f"Ammo Price Alert — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Threshold: < ${MAX_CPR:.2f}/rd  |  Free shipping required")
    print(f"{'='*55}")

    if not args.test:
        if not os.path.exists(CONFIG_FILE):
            print(f"\nERROR: config.json not found at {CONFIG_FILE}")
            print("Copy config.json.example to config.json and fill in your credentials.")
            sys.exit(1)
        with open(CONFIG_FILE) as f:
            config = json.load(f)

    print("\nScraping sources...")
    all_deals: list[dict] = []
    all_deals.extend(scrape_ammoseek())
    all_deals.extend(scrape_reddit_gundeals())
    all_deals.extend(scrape_bulkammo())
    all_deals.extend(scrape_targetsports())
    all_deals.extend(scrape_midwayusa())
    all_deals.extend(scrape_sgammo())

    print(f"\n{'-'*55}")
    print(f"Total deals found: {len(all_deals)}")

    if not all_deals:
        print("No deals meet criteria. No email sent.")
        return

    # Print summary table
    print(f"\n{'Source':<20} {'CPR':>6}  {'Product'}")
    print("-" * 55)
    for d in sorted(all_deals, key=lambda x: x["cpr"]):
        print(f"{d['source']:<20} ${d['cpr']:.3f}  {d['name'][:40]}")

    if args.test:
        print("\n[--test mode] Email not sent.")
        return

    print("\nSending email...")
    send_email(all_deals, config)


if __name__ == "__main__":
    main()
