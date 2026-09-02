"""
delivery_intel.py — Zomato/Swiggy Delivery Intelligence MCP Server
Exposes 4 tools: search_restaurant, get_menu, get_order_signals, compare_delivery_presence
Uses Playwright for JS-heavy pages, bs4 for HTML parsing, httpx for lightweight requests.
"""

import asyncio
import json
import random
import re
import uuid
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

import httpx
from urllib.parse import quote_plus

# Mobile UA — triggers less bot detection than desktop on Zomato/Swiggy
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.6099.144 Mobile Safari/537.36"
)

ZOMATO_BASE = "https://www.zomato.com"
SWIGGY_BASE = "https://www.swiggy.com"

# City lat/lng for Swiggy DAPI (no browser needed)
CITY_COORDS: dict[str, tuple[float, float]] = {
    "tirupur": (11.1085, 77.3411),
    "coimbatore": (11.0168, 76.9558),
    "chennai": (13.0827, 80.2707),
    "bangalore": (12.9716, 77.5946),
    "hyderabad": (17.3850, 78.4867),
    "mumbai": (19.0760, 72.8777),
    "delhi": (28.6139, 77.2090),
}

# ---------------------------------------------------------------------------
# Playwright helpers
# ---------------------------------------------------------------------------

async def _get_page_html(url: str, wait_selector: str | None = None) -> str:
    """
    Stealth headless Chromium: mobile UA, Indian locale, anti-fingerprint patches.
    Much harder for Zomato/Swiggy bot detection to flag than a plain headless browser.
    """
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    from playwright_stealth import Stealth

    html = ""
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 390, "height": 844},
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                geolocation={"latitude": 11.1085, "longitude": 77.3411},
                permissions=["geolocation"],
                extra_http_headers={
                    "Accept-Language": "en-IN,en;q=0.9,ta;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                },
            )
            page = await context.new_page()

            # Apply all stealth patches (navigator.webdriver, plugins, etc.)
            await Stealth().apply_stealth_async(page)

            # Only block fonts — blocking images looks too suspicious
            await page.route(
                "**/*.{woff,woff2,ttf,otf}",
                lambda route: route.abort(),
            )

            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(1500 + random.randint(500, 1500))

            # Simulate human scroll
            await page.mouse.wheel(0, random.randint(300, 600))
            await page.wait_for_timeout(random.randint(400, 900))

            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=10_000)
                except PWTimeout:
                    pass

            html = await page.content()
            await browser.close()
    except Exception as exc:  # noqa: BLE001
        html = f"<!-- playwright error: {exc} -->"

    return html


async def _swiggy_dapi_search(name: str, city: str = "tirupur") -> list[dict]:
    """
    Hit Swiggy's internal mobile JSON API — no browser, no CAPTCHA.
    Returns list of restaurant dicts with name, rating, delivery_time.
    """
    lat, lng = CITY_COORDS.get(city.lower(), CITY_COORDS["tirupur"])
    url = (
        f"https://www.swiggy.com/dapi/restaurants/search/v3"
        f"?query={quote_plus(name)}&lat={lat}&lng={lng}"
        f"&trackingId={uuid.uuid4()}&submitAction=ENTER&queryUniqueId={uuid.uuid4()}"
    )
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": f"https://www.swiggy.com/search?query={quote_plus(name)}",
        "Origin": "https://www.swiggy.com",
    }
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return _parse_swiggy_dapi(resp.json(), name)
    except Exception:
        pass
    return []


