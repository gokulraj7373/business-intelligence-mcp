#!/usr/bin/env python3
"""
JustDial Intel MCP Server
- search_businesses: scrape JustDial (Playwright) + Sulekha fallback
- get_business_reviews: extract JustDial reviews for a specific business
- compare_justdial_vs_google: side-by-side JD vs Google Places rating comparison
"""

import os
import asyncio
import json
import random
import re
import time
from typing import Any

import httpx
from bs4 import BeautifulSoup
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

GMAPS_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
PLACES_BASE = "https://maps.googleapis.com/maps/api/place"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

app = Server("justdial-intel")

# ── Playwright helpers ────────────────────────────────────────────────────────

async def _make_browser_context(playwright):
    """Create a stealthy Playwright browser context."""
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-http2",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
        ],
    )
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 800},
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        extra_http_headers={
            "Accept-Language": "en-IN,en;q=0.9,ta;q=0.8",
        },
    )
    # Block images and fonts to speed up scraping
    await context.route(
        "**/*",
        lambda route: (
            route.abort()
            if route.request.resource_type in ("image", "font", "media", "stylesheet")
            else route.continue_()
        ),
    )
    return browser, context


async def _random_delay(min_s: float = 2.0, max_s: float = 4.0):
    await asyncio.sleep(random.uniform(min_s, max_s))


# ── JustDial scraping ─────────────────────────────────────────────────────────

def _parse_jd_rating(text: str) -> float | None:
    """Extract a float rating from a string like '4.2' or '4.2 out of 5'."""
    if not text:
        return None
    m = re.search(r"(\d+\.\d+|\d+)", text.strip())
    return float(m.group(1)) if m else None


def _parse_jd_rating_count(text: str) -> int | None:
    """Extract rating count from strings like '(142 Ratings)' or '142'."""
    if not text:
        return None
    m = re.search(r"(\d[\d,]*)", text.replace(",", ""))
    return int(m.group(1).replace(",", "")) if m else None


def _parse_years(text: str) -> str | None:
    """Extract years in service from strings like '12 Years in Business'."""
    if not text:
        return None
    m = re.search(r"(\d+)\s*[Yy]ear", text)
    return f"{m.group(1)} years" if m else text.strip()


async def _scrape_justdial_listings(page, url: str) -> list[dict]:
    """Navigate to a JustDial search URL and extract business listings."""
    businesses = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        # Wait for results container — JD uses various selectors
        result_selector = (
            "li.cntanr, "
            "div.resultbox_info, "
            "div[class*='resultbox'], "
            "div.store-details, "
            "div.jd-store-card"
        )
        try:
            await page.wait_for_selector(result_selector, timeout=30000)
        except Exception:
            pass  # Will try to parse whatever loaded

        await _random_delay(2.0, 4.0)
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")

        # --- Try selector set A: classic JD listing structure ---
        cards = soup.select("li.cntanr")
        if not cards:
            # Selector set B: newer JD layout
            cards = soup.select("div.store-details")
        if not cards:
            cards = soup.select("div[class*='resultbox_info']")

        for card in cards[:20]:
            biz: dict[str, Any] = {}

            # Name
            name_el = (
                card.select_one("span.lng_txt")
                or card.select_one("a.store-name")
                or card.select_one("h2.jdtitle")
                or card.select_one("[class*='companyName']")
                or card.select_one("[class*='store_name']")
            )
            if name_el:
                biz["name"] = name_el.get_text(strip=True)
            else:
                continue  # skip cards without a name

            # Rating
            rating_el = (
                card.select_one("span.rt_count")
                or card.select_one("span[class*='rating_count']")
                or card.select_one("div.rated")
                or card.select_one("span.green-box")
            )
            biz["rating"] = _parse_jd_rating(rating_el.get_text() if rating_el else "")

            # Number of ratings
            num_rating_el = (
                card.select_one("span.rt_count_txt")
                or card.select_one("span[class*='ratingText']")
                or card.select_one("span[class*='review_count']")
            )
            biz["num_ratings"] = _parse_jd_rating_count(
                num_rating_el.get_text() if num_rating_el else ""
            )

            # Address
            addr_el = (
                card.select_one("span.cont_fl_addr")
                or card.select_one("p.store-add")
                or card.select_one("[class*='address']")
            )
            biz["address"] = addr_el.get_text(strip=True) if addr_el else None

            # Phone (often masked)
            phone_el = (
                card.select_one("span.callcontent")
                or card.select_one("a[href^='tel:']")
                or card.select_one("[class*='phone']")
            )
            if phone_el:
                phone_text = phone_el.get("href", phone_el.get_text(strip=True))
                biz["phone"] = phone_text.replace("tel:", "").strip()
            else:
                biz["phone"] = None

            # Category
            cat_el = (
                card.select_one("span.cat_nm")
                or card.select_one("p.mrehyp")
                or card.select_one("[class*='category']")
            )
            biz["category"] = cat_el.get_text(strip=True) if cat_el else None

            # Years in service
            years_el = (
                card.select_one("span.est")
                or card.select_one("[class*='years']")
                or card.select_one("span[class*='yrs']")
            )
            biz["years_in_service"] = _parse_years(
                years_el.get_text() if years_el else ""
            )

            biz["source"] = "justdial"
            businesses.append(biz)

    except Exception as exc:
        businesses.append({"error": f"JustDial scrape failed: {exc}", "source": "justdial"})

    return businesses


