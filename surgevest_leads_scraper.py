"""
surgevest_leads_scraper.py
Searches Google Places API for local businesses, extracts contact info,
and saves leads to surgevest_leads.csv with smart Best Call Time logic.

Usage:
    python surgevest_leads_scraper.py --query "med spas in Atlanta" --api-key YOUR_KEY
    python surgevest_leads_scraper.py --query "restaurants in Miami" --api-key YOUR_KEY
"""

import argparse
import csv
import os
import sys
import time

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PLACES_TEXT_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
PLACES_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
OUTPUT_FILE = "surgevest_leads.csv"

DETAIL_FIELDS = (
    "name,formatted_phone_number,formatted_address,website,rating,types,business_status"
)

# ---------------------------------------------------------------------------
# Best Call Time logic
# ---------------------------------------------------------------------------

# Maps Google Places types -> call window
_TYPE_CALL_TIMES: dict[str, str] = {
    # Restaurants / food
    "restaurant": "Evenings 5–8 PM",
    "food": "Evenings 5–8 PM",
    "bar": "Evenings 5–9 PM",
    "cafe": "Mornings 8–10 AM",
    "bakery": "Mornings 8–10 AM",
    "meal_delivery": "Evenings 5–8 PM",
    "meal_takeaway": "Evenings 5–8 PM",
    # Salons / beauty / wellness / spas
    "beauty_salon": "Midday 11 AM–2 PM",
    "hair_care": "Midday 11 AM–2 PM",
    "spa": "Midday 11 AM–2 PM",
    "gym": "Mornings 8–10 AM or Evenings 5–7 PM",
    # Medical / health
    "doctor": "Mornings 9–11 AM",
    "dentist": "Mornings 9–11 AM",
    "hospital": "Mornings 9–11 AM",
    "physiotherapist": "Mornings 9–11 AM",
    "health": "Mornings 9–11 AM",
    # Professional offices
    "lawyer": "Mornings 9–11 AM or Afternoons 2–4 PM",
    "accounting": "Mornings 9–11 AM or Afternoons 2–4 PM",
    "finance": "Mornings 9–11 AM or Afternoons 2–4 PM",
    "insurance_agency": "Mornings 9–11 AM or Afternoons 2–4 PM",
    "real_estate_agency": "Mornings 9–11 AM or Afternoons 2–4 PM",
    # Retail
    "store": "Midday 11 AM–2 PM",
    "clothing_store": "Midday 11 AM–2 PM",
    "shoe_store": "Midday 11 AM–2 PM",
    "shopping_mall": "Midday 11 AM–2 PM",
    # Auto
    "car_dealer": "Mornings 9–11 AM or Afternoons 2–4 PM",
    "car_repair": "Mornings 8–10 AM",
}

_DEFAULT_CALL_TIME = "Mornings 9–11 AM or Afternoons 2–4 PM"


def best_call_time(types: list[str], query: str = "") -> str:
    """Return the best call-time window for a business given its Place types."""
    for t in types:
        if t in _TYPE_CALL_TIMES:
            return _TYPE_CALL_TIMES[t]

    # Fallback: infer from the search query keyword
    q = query.lower()
    if any(k in q for k in ("restaurant", "food", "bar", "cafe", "bistro", "diner")):
        return "Evenings 5–8 PM"
    if any(k in q for k in ("salon", "spa", "beauty", "hair", "nail", "massage")):
        return "Midday 11 AM–2 PM"
    if any(k in q for k in ("gym", "fitness", "yoga", "pilates")):
        return "Mornings 8–10 AM or Evenings 5–7 PM"

    return _DEFAULT_CALL_TIME


# ---------------------------------------------------------------------------
# Google Places API helpers
# ---------------------------------------------------------------------------

def text_search(query: str, api_key: str) -> list[dict]:
    """Fetch all pages of Text Search results for the given query."""
    results: list[dict] = []
    params: dict = {"query": query, "key": api_key}

    while True:
        resp = requests.get(PLACES_TEXT_SEARCH_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        status = data.get("status")
        if status not in ("OK", "ZERO_RESULTS"):
            sys.exit(f"[Places API] Text Search error: {status} — {data.get('error_message', '')}")

        results.extend(data.get("results", []))

        next_token = data.get("next_page_token")
        if not next_token:
            break

        # Google requires a short delay before the next-page token is valid
        time.sleep(2)
        params = {"pagetoken": next_token, "key": api_key}

    return results


def place_details(place_id: str, api_key: str) -> dict:
    """Fetch detailed fields for a single place."""
    params = {"place_id": place_id, "fields": DETAIL_FIELDS, "key": api_key}
    resp = requests.get(PLACES_DETAILS_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    status = data.get("status")
    if status != "OK":
        return {}

    return data.get("result", {})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Google Places leads → CSV")
    parser.add_argument(
        "--query",
        required=True,
        help='Search query, e.g. "med spas in Atlanta"',
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("GOOGLE_PLACES_API_KEY"),
        help="Google Places API key (or set GOOGLE_PLACES_API_KEY env var)",
    )
    return parser.parse_args()


def build_row(detail: dict, query: str) -> dict:
    types: list[str] = detail.get("types", [])
    return {
        "Business Name": detail.get("name", ""),
        "Phone Number": detail.get("formatted_phone_number", ""),
        "Address": detail.get("formatted_address", ""),
        "Website": detail.get("website", ""),
        "Rating": detail.get("rating", ""),
        "Business Status": detail.get("business_status", ""),
        "Business Types": ", ".join(t for t in types if t != "point_of_interest"),
        "Best Call Time": best_call_time(types, query),
    }


def main() -> None:
    args = parse_args()

    if not args.api_key:
        sys.exit(
            "No API key provided. Pass --api-key or set the "
            "GOOGLE_PLACES_API_KEY environment variable."
        )

    print(f'Searching Google Places for: "{args.query}"')
    listings = text_search(args.query, args.api_key)

    if not listings:
        print("No results found.")
        return

    print(f"Found {len(listings)} listings — fetching details...")

    fieldnames = [
        "Business Name",
        "Phone Number",
        "Address",
        "Website",
        "Rating",
        "Business Status",
        "Business Types",
        "Best Call Time",
    ]

    rows: list[dict] = []
    for i, listing in enumerate(listings, 1):
        place_id = listing.get("place_id")
        if not place_id:
            continue

        detail = place_details(place_id, args.api_key)
        if not detail:
            # Fall back to summary data from text search
            detail = listing

        row = build_row(detail, args.query)
        rows.append(row)

        name = row["Business Name"] or f"(place {i})"
        print(f"  [{i}/{len(listings)}] {name}")

        # Be polite to the API
        if i < len(listings):
            time.sleep(0.2)

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nSaved {len(rows)} leads → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