def _parse_swiggy_dapi(data: Any, query: str) -> list[dict]:
    """Walk Swiggy DAPI response for restaurant cards."""
    results: list[dict] = []
    query_lower = query.lower()

    def _walk(node: Any, depth: int = 0) -> None:
        if depth > 10 or not isinstance(node, (dict, list)):
            return
        if isinstance(node, list):
            for item in node:
                _walk(item, depth + 1)
        elif isinstance(node, dict):
            # Restaurant card has avgRating + name
            if ("avgRating" in node or "avgRatingString" in node) and "name" in node:
                r_name = node.get("name", "")
                results.append({
                    "name": r_name,
                    "rating": node.get("avgRatingString") or node.get("avgRating", "N/A"),
                    "delivery_time": (
                        f"{node.get('sla', {}).get('deliveryTime', 'N/A')} min"
                        if isinstance(node.get("sla"), dict) else "N/A"
                    ),
                    "cuisine": ", ".join(node.get("cuisines", [])) if node.get("cuisines") else "N/A",
                    "cost_for_two": node.get("costForTwoMessage", "N/A"),
                    "on_swiggy": True,
                    "source": "swiggy-dapi",
                })
            else:
                for v in node.values():
                    _walk(v, depth + 1)

    _walk(data)
    # Prioritise exact name matches
    exact = [r for r in results if query_lower in r["name"].lower()]
    return exact if exact else results[:5]


async def _zomato_api_search(name: str, city: str = "tirupur") -> list[dict]:
    """
    Try Zomato's internal webroutes JSON API with httpx.
    Less bot-filtered than the website; falls back gracefully.
    """
    lat, lng = CITY_COORDS.get(city.lower(), CITY_COORDS["tirupur"])
    url = (
        f"https://www.zomato.com/webroutes/search/results"
        f"?q={quote_plus(name)}&lat={lat}&lon={lng}&entity_type=city"
    )
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": f"https://www.zomato.com/{city.lower()}/restaurants",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200 and "application/json" in resp.headers.get("content-type", ""):
                return _parse_zomato_api(resp.json(), name)
    except Exception:
        pass
    return []


def _parse_zomato_api(data: Any, query: str) -> list[dict]:
    """Parse Zomato webroutes JSON search response."""
    results: list[dict] = []
    restaurants = []

    if isinstance(data, dict):
        # Try common paths
        for key in ("restaurants", "search_results", "data", "results"):
            if key in data and isinstance(data[key], list):
                restaurants = data[key]
                break

    for item in restaurants:
        if isinstance(item, dict):
            r = item.get("restaurant") or item.get("info") or item
            if not isinstance(r, dict):
                continue
            rating = r.get("user_rating", {})
            results.append({
                "name": r.get("name", ""),
                "rating": rating.get("aggregate_rating", "N/A") if isinstance(rating, dict) else "N/A",
                "votes": rating.get("votes", "N/A") if isinstance(rating, dict) else "N/A",
                "cuisine": r.get("cuisines", "N/A"),
                "average_cost_for_two": r.get("average_cost_for_two", "N/A"),
                "url": r.get("url", ""),
                "source": "zomato-api",
            })
    return results


def _random_delay() -> float:
    """Return a random delay between 1 and 3 seconds."""
    return random.uniform(1.0, 3.0)


# ---------------------------------------------------------------------------
# BeautifulSoup helpers
# ---------------------------------------------------------------------------

def _soup(html: str):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser")


def _text(el) -> str:
    """Safe .get_text() on a possibly-None element."""
    return el.get_text(strip=True) if el else ""


def _find_by_patterns(soup, tag: str, patterns: list[str]):
    """
    Try to find the first element matching any of the given CSS class
    substring patterns, returning None if nothing matches.
    """
    for pattern in patterns:
        el = soup.find(tag, class_=re.compile(pattern, re.I))
        if el:
            return el
    return None


def _find_all_by_patterns(soup, tag: str, patterns: list[str]) -> list:
    results = []
    for pattern in patterns:
        found = soup.find_all(tag, class_=re.compile(pattern, re.I))
        if found:
            results.extend(found)
    return results


# ---------------------------------------------------------------------------
# Tool 1 — search_restaurant
# ---------------------------------------------------------------------------

