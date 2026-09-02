"""
review_analytics.py — Review Time-Series Analyser MCP Server
Exposes three tools:
  1. review_trend(place_id, months=12)
  2. sentiment_by_category(place_id)
  3. inflection_detector(place_id)

Run with: C:/c4/venv/Scripts/python review_analytics.py
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from google import genai as genai_sdk

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
PLACES_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"

_gemini_client = genai_sdk.Client(api_key=GEMINI_API_KEY)

async def _gemini_generate(prompt: str) -> str:
    try:
        response = _gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        return response.text or ""
    except Exception as e:
        return f"Gemini unavailable: {e}"

server = Server("review-analytics")

# ---------------------------------------------------------------------------
# Utility: relative timestamp → approximate months ago
# ---------------------------------------------------------------------------

def relative_to_months(text: str) -> int:
    """
    Convert a relative date string like '2 months ago', 'a year ago', etc.
    into an integer representing months in the past.
    """
    if not text:
        return 0
    t = text.lower().strip()

    # Patterns: "just now", "today", "yesterday"
    if any(w in t for w in ("just now", "today", "an hour", "hours ago")):
        return 0
    if "yesterday" in t:
        return 0
    if "week" in t:
        num = _extract_num(t)
        return max(0, round(num / 4))
    if "month" in t:
        num = _extract_num(t)
        return int(num)
    if "year" in t:
        num = _extract_num(t)
        return int(num * 12)
    return 0


def _extract_num(text: str) -> float:
    """Extract leading number from text; 'a'/'an' → 1."""
    m = re.search(r"(\d+(\.\d+)?)", text)
    if m:
        return float(m.group(1))
    if re.search(r"\ba\b|\ban\b", text):
        return 1.0
    return 1.0


def months_ago_to_label(months: int) -> str:
    """Convert months_ago integer to a YYYY-MM label relative to today."""
    now = datetime.now(timezone.utc)
    year = now.year
    month = now.month - months
    while month <= 0:
        month += 12
        year -= 1
    return f"{year}-{month:02d}"


# ---------------------------------------------------------------------------
# Google Places API helpers
# ---------------------------------------------------------------------------

async def fetch_place_details(place_id: str) -> dict[str, Any]:
    """Fetch place details including reviews from Google Places API."""
    params = {
        "place_id": place_id,
        "fields": "name,rating,reviews,url,user_ratings_total",
        "key": PLACES_API_KEY,
        "reviews_sort": "newest",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(PLACES_DETAILS_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    return data.get("result", {})


# ---------------------------------------------------------------------------
# Playwright scraping helpers
# ---------------------------------------------------------------------------

async def scrape_maps_reviews(maps_url: str, max_reviews: int = 40) -> list[dict]:
    """
    Use Playwright to open the Google Maps URL, navigate to Reviews tab,
    scroll to load more reviews, and extract text + rating + date.
    Returns list of dicts: {text, rating, date_text}
    """
    reviews: list[dict] = []
    try:
        from playwright.async_api import async_playwright  # noqa: PLC0415

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                locale="en-US",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            await page.goto(maps_url, wait_until="domcontentloaded", timeout=20000)

            # Click on the "Reviews" tab if available
            try:
                reviews_tab = page.locator('button[data-tab-index="1"], [aria-label*="Reviews"], button:has-text("Reviews")')
                await reviews_tab.first.click(timeout=3000)
                await page.wait_for_timeout(1000)
            except Exception:
                pass  # Tab may not exist or already on reviews

            # Scroll the reviews panel to load more (reduced from 8 to 3 for speed)
            scroll_container = page.locator('div[role="feed"], div.m6QErb.DxyBCb')
            for _ in range(3):
                try:
                    await scroll_container.first.evaluate("el => el.scrollTop += 1500")
                    await page.wait_for_timeout(600)
                except Exception:
                    await page.evaluate("window.scrollBy(0, 1500)")
                    await page.wait_for_timeout(600)

            content = await page.content()
            await browser.close()

            # Parse with BeautifulSoup
            from bs4 import BeautifulSoup  # noqa: PLC0415

            soup = BeautifulSoup(content, "html.parser")

            # Review blocks — multiple selector strategies
            review_blocks = (
                soup.select("div.jftiEf")
                or soup.select("div[data-review-id]")
                or soup.select("div.WMbnJf")
            )

            for block in review_blocks[:max_reviews]:
                # Rating
                rating_el = block.select_one("span[aria-label*='star'], span.kvMYJc")
                rating = None
                if rating_el:
                    aria = rating_el.get("aria-label", "")
                    m = re.search(r"(\d+(\.\d+)?)", aria)
                    if m:
                        rating = float(m.group(1))

                # Date text
                date_el = block.select_one("span.rsqaWe, span.dehysf")
                date_text = date_el.get_text(strip=True) if date_el else ""

                # Review text
                text_el = block.select_one("span.wiI7pd, span[jsname='bN97Pc']")
                text = text_el.get_text(strip=True) if text_el else ""

                if text or rating:
                    reviews.append(
                        {"text": text, "rating": rating, "date_text": date_text}
                    )

    except Exception as exc:
        # If Playwright fails, return empty list gracefully
        reviews.append({"_scrape_error": str(exc), "text": "", "rating": None, "date_text": ""})

    return reviews


# ---------------------------------------------------------------------------
# Category sentiment helpers
# ---------------------------------------------------------------------------

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "food": [
        "taste", "flavor", "flavour", "food", "dish", "meal", "menu",
        "delicious", "yummy", "fresh", "stale", "bland", "spicy", "sweet",
        "coffee", "cake", "snack", "drink", "sandwich", "breakfast", "lunch",
    ],
    "service": [
        "staff", "waiter", "service", "slow", "fast", "friendly", "rude",
        "attentive", "ignored", "helpful", "quick", "server", "manager",
        "polite", "unprofessional",
    ],
    "ambiance": [
        "atmosphere", "vibe", "decor", "music", "ambiance", "ambience",
        "cozy", "noise", "dirty", "clean", "comfortable", "crowded",
        "lighting", "seating", "interior", "aesthetic",
    ],
    "price": [
        "expensive", "cheap", "value", "price", "worth", "costly",
        "affordable", "overpriced", "reasonable", "pricey", "budget",
        "money", "cost",
    ],
}

POSITIVE_WORDS = {
    "great", "good", "excellent", "amazing", "wonderful", "fantastic",
    "love", "best", "perfect", "nice", "lovely", "recommend", "awesome",
    "delicious", "friendly", "clean", "cozy", "fresh", "quick", "helpful",
    "attentive", "reasonable", "affordable", "worth", "beautiful",
}
NEGATIVE_WORDS = {
    "bad", "terrible", "awful", "horrible", "worst", "poor", "dirty",
    "rude", "slow", "expensive", "overpriced", "stale", "bland", "cold",
    "disappointing", "disgusting", "unpleasant", "ignored", "noisy",
    "crowded", "uncomfortable", "unfriendly", "costly", "pricey",
}


def classify_review_categories(text: str) -> list[str]:
    """Return which categories a review text belongs to (can be multiple)."""
    lower = text.lower()
    matched = []
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            matched.append(cat)
    if not matched:
        matched = ["overall"]
    return matched


def score_sentiment(text: str) -> str:
    """Simple word-match sentiment: positive / negative / neutral."""
    lower = text.lower()
    words = set(re.findall(r"\b\w+\b", lower))
    pos = len(words & POSITIVE_WORDS)
    neg = len(words & NEGATIVE_WORDS)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


def extract_key_phrases(text: str, sentiment: str) -> list[str]:
    """Extract short phrases (up to 6 words) near sentiment trigger words."""
    trigger_set = POSITIVE_WORDS if sentiment == "positive" else NEGATIVE_WORDS
    phrases = []
    lower = text.lower()
    tokens = re.split(r"[.,!?;]", lower)
    for tok in tokens:
        words_in_tok = set(re.findall(r"\b\w+\b", tok))
        if words_in_tok & trigger_set:
            clean = tok.strip()
            if clean and len(clean) > 5:
                phrases.append(clean[:80])
    return phrases[:3]


# ---------------------------------------------------------------------------
# Tool 1: review_trend
# ---------------------------------------------------------------------------

async def _review_trend(place_id: str, months: int = 12) -> dict:
    details = await fetch_place_details(place_id)
    maps_url = details.get("url", f"https://www.google.com/maps/place/?q=place_id:{place_id}")

    api_reviews = details.get("reviews", [])
    all_reviews: list[dict] = []

    # Normalise Places API reviews
    for r in api_reviews:
        all_reviews.append(
            {
                "text": r.get("text", ""),
                "rating": r.get("rating"),
                "date_text": r.get("relative_time_description", ""),
            }
        )

    # Supplement with Playwright scraping
    scraped = await scrape_maps_reviews(maps_url)
    for r in scraped:
        if not r.get("_scrape_error"):
            all_reviews.append(r)

    # De-duplicate by (text[:50], rating)
    seen: set[tuple] = set()
    unique_reviews: list[dict] = []
    for r in all_reviews:
        key = (r["text"][:50], r["rating"])
        if key not in seen:
            seen.add(key)
            unique_reviews.append(r)

    # Build month buckets
    monthly: dict[str, list[float]] = defaultdict(list)
    unclassified: list[float] = []

    for r in unique_reviews:
        rating = r.get("rating")
        if rating is None:
            continue
        date_text = r.get("date_text", "")
        mo_ago = relative_to_months(date_text)
        if mo_ago > months:
            continue  # outside requested window
        label = months_ago_to_label(mo_ago)
        monthly[label].append(float(rating))

    if not monthly and unclassified:
        monthly[months_ago_to_label(0)].extend(unclassified)

    # Compute averages
    trend: list[dict] = []
    for label in sorted(monthly.keys()):
        ratings_list = monthly[label]
        avg = round(sum(ratings_list) / len(ratings_list), 2)
        trend.append({"month": label, "avg_rating": avg, "review_count": len(ratings_list)})

    # Direction
    direction = "stable"
    if len(trend) >= 2:
        first_half = trend[: len(trend) // 2]
        second_half = trend[len(trend) // 2 :]
        avg_first = sum(t["avg_rating"] for t in first_half) / len(first_half)
        avg_second = sum(t["avg_rating"] for t in second_half) / len(second_half)
        diff = avg_second - avg_first
        if diff > 0.2:
            direction = "improving"
        elif diff < -0.2:
            direction = "declining"

    return {
        "place_name": details.get("name", "Unknown"),
        "overall_rating": details.get("rating"),
        "total_reviews": details.get("user_ratings_total"),
        "trend": trend,
        "direction": direction,
        "months_analysed": months,
        "reviews_used": len(unique_reviews),
    }


# ---------------------------------------------------------------------------
# Tool 2: sentiment_by_category
# ---------------------------------------------------------------------------

async def _sentiment_by_category(place_id: str) -> dict:
    details = await fetch_place_details(place_id)
    maps_url = details.get("url", f"https://www.google.com/maps/place/?q=place_id:{place_id}")

    api_reviews = details.get("reviews", [])
    all_reviews: list[dict] = []
    for r in api_reviews:
        all_reviews.append(
            {
                "text": r.get("text", ""),
                "rating": r.get("rating"),
                "date_text": r.get("relative_time_description", ""),
            }
        )
    scraped = await scrape_maps_reviews(maps_url)
    for r in scraped:
        if not r.get("_scrape_error"):
            all_reviews.append(r)

    # Remove empty text
    all_reviews = [r for r in all_reviews if r.get("text", "").strip()]

    # Category analysis
    categories: dict[str, dict] = {
        cat: {"positive": 0, "neutral": 0, "negative": 0, "phrases_pos": [], "phrases_neg": []}
        for cat in list(CATEGORY_KEYWORDS.keys()) + ["overall"]
    }

    for r in all_reviews:
        text = r.get("text", "")
        cats = classify_review_categories(text)
        sentiment = score_sentiment(text)
        for cat in cats:
            if cat not in categories:
                continue
            categories[cat][sentiment] += 1
            if sentiment == "positive":
                categories[cat]["phrases_pos"].extend(extract_key_phrases(text, "positive"))
            elif sentiment == "negative":
                categories[cat]["phrases_neg"].extend(extract_key_phrases(text, "negative"))

    # Trim phrase lists and de-dup
    result_cats: dict = {}
    for cat, data in categories.items():
        total = data["positive"] + data["neutral"] + data["negative"]
        if total == 0:
            continue
        pos_phrases = list(dict.fromkeys(data["phrases_pos"]))[:5]
        neg_phrases = list(dict.fromkeys(data["phrases_neg"]))[:5]
        result_cats[cat] = {
            "positive": data["positive"],
            "neutral": data["neutral"],
            "negative": data["negative"],
            "total_mentions": total,
            "top_positive_phrases": pos_phrases,
            "top_negative_phrases": neg_phrases,
        }

    return {
        "place_name": details.get("name", "Unknown"),
        "reviews_analysed": len(all_reviews),
        "categories": result_cats,
    }


# ---------------------------------------------------------------------------
# Tool 3: inflection_detector
# ---------------------------------------------------------------------------

async def _inflection_detector(place_id: str) -> dict:
    trend_data = await _review_trend(place_id)
    sentiment_data = await _sentiment_by_category(place_id)

    # Find biggest rating drop or rise in trend
    trend = trend_data.get("trend", [])
    inflection_point: str = "No significant inflection detected"
    inflection_details: list[dict] = []

    if len(trend) >= 2:
        for i in range(1, len(trend)):
            delta = trend[i]["avg_rating"] - trend[i - 1]["avg_rating"]
            inflection_details.append(
                {
                    "from_month": trend[i - 1]["month"],
                    "to_month": trend[i]["month"],
                    "from_rating": trend[i - 1]["avg_rating"],
                    "to_rating": trend[i]["avg_rating"],
                    "delta": round(delta, 2),
                }
            )

        if inflection_details:
            biggest = max(inflection_details, key=lambda x: abs(x["delta"]))
            if abs(biggest["delta"]) >= 0.3:
                direction_word = "dropped" if biggest["delta"] < 0 else "rose"
                inflection_point = (
                    f"Rating {direction_word} from {biggest['from_rating']} "
                    f"to {biggest['to_rating']} around {biggest['to_month']}"
                )

    # Prepare summary for Gemini
    summary_data = {
        "trend": trend,
        "direction": trend_data.get("direction"),
        "category_sentiment": {
            cat: {k: v for k, v in vals.items() if k in ("positive", "negative", "total_mentions")}
            for cat, vals in sentiment_data.get("categories", {}).items()
        },
        "inflection_changes": inflection_details[-6:] if inflection_details else [],
    }

    # Gemini analysis
    gemini_insight = "Unable to generate insight."
    opportunity = "Monitor reviews for further changes."
    suspected_cause = "Insufficient data for cause analysis."

    try:
        prompt = (
            "Analyse these review summaries over time and identify what changed and when. "
            "Respond with exactly 2 sentences: first sentence = what changed and when, "
            "second sentence = the most likely cause based on the category sentiment data.\n\n"
            f"Data: {json.dumps(summary_data, indent=2)}"
        )
        gemini_text = (await _gemini_generate(prompt)).strip()
        sentences = re.split(r"(?<=[.!?])\s+", gemini_text)
        if len(sentences) >= 2:
            gemini_insight = sentences[0]
            suspected_cause = sentences[1]
        else:
            gemini_insight = gemini_text
    except Exception as exc:
        gemini_insight = f"Gemini analysis error: {exc}"

    # Opportunity for competitors
    direction = trend_data.get("direction", "stable")
    neg_cats = []
    for cat, vals in sentiment_data.get("categories", {}).items():
        if vals.get("negative", 0) > vals.get("positive", 0):
            neg_cats.append(cat)

    if direction == "declining" and neg_cats:
        opportunity = (
            f"Competitor opportunity: ratings are declining and customers are "
            f"dissatisfied with {', '.join(neg_cats)}. "
            "A business excelling in these areas could capture displaced customers."
        )
    elif direction == "stable":
        opportunity = "Ratings are stable — no immediate competitor opening, but watch for emerging service gaps."
    else:
        opportunity = "Ratings are improving — this venue is strengthening its position."

    return {
        "place_name": trend_data.get("place_name", "Unknown"),
        "inflection_point": inflection_point,
        "inflection_changes": inflection_details,
        "gemini_insight": gemini_insight,
        "suspected_cause": suspected_cause,
        "competitor_opportunity": opportunity,
        "trend_direction": direction,
        "overall_rating": trend_data.get("overall_rating"),
    }


# ---------------------------------------------------------------------------
# MCP Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    Tool(
        name="review_trend",
        description=(
            "Analyse Google Maps review rating trends over time for a place. "
            "Uses Google Places API (5 reviews) + Playwright scraping for more reviews. "
            "Groups reviews by month using relative timestamps and returns rating trend + direction."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "place_id": {
                    "type": "string",
                    "description": "Google Maps place_id (e.g. ChIJ...).",
                },
                "months": {
                    "type": "integer",
                    "description": "Number of months to look back (default 12).",
                    "default": 12,
                },
            },
            "required": ["place_id"],
        },
    ),
    Tool(
        name="sentiment_by_category",
        description=(
            "Classify Google Maps reviews into categories (food, service, ambiance, price, overall) "
            "using keyword matching and return positive/neutral/negative counts with top phrases."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "place_id": {
                    "type": "string",
                    "description": "Google Maps place_id.",
                },
            },
            "required": ["place_id"],
        },
    ),
    Tool(
        name="inflection_detector",
        description=(
            "Detect significant rating changes over time for a place, identify suspected causes "
            "using Gemini AI, and surface competitor opportunities."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "place_id": {
                    "type": "string",
                    "description": "Google Maps place_id.",
                },
            },
            "required": ["place_id"],
        },
    ),
]


# ---------------------------------------------------------------------------
# MCP Server handlers
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "review_trend":
            place_id = arguments["place_id"]
            months = int(arguments.get("months", 12))
            result = await _review_trend(place_id, months)

        elif name == "sentiment_by_category":
            place_id = arguments["place_id"]
            result = await _sentiment_by_category(place_id)

        elif name == "inflection_detector":
            place_id = arguments["place_id"]
            result = await _inflection_detector(place_id)

        else:
            result = {"error": f"Unknown tool: {name}"}

    except Exception as exc:
        result = {"error": str(exc), "tool": name}

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