# ── Sulekha fallback (httpx + BS4) ───────────────────────────────────────────

async def _scrape_sulekha(query: str, city: str) -> list[dict]:
    """Fallback: scrape Sulekha with httpx + BeautifulSoup."""
    query_slug = query.strip().lower().replace(" ", "-")
    city_slug = city.strip().lower().replace(" ", "-")
    url = f"https://www.sulekha.com/{city_slug}/{query_slug}-in-{city_slug}"

    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-IN,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    businesses = []
    try:
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return [{"error": f"Sulekha returned {r.status_code}", "source": "sulekha"}]

        soup = BeautifulSoup(r.text, "html.parser")

        # Sulekha listing cards
        cards = (
            soup.select("div.compnybox")
            or soup.select("div.listing-card")
            or soup.select("div[class*='biz-listing']")
            or soup.select("li.biz-listing-large")
        )

        for card in cards[:15]:
            biz: dict[str, Any] = {}

            name_el = (
                card.select_one("h2.companyname")
                or card.select_one("h2.biz-name")
                or card.select_one("a.businessname")
                or card.select_one("[class*='company-name']")
            )
            if name_el:
                biz["name"] = name_el.get_text(strip=True)
            else:
                continue

            rating_el = (
                card.select_one("span.rating")
                or card.select_one("[class*='rating-num']")
                or card.select_one("span[class*='stars']")
            )
            biz["rating"] = _parse_jd_rating(rating_el.get_text() if rating_el else "")

            num_el = card.select_one("[class*='review']") or card.select_one("[class*='rating-count']")
            biz["num_ratings"] = _parse_jd_rating_count(num_el.get_text() if num_el else "")

            addr_el = card.select_one("[class*='address']") or card.select_one("p.address")
            biz["address"] = addr_el.get_text(strip=True) if addr_el else None

            phone_el = card.select_one("a[href^='tel:']") or card.select_one("[class*='phone']")
            if phone_el:
                biz["phone"] = phone_el.get("href", phone_el.get_text(strip=True)).replace("tel:", "").strip()
            else:
                biz["phone"] = None

            biz["category"] = None
            biz["years_in_service"] = None
            biz["source"] = "sulekha"
            businesses.append(biz)

    except Exception as exc:
        businesses.append({"error": f"Sulekha scrape failed: {exc}", "source": "sulekha"})

    return businesses


# ── Google Places helpers ─────────────────────────────────────────────────────

async def _google_place_details(place_id: str) -> dict:
    """Fetch basic rating data from Google Places Details API."""
    url = f"{PLACES_BASE}/details/json"
    params = {
        "place_id": place_id,
        "fields": "name,rating,user_ratings_total,formatted_address",
        "key": GMAPS_KEY,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params=params)
        data = r.json()
    if data.get("status") != "OK":
        return {"error": data.get("status", "unknown error")}
    result = data.get("result", {})
    return {
        "name": result.get("name"),
        "google_rating": result.get("rating"),
        "google_review_count": result.get("user_ratings_total"),
        "address": result.get("formatted_address"),
    }