async def search_restaurant(name: str, city: str = "tirupur") -> dict[str, Any]:
    """
    Search Zomato for a restaurant by name in a given city.
    Returns a list of matched restaurant summaries.
    """
    city_slug = city.lower().replace(" ", "-")
    url = f"{ZOMATO_BASE}/{city_slug}/restaurants?q={quote_plus(name)}"

    # Try internal API first (no CAPTCHA)
    await asyncio.sleep(_random_delay())
    api_hits = await _zomato_api_search(name, city)
    if api_hits:
        return {
            "query": name,
            "city": city,
            "url_searched": url,
            "results": api_hits,
            "source": "zomato-internal-api",
        }

    # Fallback: stealth Playwright + HTML scraping
    html = await _get_page_html(url, wait_selector="[class*='restnt']")

    soup = _soup(html)
    results: list[dict] = []

    # Zomato renders restaurant cards — try multiple class patterns
    card_patterns = [
        r"restnt",
        r"sc-[a-z0-9]+-.*card",
        r"search.*result",
        r"ResCard",
        r"result-listing",
    ]
    cards = _find_all_by_patterns(soup, "div", card_patterns)

    # Also try <article> elements which Zomato sometimes uses
    if not cards:
        cards = soup.find_all("article")

    # Try JSON-LD structured data as a reliable fallback
    json_ld_blocks = soup.find_all("script", type="application/ld+json")
    for block in json_ld_blocks:
        try:
            data = json.loads(block.string or "")
            if isinstance(data, list):
                for item in data:
                    if item.get("@type") in ("Restaurant", "FoodEstablishment"):
                        results.append(_extract_jsonld_restaurant(item))
            elif isinstance(data, dict) and data.get("@type") in (
                "Restaurant",
                "FoodEstablishment",
            ):
                results.append(_extract_jsonld_restaurant(data))
        except (json.JSONDecodeError, AttributeError):
            pass

    # Parse DOM cards
    for card in cards[:10]:
        entry = _parse_restaurant_card(card)
        if entry.get("name"):
            results.append(entry)

    # Deduplicate by name
    seen: set[str] = set()
    unique: list[dict] = []
    for r in results:
        key = r.get("name", "").lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(r)

    return {
        "query": name,
        "city": city,
        "url_searched": url,
        "results": unique or [{"note": "No structured data found; page may require login or CAPTCHA."}],
    }


def _extract_jsonld_restaurant(data: dict) -> dict:
    address_raw = data.get("address", {})
    if isinstance(address_raw, dict):
        address = ", ".join(filter(None, [
            address_raw.get("streetAddress"),
            address_raw.get("addressLocality"),
            address_raw.get("addressRegion"),
        ]))
    else:
        address = str(address_raw)

    agg = data.get("aggregateRating", {})
    return {
        "name": data.get("name", ""),
        "rating": agg.get("ratingValue", "N/A"),
        "votes": agg.get("reviewCount", "N/A"),
        "cuisine": ", ".join(data.get("servesCuisine", [])) if isinstance(data.get("servesCuisine"), list) else data.get("servesCuisine", "N/A"),
        "average_cost_for_two": data.get("priceRange", "N/A"),
        "delivery_time": "N/A",
        "address": address,
        "url": data.get("url", ""),
        "source": "json-ld",
    }


def _parse_restaurant_card(card) -> dict:
    """Extract fields from a Zomato restaurant card DOM element."""
    entry: dict[str, Any] = {
        "name": "",
        "rating": "N/A",
        "votes": "N/A",
        "cuisine": "N/A",
        "average_cost_for_two": "N/A",
        "delivery_time": "N/A",
        "address": "N/A",
        "url": "",
        "source": "dom",
    }

    # Name: usually an h3, h4, or strong inside the card
    for tag in ("h3", "h4", "h2", "strong"):
        el = card.find(tag)
        if el and el.get_text(strip=True):
            entry["name"] = el.get_text(strip=True)
            break

    # Link
    link = card.find("a", href=True)
    if link:
        href = link["href"]
        entry["url"] = href if href.startswith("http") else ZOMATO_BASE + href

    # Rating — look for elements containing a decimal number like "4.2"
    for el in card.find_all(string=re.compile(r"^\d\.\d$")):
        entry["rating"] = el.strip()
        break

    # Votes — "1.2k votes" or "1,200 votes"
    for el in card.find_all(string=re.compile(r"\d.*vote", re.I)):
        entry["votes"] = el.strip()
        break

    # Cuisine — look for spans containing "," separated food words
    cuisine_el = card.find(string=re.compile(r"(pizza|biryani|chinese|south indian|north indian|cafe|burger|fast food|dessert)", re.I))
    if cuisine_el:
        entry["cuisine"] = cuisine_el.strip()

    # Cost for two
    cost_el = card.find(string=re.compile(r"₹\s*\d+\s*for\s*two", re.I))
    if cost_el:
        entry["average_cost_for_two"] = cost_el.strip()

    # Delivery time
    time_el = card.find(string=re.compile(r"\d+\s*min", re.I))
    if time_el:
        entry["delivery_time"] = time_el.strip()

    # Address
    addr_el = _find_by_patterns(card, "span", [r"addr", r"location", r"locality"])
    if addr_el:
        entry["address"] = addr_el.get_text(strip=True)

    return entry


# ---------------------------------------------------------------------------
# Tool 2 — get_menu
# ---------------------------------------------------------------------------

async def get_menu(restaurant_url: str) -> dict[str, Any]:
    """
    Scrape a restaurant's Zomato page and return menu sections with dishes,
    prices, bestseller flags, and veg/non-veg indicators.
    """
    await asyncio.sleep(_random_delay())
    html = await _get_page_html(restaurant_url, wait_selector="[class*='menu'], [class*='Menu']")

    soup = _soup(html)
    sections: list[dict] = []

    # ------------------------------------------------------------------
    # Strategy 1: look for section headers + item lists
    # ------------------------------------------------------------------
    section_header_patterns = [r"menuSection", r"menu-section", r"category", r"sectionHead"]
    item_patterns = [r"menuItem", r"menu-item", r"dish", r"itemCard"]

    headers = _find_all_by_patterns(soup, "div", section_header_patterns)
    # Also try h2/h3/h4 as section headers
    if not headers:
        for hx in ("h2", "h3", "h4"):
            headers = soup.find_all(hx)
            if headers:
                break

    for header in headers:
        section_name = _text(header)
        if not section_name or len(section_name) > 120:
            continue

        # Collect sibling/child dish items
        parent = header.parent
        dishes: list[dict] = []
        if parent:
            item_els = _find_all_by_patterns(parent, "div", item_patterns)
            for item_el in item_els:
                dish = _parse_dish(item_el)
                if dish.get("name"):
                    dishes.append(dish)

        if section_name or dishes:
            sections.append({"section": section_name, "dishes": dishes})

    # ------------------------------------------------------------------
    # Strategy 2: flat item scan if sections came up empty
    # ------------------------------------------------------------------
    if not sections:
        all_items = _find_all_by_patterns(soup, "div", item_patterns)
        dishes: list[dict] = []
        for item_el in all_items:
            dish = _parse_dish(item_el)
            if dish.get("name"):
                dishes.append(dish)
        if dishes:
            sections.append({"section": "Menu", "dishes": dishes})

    # ------------------------------------------------------------------
    # Strategy 3: JSON embedded in <script> tags
    # ------------------------------------------------------------------
    if not sections:
        sections = _extract_menu_from_scripts(soup)

    return {
        "restaurant_url": restaurant_url,
        "total_sections": len(sections),
        "menu": sections or [{"note": "Menu data not extractable; page may require login or render differently."}],
    }


def _parse_dish(el) -> dict:
    dish: dict[str, Any] = {
        "name": "",
        "price": "N/A",
        "is_bestseller": False,
        "is_veg": None,
        "description": "",
    }

    # Name: look for a prominent text element
    for tag in ("h4", "h3", "strong", "span"):
        name_el = el.find(tag)
        if name_el:
            text = name_el.get_text(strip=True)
            if text and len(text) < 120:
                dish["name"] = text
                break

    # Price
    price_el = el.find(string=re.compile(r"₹\s*\d+"))
    if price_el:
        dish["price"] = price_el.strip()
    else:
        price_el2 = el.find(class_=re.compile(r"price|Price|cost|Cost"))
        if price_el2:
            dish["price"] = price_el2.get_text(strip=True)

    # Bestseller
    bs_el = el.find(string=re.compile(r"bestseller|best seller|popular", re.I))
    if bs_el:
        dish["is_bestseller"] = True
    if el.find(attrs={"aria-label": re.compile(r"bestseller|popular", re.I)}):
        dish["is_bestseller"] = True

    # Veg / Non-veg indicator
    # Zomato uses small colored dot icons; look for aria-labels or data attributes
    veg_el = el.find(attrs={"aria-label": re.compile(r"\bveg\b", re.I)})
    nonveg_el = el.find(attrs={"aria-label": re.compile(r"non.?veg", re.I)})
    if veg_el and not nonveg_el:
        dish["is_veg"] = True
    elif nonveg_el:
        dish["is_veg"] = False

    # Also check for image alt text or title containing veg/non-veg
    if dish["is_veg"] is None:
        for img in el.find_all("img"):
            alt = (img.get("alt") or "").lower()
            title = (img.get("title") or "").lower()
            combined = alt + " " + title
            if "non" in combined and "veg" in combined:
                dish["is_veg"] = False
                break
            elif "veg" in combined:
                dish["is_veg"] = True
                break

    # Description
    desc_el = el.find(class_=re.compile(r"desc|description|detail", re.I))
    if desc_el:
        dish["description"] = desc_el.get_text(strip=True)[:200]

    return dish