async def _google_find_place(business_name: str, city: str) -> dict:
    """Find a place on Google by text search and return rating."""
    url = f"{PLACES_BASE}/findplacefromtext/json"
    params = {
        "input": f"{business_name} {city}",
        "inputtype": "textquery",
        "fields": "place_id,name,rating,user_ratings_total,formatted_address",
        "key": GMAPS_KEY,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params=params)
        data = r.json()
    candidates = data.get("candidates", [])
    if not candidates:
        return {"error": "No Google Places match found"}
    c = candidates[0]
    return {
        "name": c.get("name"),
        "google_rating": c.get("rating"),
        "google_review_count": c.get("user_ratings_total"),
        "address": c.get("formatted_address"),
        "place_id": c.get("place_id"),
    }


# ── MCP Tools ─────────────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search_businesses",
            description=(
                "Search for businesses on JustDial (JS-rendered, Playwright) with "
                "Sulekha as fallback. Returns a list of businesses sorted by rating "
                "descending, including name, rating, number of ratings, address, "
                "phone, category, and years in service."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Business type or name to search (e.g. 'coffee shops', 'bakeries')",
                    },
                    "city": {
                        "type": "string",
                        "description": "City name (default: tirupur)",
                        "default": "tirupur",
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="get_business_reviews",
            description=(
                "Fetch top 10 reviews for a specific business from JustDial. "
                "Returns reviewer name, rating, review text, date, average rating, "
                "and 1-5 star distribution. Useful for understanding local sentiment "
                "about competitors not well-covered on Google Maps."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "business_name": {
                        "type": "string",
                        "description": "Exact or approximate business name",
                    },
                    "city": {
                        "type": "string",
                        "description": "City name (default: tirupur)",
                        "default": "tirupur",
                    },
                },
                "required": ["business_name"],
            },
        ),
        types.Tool(
            name="compare_justdial_vs_google",
            description=(
                "Compare a business's JustDial rating vs Google rating side-by-side. "
                "Highlights discrepancies — many local Tirupur businesses have 50+ "
                "JustDial reviews but zero Google reviews. Optionally accepts a "
                "Google place_id for exact lookup."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "business_name": {
                        "type": "string",
                        "description": "Business name to compare",
                    },
                    "place_id": {
                        "type": "string",
                        "description": "Google Places place_id (optional; auto-searched if omitted)",
                    },
                    "city": {
                        "type": "string",
                        "description": "City name (default: tirupur)",
                        "default": "tirupur",
                    },
                },
                "required": ["business_name"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "search_businesses":
        result = await _tool_search_businesses(
            query=arguments["query"],
            city=arguments.get("city", "tirupur"),
        )
    elif name == "get_business_reviews":
        result = await _tool_get_business_reviews(
            business_name=arguments["business_name"],
            city=arguments.get("city", "tirupur"),
        )
    elif name == "compare_justdial_vs_google":
        result = await _tool_compare_justdial_vs_google(
            business_name=arguments["business_name"],
            place_id=arguments.get("place_id"),
            city=arguments.get("city", "tirupur"),
        )
    else:
        result = {"error": f"Unknown tool: {name}"}

    return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


# ── Tool implementations ──────────────────────────────────────────────────────

async def _tool_search_businesses(query: str, city: str) -> dict:
    from playwright.async_api import async_playwright

    jd_url = f"https://www.justdial.com/{city.strip().title()}/{query.strip().title().replace(' ', '-')}"

    businesses = []
    jd_note = None

    try:
        async with async_playwright() as pw:
            browser, context = await _make_browser_context(pw)
            try:
                page = await context.new_page()
                jd_businesses = await _scrape_justdial_listings(page, jd_url)
                # Check if we got real results or just errors
                real = [b for b in jd_businesses if "error" not in b]
                errors = [b for b in jd_businesses if "error" in b]
                businesses.extend(real)
                if errors:
                    jd_note = errors[0].get("error", "JustDial partially blocked")
            finally:
                await context.close()
                await browser.close()
    except Exception as exc:
        jd_note = f"JustDial Playwright failed: {exc}"

    # Sulekha fallback — always run if JD returned fewer than 3 results
    sulekha_note = None
    if len(businesses) < 3:
        try:
            sulekha_biz = await _scrape_sulekha(query, city)
            real_s = [b for b in sulekha_biz if "error" not in b]
            errs_s = [b for b in sulekha_biz if "error" in b]
            businesses.extend(real_s)
            if errs_s:
                sulekha_note = errs_s[0].get("error")
        except Exception as exc:
            sulekha_note = f"Sulekha fallback failed: {exc}"

    # Sort by rating descending (None ratings go last)
    businesses.sort(key=lambda b: (b.get("rating") is None, -(b.get("rating") or 0)))

    output: dict[str, Any] = {
        "query": query,
        "city": city,
        "justdial_url": jd_url,
        "total_found": len(businesses),
        "businesses": businesses,
    }
    if jd_note:
        output["justdial_note"] = jd_note
    if sulekha_note:
        output["sulekha_note"] = sulekha_note

    return output


async def _tool_get_business_reviews(business_name: str, city: str) -> dict:
    from playwright.async_api import async_playwright

    search_url = f"https://www.justdial.com/{city.strip().title()}/{business_name.strip().title().replace(' ', '-')}"

    reviews = []
    avg_rating = None
    distribution = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    note = None
    business_found = None

    try:
        async with async_playwright() as pw:
            browser, context = await _make_browser_context(pw)
            try:
                page = await context.new_page()

                # Step 1: go to search results
                await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
                try:
                    await page.wait_for_selector(
                        "li.cntanr, div.store-details, div[class*='resultbox']",
                        timeout=30000,
                    )
                except Exception:
                    pass
                await _random_delay(2.0, 3.0)

                # Step 2: click the first result to open its detail page
                first_link = (
                    await page.query_selector("li.cntanr a.jdtitle")
                    or await page.query_selector("h2.jdtitle a")
                    or await page.query_selector("a.store-name")
                    or await page.query_selector("div.store-details a[href*='/tirupur/']")
                    or await page.query_selector("li.cntanr a")
                )

                detail_url = None
                if first_link:
                    href = await first_link.get_attribute("href")
                    if href:
                        detail_url = href if href.startswith("http") else f"https://www.justdial.com{href}"

                if detail_url:
                    await page.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
                    await _random_delay(2.0, 4.0)

                html = await page.content()
                soup = BeautifulSoup(html, "html.parser")

                # Business name from detail page
                name_el = soup.select_one("h1.jdtitle, h1[class*='company'], span.heading1")
                if name_el:
                    business_found = name_el.get_text(strip=True)

                # Overall rating
                overall_el = soup.select_one(
                    "span.green-box, span[class*='overall_rating'], div.rated span"
                )
                if overall_el:
                    avg_rating = _parse_jd_rating(overall_el.get_text())

                # Rating distribution
                dist_els = soup.select("div[class*='ratingrow'], li[class*='ratingrow']")
                for el in dist_els:
                    star_el = el.select_one("span[class*='star_count'], span.star")
                    count_el = el.select_one("span[class*='ratingcount'], span.count")
                    if star_el and count_el:
                        star = _parse_jd_rating_count(star_el.get_text())
                        count = _parse_jd_rating_count(count_el.get_text())
                        if star and count and 1 <= star <= 5:
                            distribution[star] = count

                # Extract reviews
                review_cards = soup.select(
                    "div.reviewInfo, div[class*='review_block'], div[class*='reviewcard'], li.review_list"
                )
                for card in review_cards[:10]:
                    rev: dict[str, Any] = {}

                    reviewer_el = card.select_one(
                        "span.reviewer_name, span[class*='reviewer'], strong[class*='name']"
                    )
                    rev["reviewer"] = reviewer_el.get_text(strip=True) if reviewer_el else "Anonymous"

                    star_el = card.select_one(
                        "span.rt_count, span[class*='rating'], div.rated span"
                    )
                    rev["rating"] = _parse_jd_rating(star_el.get_text() if star_el else "")

                    text_el = card.select_one(
                        "p.review_text, span[class*='review_text'], div[class*='reviewdesc']"
                    )
                    rev["review_text"] = text_el.get_text(strip=True) if text_el else None

                    date_el = card.select_one(
                        "span.review_date, span[class*='date'], time"
                    )
                    rev["date"] = date_el.get_text(strip=True) if date_el else None

                    reviews.append(rev)

            finally:
                await context.close()
                await browser.close()

    except Exception as exc:
        note = f"Playwright error: {exc}"

    # Compute avg from reviews if not found on page
    if not avg_rating and reviews:
        rated = [r["rating"] for r in reviews if r.get("rating")]
        if rated:
            avg_rating = round(sum(rated) / len(rated), 2)

    return {
        "business_name": business_found or business_name,
        "city": city,
        "average_rating": avg_rating,
        "rating_distribution": distribution,
        "total_reviews_fetched": len(reviews),
        "reviews": reviews,
        **({"note": note} if note else {}),
    }


async def _tool_compare_justdial_vs_google(
    business_name: str, place_id: str | None, city: str
) -> dict:
    from playwright.async_api import async_playwright

    jd_url = f"https://www.justdial.com/{city.strip().title()}/{business_name.strip().title().replace(' ', '-')}"

    jd_rating = None
    jd_review_count = None
    jd_note = None

    # --- Scrape JustDial ---
    try:
        async with async_playwright() as pw:
            browser, context = await _make_browser_context(pw)
            try:
                page = await context.new_page()
                await page.goto(jd_url, wait_until="domcontentloaded", timeout=60000)
                try:
                    await page.wait_for_selector(
                        "li.cntanr, div.store-details, div[class*='resultbox']",
                        timeout=30000,
                    )
                except Exception:
                    pass
                await _random_delay(2.0, 3.5)

                html = await page.content()
                soup = BeautifulSoup(html, "html.parser")

                # Grab first result's rating
                first_card = (
                    soup.select_one("li.cntanr")
                    or soup.select_one("div.store-details")
                    or soup.select_one("div[class*='resultbox_info']")
                )
                if first_card:
                    rating_el = (
                        first_card.select_one("span.rt_count")
                        or first_card.select_one("span[class*='rating_count']")
                        or first_card.select_one("span.green-box")
                    )
                    jd_rating = _parse_jd_rating(rating_el.get_text() if rating_el else "")

                    num_el = (
                        first_card.select_one("span.rt_count_txt")
                        or first_card.select_one("span[class*='ratingText']")
                    )
                    jd_review_count = _parse_jd_rating_count(
                        num_el.get_text() if num_el else ""
                    )
            finally:
                await context.close()
                await browser.close()

    except Exception as exc:
        jd_note = f"JustDial scrape failed: {exc}"

    # --- Google Places ---
    google_data: dict[str, Any] = {}
    try:
        if place_id:
            google_data = await _google_place_details(place_id)
        else:
            google_data = await _google_find_place(business_name, city)
    except Exception as exc:
        google_data = {"error": f"Google Places API failed: {exc}"}

    g_rating = google_data.get("google_rating")
    g_count = google_data.get("google_review_count")

    # --- Insight generation ---
    insights = []

    if jd_review_count and (not g_count or g_count == 0):
        insights.append(
            f"This business has {jd_review_count} JustDial reviews but 0 Google reviews — "
            "strong local presence not reflected on Google Maps."
        )
    elif jd_review_count and g_count:
        ratio = jd_review_count / max(g_count, 1)
        if ratio > 5:
            insights.append(
                f"JustDial reviews ({jd_review_count}) far outnumber Google reviews ({g_count}). "
                "Audience skews older / non-smartphone-savvy locals."
            )
        elif g_count > jd_review_count * 5:
            insights.append(
                f"Google reviews ({g_count}) far outnumber JustDial ({jd_review_count or 0}). "
                "Likely targets tech-savvy or out-of-town customers."
            )

    if jd_rating and g_rating:
        diff = abs(jd_rating - g_rating)
        if diff >= 0.5:
            higher = "JustDial" if jd_rating > g_rating else "Google"
            lower = "Google" if higher == "JustDial" else "JustDial"
            insights.append(
                f"Rating gap of {diff:.1f} stars: {higher} users rate this business higher. "
                f"{lower} reviewers may have different expectations."
            )
    elif jd_rating and not g_rating:
        insights.append("Business is rated on JustDial but not found/rated on Google Maps.")
    elif g_rating and not jd_rating:
        insights.append("Business is rated on Google but not found/rated on JustDial.")

    if not insights:
        insights.append("Ratings are broadly consistent across platforms.")

    return {
        "business_name": business_name,
        "city": city,
        "justdial": {
            "rating": jd_rating,
            "review_count": jd_review_count,
            "url": jd_url,
            **({"note": jd_note} if jd_note else {}),
        },
        "google": {
            "rating": g_rating,
            "review_count": g_count,
            "place_id": google_data.get("place_id") or place_id,
            "address": google_data.get("address"),
            **({"error": google_data["error"]} if "error" in google_data else {}),
        },
        "insights": insights,
    }


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