def _extract_menu_from_scripts(soup) -> list[dict]:
    """Try to find menu data embedded as JSON in <script> tags."""
    sections: list[dict] = []
    for script in soup.find_all("script"):
        content = script.string or ""
        # Look for patterns like "menuSections" or "categories" arrays
        for pattern in (r'"menuSections"\s*:\s*(\[.*?\])', r'"categories"\s*:\s*(\[.*?\])'):
            match = re.search(pattern, content, re.S)
            if match:
                try:
                    data = json.loads(match.group(1))
                    for section in data:
                        if not isinstance(section, dict):
                            continue
                        section_name = section.get("name") or section.get("title") or "Section"
                        dishes: list[dict] = []
                        items = section.get("items") or section.get("dishes") or []
                        for item in items:
                            if not isinstance(item, dict):
                                continue
                            dishes.append({
                                "name": item.get("name") or item.get("dish_name") or "",
                                "price": f"₹{item.get('price') or item.get('cost') or 'N/A'}",
                                "is_bestseller": bool(item.get("isBestseller") or item.get("is_bestseller")),
                                "is_veg": item.get("isVeg") if "isVeg" in item else item.get("is_veg"),
                                "description": (item.get("description") or "")[:200],
                            })
                        sections.append({"section": section_name, "dishes": dishes})
                except (json.JSONDecodeError, KeyError):
                    pass
    return sections


# ---------------------------------------------------------------------------
# Tool 3 — get_order_signals
# ---------------------------------------------------------------------------

async def get_order_signals(restaurant_url: str) -> dict[str, Any]:
    """
    Extract competitive intelligence signals from a Zomato restaurant page:
    order count badge, rating, review count, online ordering status,
    active discounts, and photo count.
    """
    await asyncio.sleep(_random_delay())
    html = await _get_page_html(restaurant_url)
    soup = _soup(html)

    signals: dict[str, Any] = {
        "restaurant_url": restaurant_url,
        "order_count_badge": None,
        "rating": None,
        "review_count": None,
        "online_ordering_enabled": False,
        "active_discounts": [],
        "photo_count": None,
        "raw_badges": [],
    }

    # ------------------------------------------------------------------
    # Order count badge  e.g. "500+ orders this month"
    # ------------------------------------------------------------------
    order_text = soup.find(string=re.compile(r"\d+\+?\s*orders?\s*(this\s*month|today)?", re.I))
    if order_text:
        signals["order_count_badge"] = order_text.strip()

    # Also check data attributes and aria-labels
    for el in soup.find_all(attrs={"aria-label": True}):
        label = el["aria-label"]
        if re.search(r"\d+\+?\s*orders?", label, re.I):
            signals["order_count_badge"] = label
            break

    # ------------------------------------------------------------------
    # Rating
    # ------------------------------------------------------------------
    # Look for a prominent rating number (e.g., "4.2")
    rating_patterns = [r"sc-.*rating", r"rating", r"Rating"]
    for pattern in rating_patterns:
        rating_el = soup.find(class_=re.compile(pattern, re.I))
        if rating_el:
            candidate = rating_el.get_text(strip=True)
            if re.match(r"^\d\.\d$", candidate):
                signals["rating"] = candidate
                break

    if not signals["rating"]:
        for text in soup.find_all(string=re.compile(r"^\d\.\d$")):
            signals["rating"] = text.strip()
            break

    # ------------------------------------------------------------------
    # Review count
    # ------------------------------------------------------------------
    review_el = soup.find(string=re.compile(r"\d[\d,\.k]+\s+(reviews?|ratings?)", re.I))
    if review_el:
        signals["review_count"] = review_el.strip()

    # ------------------------------------------------------------------
    # Online ordering
    # ------------------------------------------------------------------
    # If "Order Online" button or section exists, ordering is enabled
    order_btn = soup.find(string=re.compile(r"order\s*online|place\s*order", re.I))
    if order_btn:
        signals["online_ordering_enabled"] = True

    # Check for delivery-related sections
    delivery_section = soup.find(attrs={"data-section": re.compile(r"delivery|order", re.I)})
    if delivery_section:
        signals["online_ordering_enabled"] = True

    # ------------------------------------------------------------------
    # Discount / offer banners
    # ------------------------------------------------------------------
    discount_patterns = [
        r"\d+%\s*off",
        r"flat\s*₹?\d+\s*off",
        r"free\s*deliver",
        r"extra\s*\d+%",
        r"use\s+code",
        r"offer",
    ]
    seen_discounts: set[str] = set()
    for pattern in discount_patterns:
        for el in soup.find_all(string=re.compile(pattern, re.I)):
            text = el.strip()
            if text and text.lower() not in seen_discounts and len(text) < 200:
                seen_discounts.add(text.lower())
                signals["active_discounts"].append(text)

    # ------------------------------------------------------------------
    # Photo count
    # ------------------------------------------------------------------
    photo_el = soup.find(string=re.compile(r"\d+\+?\s*photos?", re.I))
    if photo_el:
        signals["photo_count"] = photo_el.strip()

    # Actual img count as proxy
    if not signals["photo_count"]:
        # Count non-icon images (rough heuristic: src contains /food/ or /restaurant/)
        food_imgs = soup.find_all("img", src=re.compile(r"/(food|restaurant|dish)/", re.I))
        if food_imgs:
            signals["photo_count"] = f"{len(food_imgs)} images found on page"

    # ------------------------------------------------------------------
    # Collect any badge/pill texts
    # ------------------------------------------------------------------
    badge_patterns = [r"badge", r"tag", r"pill", r"label", r"chip"]
    for pattern in badge_patterns:
        for el in soup.find_all(class_=re.compile(pattern, re.I)):
            text = el.get_text(strip=True)
            if text and len(text) < 80:
                signals["raw_badges"].append(text)

    # Deduplicate badges
    signals["raw_badges"] = list(dict.fromkeys(signals["raw_badges"]))[:20]

    return signals


# ---------------------------------------------------------------------------
# Tool 4 — compare_delivery_presence
# ---------------------------------------------------------------------------

async def compare_delivery_presence(
    restaurant_names: list[str],
    city: str = "tirupur",
) -> dict[str, Any]:
    """
    For each restaurant name, search both Zomato and Swiggy and return a
    side-by-side presence comparison with ratings and delivery times.
    """
    city_slug = city.lower().replace(" ", "-")
    comparisons: list[dict] = []

    for name in restaurant_names:
        await asyncio.sleep(_random_delay())
        zomato_data = await _search_zomato_single(name, city_slug)

        await asyncio.sleep(_random_delay())
        swiggy_data = await _search_swiggy_single(name, city_slug)

        comparisons.append({
            "restaurant": name,
            "zomato": zomato_data,
            "swiggy": swiggy_data,
            "summary": _build_comparison_summary(zomato_data, swiggy_data),
        })

    return {
        "city": city,
        "comparisons": comparisons,
    }


async def _search_zomato_single(name: str, city_slug: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "found": False,
        "platform": "zomato",
        "url": f"{ZOMATO_BASE}/{city_slug}/restaurants?q={quote_plus(name)}",
        "rating": "N/A",
        "votes": "N/A",
        "delivery_time": "N/A",
        "online_ordering": False,
    }

    # PRIMARY: try internal JSON API (no CAPTCHA)
    api_results = await _zomato_api_search(name, city_slug.replace("-", " "))
    if api_results:
        r = api_results[0]
        result.update({
            "found": True,
            "name_on_platform": r.get("name", name),
            "rating": r.get("rating", "N/A"),
            "votes": r.get("votes", "N/A"),
            "restaurant_url": r.get("url", ""),
            "online_ordering": bool(r.get("url")),
        })
        return result

    # FALLBACK: stealth Playwright
    url = f"{ZOMATO_BASE}/{city_slug}/restaurants?q={quote_plus(name)}"
    html = await _get_page_html(url, wait_selector="[class*='restnt']")
    soup = _soup(html)

    for block in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(block.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") in ("Restaurant", "FoodEstablishment"):
                    agg = item.get("aggregateRating", {})
                    result["found"] = True
                    result["name_on_platform"] = item.get("name", name)
                    result["rating"] = agg.get("ratingValue", "N/A")
                    result["votes"] = agg.get("reviewCount", "N/A")
                    result["restaurant_url"] = item.get("url", "")
                    return result
        except (json.JSONDecodeError, AttributeError):
            pass

    cards = _find_all_by_patterns(soup, "div", [r"restnt", r"ResCard", r"result-listing"])
    if not cards:
        cards = soup.find_all("article")
    if cards:
        entry = _parse_restaurant_card(cards[0])
        if entry.get("name"):
            result.update({
                "found": True,
                "name_on_platform": entry["name"],
                "rating": entry["rating"],
                "votes": entry["votes"],
                "delivery_time": entry["delivery_time"],
                "restaurant_url": entry["url"],
            })

    if soup.find(string=re.compile(r"order\s*online|place\s*order", re.I)):
        result["online_ordering"] = True

    return result


async def _search_swiggy_single(name: str, city_slug: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "found": False,
        "platform": "swiggy",
        "url": f"https://www.swiggy.com/search?query={quote_plus(name)}",
        "rating": "N/A",
        "votes": "N/A",
        "delivery_time": "N/A",
        "online_ordering": False,
    }

    # PRIMARY: Swiggy DAPI JSON endpoint — no browser, no CAPTCHA
    dapi_results = await _swiggy_dapi_search(name, city_slug.replace("-", " "))
    if dapi_results:
        r = dapi_results[0]
        result.update({
            "found": True,
            "name_on_platform": r.get("name", name),
            "rating": r.get("rating", "N/A"),
            "delivery_time": r.get("delivery_time", "N/A"),
            "cuisine": r.get("cuisine", "N/A"),
            "cost_for_two": r.get("cost_for_two", "N/A"),
            "online_ordering": True,
        })
        return result

    # FALLBACK: stealth Playwright
    search_url = f"https://www.swiggy.com/search?query={quote_plus(name)}"
    html = await _get_page_html(search_url, wait_selector="[class*='restaurant']")
    soup = _soup(html)

    next_data_script = soup.find("script", id="__NEXT_DATA__")
    if next_data_script:
        try:
            next_data = json.loads(next_data_script.string or "")
            restaurants = _dig_swiggy_restaurants(next_data)
            name_lower = name.lower()
            for r in restaurants:
                r_name = (r.get("name") or r.get("restaurantName") or "").lower()
                if name_lower in r_name or r_name in name_lower:
                    result.update({
                        "found": True,
                        "name_on_platform": r.get("name") or r.get("restaurantName"),
                        "rating": r.get("avgRating") or r.get("avgRatingString") or "N/A",
                        "delivery_time": (
                            f"{r.get('sla', {}).get('deliveryTime', 'N/A')} min"
                            if isinstance(r.get("sla"), dict) else "N/A"
                        ),
                        "online_ordering": True,
                    })
                    return result
        except (json.JSONDecodeError, AttributeError, KeyError):
            pass

    return result


def _dig_swiggy_restaurants(data: Any, depth: int = 0) -> list[dict]:
    """Recursively search Swiggy's Next.js data for restaurant objects."""
    if depth > 8 or not isinstance(data, (dict, list)):
        return []

    found: list[dict] = []

    if isinstance(data, list):
        for item in data:
            found.extend(_dig_swiggy_restaurants(item, depth + 1))
    elif isinstance(data, dict):
        # A restaurant object typically has avgRating and name fields
        if "avgRating" in data and ("name" in data or "restaurantName" in data):
            found.append(data)
        else:
            for v in data.values():
                found.extend(_dig_swiggy_restaurants(v, depth + 1))

    return found


def _build_comparison_summary(zomato: dict, swiggy: dict) -> dict:
    both_present = zomato["found"] and swiggy["found"]
    zomato_only = zomato["found"] and not swiggy["found"]
    swiggy_only = swiggy["found"] and not zomato["found"]
    neither = not zomato["found"] and not swiggy["found"]

    if both_present:
        platform_status = "on both platforms"
    elif zomato_only:
        platform_status = "Zomato only"
    elif swiggy_only:
        platform_status = "Swiggy only"
    else:
        platform_status = "not found on either platform"

    # Rating comparison
    rating_note = ""
    try:
        z_rating = float(str(zomato.get("rating", 0)).replace("N/A", "0"))
        s_rating = float(str(swiggy.get("rating", 0)).replace("N/A", "0"))
        if z_rating and s_rating:
            if z_rating > s_rating:
                rating_note = f"Better rated on Zomato ({z_rating} vs {s_rating})"
            elif s_rating > z_rating:
                rating_note = f"Better rated on Swiggy ({s_rating} vs {z_rating})"
            else:
                rating_note = f"Equal rating ({z_rating}) on both"
    except ValueError:
        pass

    return {
        "platform_status": platform_status,
        "zomato_present": zomato["found"],
        "swiggy_present": swiggy["found"],
        "rating_comparison": rating_note or "N/A",
        "zomato_delivery_time": zomato.get("delivery_time", "N/A"),
        "swiggy_delivery_time": swiggy.get("delivery_time", "N/A"),
    }


# ---------------------------------------------------------------------------
# MCP Server setup
# ---------------------------------------------------------------------------

server = Server("delivery-intel")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_restaurant",
            description=(
                "Search Zomato for a restaurant by name in a given city. "
                "Returns name, rating, votes, cuisine, average cost for two, "
                "delivery time, and address."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Restaurant name to search for"},
                    "city": {
                        "type": "string",
                        "description": "City name (default: tirupur)",
                        "default": "tirupur",
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="get_menu",
            description=(
                "Scrape a Zomato restaurant page and return all menu sections "
                "with dish names, prices, bestseller flags, and veg/non-veg indicators."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "restaurant_url": {
                        "type": "string",
                        "description": "Full Zomato URL of the restaurant page",
                    }
                },
                "required": ["restaurant_url"],
            },
        ),
        Tool(
            name="get_order_signals",
            description=(
                "Extract competitive intelligence from a Zomato restaurant page: "
                "order count badge, rating, review count, online ordering status, "
                "active discounts, and photo count."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "restaurant_url": {
                        "type": "string",
                        "description": "Full Zomato URL of the restaurant page",
                    }
                },
                "required": ["restaurant_url"],
            },
        ),
        Tool(
            name="compare_delivery_presence",
            description=(
                "Compare a list of restaurants across Zomato and Swiggy. "
                "Returns presence on each platform, rating comparison, and delivery time comparison."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "restaurant_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of restaurant names to compare",
                    },
                    "city": {
                        "type": "string",
                        "description": "City name (default: tirupur)",
                        "default": "tirupur",
                    },
                },
                "required": ["restaurant_names"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "search_restaurant":
            result = await search_restaurant(
                name=arguments["name"],
                city=arguments.get("city", "tirupur"),
            )
        elif name == "get_menu":
            result = await get_menu(restaurant_url=arguments["restaurant_url"])
        elif name == "get_order_signals":
            result = await get_order_signals(restaurant_url=arguments["restaurant_url"])
        elif name == "compare_delivery_presence":
            result = await compare_delivery_presence(
                restaurant_names=arguments["restaurant_names"],
                city=arguments.get("city", "tirupur"),
            )
        else:
            result = {"error": f"Unknown tool: {name}"}
    except Exception as exc:  # noqa: BLE001
        result = {"error": str(exc), "tool": name, "arguments": arguments}

    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
