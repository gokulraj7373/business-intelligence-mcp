#!/usr/bin/env python3
"""
Business Idea Validator MCP
Orchestrates: trends + delivery_intel + local_seo + metasearch + review_analytics
One command → structured GO / RISKY / NO-GO verdict with evidence.
"""

import os
import asyncio
import sys
import time
from datetime import datetime
from typing import Any

sys.path.insert(0, "C:/c4")

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

app = Server("idea-validator")

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GMAPS_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
# ── safe importers (graceful if a module not ready) ──────────────────────────

def _try_import(module: str, fn: str):
    try:
        import importlib
        mod = importlib.import_module(module)
        return getattr(mod, fn, None)
    except Exception:
        return None


# ── scoring helpers ──────────────────────────────────────────────────────────

def _score_band(score: float) -> str:
    if score >= 70: return "GO"
    if score >= 45: return "RISKY"
    return "NO-GO"

def _confidence_label(n_sources: int) -> str:
    if n_sources >= 4: return "High"
    if n_sources >= 2: return "Medium"
    return "Low"


async def _fetch_trend_signal(concept: str, location: str) -> dict:
    """Call trends module directly."""
    try:
        import importlib, sys
        if "trends" in sys.modules:
            del sys.modules["trends"]
        trends = importlib.import_module("trends")
        result = await trends._trend_score_impl(concept, location)
        return result
    except Exception as e:
        return {"error": str(e), "score": 50, "direction": "unknown"}


async def _fetch_competitor_signal(concept: str, location: str) -> dict:
    """Call local_seo module directly."""
    try:
        import importlib
        lseo = importlib.import_module("local_seo")
        results = await lseo._places_text_search(concept, location)
        count = len(results)
        avg_rating = sum(p.get("rating", 0) for p in results) / max(count, 1)
        beatable = sum(1 for p in results if p.get("rating", 0) < 4.2)
        return {
            "competitor_count": count,
            "avg_rating": round(avg_rating, 1),
            "beatable_count": beatable,
            "saturation": "high" if count > 10 else "medium" if count > 4 else "low",
        }
    except Exception as e:
        return {"error": str(e), "competitor_count": 0, "saturation": "unknown"}


async def _fetch_demand_signal(concept: str, location: str) -> dict:
    """Call metasearch for web + news demand signals."""
    try:
        import importlib
        ms = importlib.import_module("metasearch")
        web = await ms._ddg_search(f"{concept} {location}", 5)
        news = await ms._google_news_rss(f"{concept} food trend India", 5)
        return {
            "web_results": len(web),
            "news_results": len(news),
            "top_news": [n["title"] for n in news[:3]],
        }
    except Exception as e:
        return {"error": str(e), "web_results": 0, "news_results": 0}


async def _fetch_delivery_signal(concept: str, city: str) -> dict:
    """Check delivery platform presence (light check, no heavy scraping)."""
    try:
        import httpx
        city_slug = city.lower().replace(" ", "-").split(",")[0]
        url = f"https://www.zomato.com/{city_slug}/restaurants?q={concept.replace(' ', '+')}"
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"},
            timeout=10, follow_redirects=True
        ) as client:
            r = await client.get(url)
        text = r.text
        # Count restaurant card occurrences as proxy for results
        count = text.lower().count('"resId"') or text.lower().count('data-res-id')
        has_bestseller = "bestseller" in text.lower()
        return {
            "zomato_count": min(count, 50),
            "has_bestseller_items": has_bestseller,
            "delivery_demand": "proven" if count > 3 else "limited",
        }
    except Exception as e:
        return {"error": str(e), "delivery_demand": "unknown"}


def _unit_economics(monthly_customers: int, price_level: int, concept: str) -> dict:
    """Estimate unit economics for a cafe concept."""
    spend_map = {1: 80, 2: 180, 3: 350, 4: 600}
    avg_spend = spend_map.get(price_level, 180)

    # COGS ratio varies by concept
    concept_lower = concept.lower()
    if any(w in concept_lower for w in ["coffee", "tea", "beverage", "drink"]):
        cogs_ratio = 0.28  # beverages have ~72% gross margin
    elif any(w in concept_lower for w in ["cake", "pastry", "bakery", "dessert"]):
        cogs_ratio = 0.38
    else:
        cogs_ratio = 0.42  # food in general

    monthly_revenue = monthly_customers * avg_spend
    monthly_cogs    = monthly_revenue * cogs_ratio
    gross_profit    = monthly_revenue - monthly_cogs
    gross_margin    = 1 - cogs_ratio

    # Rough break-even (fixed costs: ₹40k rent + ₹30k salaries + ₹10k utilities)
    fixed_costs = 80000
    break_even_customers = round(fixed_costs / (avg_spend * (1 - cogs_ratio)))

    return {
        "avg_spend_inr":          avg_spend,
        "monthly_revenue_inr":    round(monthly_revenue),
        "gross_margin_pct":       round(gross_margin * 100),
        "gross_profit_inr":       round(gross_profit),
        "break_even_customers":   break_even_customers,
        "viable":                 gross_margin >= 0.50,
    }


def _build_verdict(
    trend: dict,
    competitors: dict,
    demand: dict,
    delivery: dict,
    econ: dict,
    concept: str,
    location: str,
) -> tuple[str, float, str]:
    """
    Combine signals into a 0-100 score and GO/RISKY/NO-GO verdict.
    Returns (verdict, score, reasoning)
    """
    score = 50.0
    reasons_for   = []
    reasons_against = []
    n_sources = 0

    # — Trend signal (0-25 pts)
    td = trend.get("direction", "unknown")
    ts = trend.get("score", 50)
    if "error" not in trend:
        n_sources += 1
        if td == "rising":
            score += 20; reasons_for.append(f"Search interest rising ({ts}/100 nationally)")
        elif td == "flat":
            score += 5;  reasons_for.append(f"Steady search interest ({ts}/100)")
        elif td == "falling":
            score -= 15; reasons_against.append(f"Search interest declining ({ts}/100) — fading trend")

    # — Competition signal (0-20 pts)
    sat = competitors.get("saturation", "unknown")
    cnt = competitors.get("competitor_count", 0)
    if "error" not in competitors:
        n_sources += 1
        if sat == "low":
            score += 20; reasons_for.append(f"Low competition — only {cnt} competitors found")
        elif sat == "medium":
            score += 8;  reasons_for.append(f"Moderate competition ({cnt} competitors, {competitors.get('beatable_count',0)} below 4.2★)")
        else:
            score -= 10; reasons_against.append(f"Saturated market — {cnt}+ competitors already present")

    # — Delivery/demand proof (0-15 pts)
    if "error" not in delivery:
        n_sources += 1
        dd = delivery.get("delivery_demand", "unknown")
        if dd == "proven":
            score += 15; reasons_for.append("Proven delivery demand on Zomato")
        elif dd == "limited":
            score += 0;  reasons_against.append("Limited delivery presence — unproven demand on platforms")
        if delivery.get("has_bestseller_items"):
            score += 5;  reasons_for.append("Bestseller items exist in this category")

    # — News/web interest (0-10 pts)
    if "error" not in demand:
        n_sources += 1
        if demand.get("news_results", 0) >= 3:
            score += 10; reasons_for.append(f"{demand['news_results']} recent news stories about this trend")
        elif demand.get("news_results", 0) >= 1:
            score += 4

    # — Unit economics (0-15 pts)
    if econ.get("viable"):
        score += 15; reasons_for.append(f"{econ['gross_margin_pct']}% gross margin — strong unit economics")
    else:
        score -= 10; reasons_against.append(f"Thin margins ({econ['gross_margin_pct']}%) — difficult to sustain")

    score = max(0, min(100, score))
    verdict = _score_band(score)
    confidence = _confidence_label(n_sources)

    return verdict, round(score, 1), confidence, reasons_for, reasons_against


# ── tools ────────────────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="validate_idea",
            description=(
                "Full business idea validation pipeline. "
                "Checks demand trends, competition density, delivery platform presence, "
                "unit economics, and news signals. Returns a structured GO / RISKY / NO-GO "
                "verdict with evidence and the single most important thing to validate next."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "concept": {
                        "type": "string",
                        "description": "Business concept e.g. 'bubble tea', 'specialty cold brew coffee', 'Korean fried chicken'"
                    },
                    "location": {
                        "type": "string",
                        "default": "Tirupur Tamil Nadu",
                        "description": "Target location e.g. 'Tirupur Tamil Nadu', 'Coimbatore'"
                    },
                    "price_level": {
                        "type": "integer",
                        "default": 2,
                        "description": "1=budget(under ₹100), 2=mid(₹100-300), 3=premium(₹300-600), 4=luxury(₹600+)"
                    },
                    "monthly_customers_estimate": {
                        "type": "integer",
                        "default": 900,
                        "description": "Conservative monthly customer estimate (30/day × 30 days)"
                    },
                },
                "required": ["concept"],
            },
        ),
        types.Tool(
            name="compare_ideas",
            description=(
                "Compare 2-4 business concepts side by side. "
                "Runs validate_idea for each and returns a ranked comparison table. "
                "Use to choose between business ideas before committing."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "concepts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of 2-4 concepts to compare"
                    },
                    "location": {
                        "type": "string",
                        "default": "Tirupur Tamil Nadu"
                    },
                    "price_level": {"type": "integer", "default": 2},
                },
                "required": ["concepts"],
            },
        ),
        types.Tool(
            name="quick_screen",
            description=(
                "Fast 10-second pre-screen of a concept using only search signals. "
                "No scraping — use this before running the full validate_idea. "
                "Returns: trending/flat/dying + quick competition read."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "concept": {"type": "string"},
                    "location": {"type": "string", "default": "India"},
                },
                "required": ["concept"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        if name == "validate_idea":
            return await _handle_validate(arguments)
        elif name == "compare_ideas":
            return await _handle_compare(arguments)
        elif name == "quick_screen":
            return await _handle_quick_screen(arguments)
        return [types.TextContent(type="text", text=f"Unknown: {name}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"Error: {e}")]


async def _handle_validate(args: dict) -> list[types.TextContent]:
    concept   = args["concept"]
    location  = args.get("location", "Tirupur Tamil Nadu")
    price_lvl = args.get("price_level", 2)
    cust_est  = args.get("monthly_customers_estimate", 900)
    city      = location.split()[0].lower()

    lines = [
        f"# Business Idea Validation: {concept}",
        f"Location: {location} | Price level: {'₹' * price_lvl} | {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "\n_Gathering signals from 4 independent sources..._\n",
    ]

    # Run all signal gathering in parallel
    trend, competitors, demand, delivery = await asyncio.gather(
        _fetch_trend_signal(concept, location),
        _fetch_competitor_signal(concept, location),
        _fetch_demand_signal(concept, location),
        _fetch_delivery_signal(concept, city),
        return_exceptions=False,
    )
    econ = _unit_economics(cust_est, price_lvl, concept)

    verdict, score, confidence, pros, cons = _build_verdict(
        trend, competitors, demand, delivery, econ, concept, location
    )

    # Verdict banner
    emoji = {"GO": "✅", "RISKY": "⚠️", "NO-GO": "❌"}[verdict]
    lines += [
        f"## {emoji} Verdict: **{verdict}** (Score: {score}/100 · {confidence} confidence)\n",
    ]

    # Signal breakdown
    lines.append("## Signal Breakdown\n")

    # Trend
    if "error" not in trend:
        d = trend.get("direction", "?")
        arrow = {"rising": "↑", "flat": "→", "falling": "↓"}.get(d, "?")
        lines.append(f"**Demand Trend** {arrow}")
        lines.append(f"- Search interest: {trend.get('score', '?')}/100 nationally")
        lines.append(f"- Direction: {d.title()} | Peak month: {trend.get('peak_month', 'N/A')}\n")
    else:
        lines.append(f"**Demand Trend** — _trend data unavailable_\n")

    # Competition
    lines.append("**Competition**")
    lines.append(f"- {competitors.get('competitor_count', '?')} competitors found in {location}")
    lines.append(f"- Avg competitor rating: {competitors.get('avg_rating', '?')}★")
    lines.append(f"- Market saturation: {competitors.get('saturation', 'unknown').title()}")
    lines.append(f"- Beatable (under 4.2★): {competitors.get('beatable_count', '?')}\n")

    # Delivery
    lines.append("**Delivery Platform Signals**")
    lines.append(f"- Zomato presence: {delivery.get('zomato_count', '?')} results")
    lines.append(f"- Delivery demand: {delivery.get('delivery_demand', 'unknown').title()}")
    lines.append(f"- Bestseller items exist: {'Yes' if delivery.get('has_bestseller_items') else 'No'}\n")

    # Unit economics
    lines.append("**Unit Economics**")
    lines.append(f"- Avg spend: ₹{econ['avg_spend_inr']}/customer")
    lines.append(f"- Gross margin: {econ['gross_margin_pct']}%")
    lines.append(f"- Monthly revenue (est.): ₹{econ['monthly_revenue_inr']:,}")
    lines.append(f"- Break-even at: {econ['break_even_customers']} customers/month\n")

    # Evidence
    if pros:
        lines.append("## Evidence FOR")
        for p in pros: lines.append(f"- ✓ {p}")
        lines.append("")

    if cons:
        lines.append("## Evidence AGAINST")
        for c in cons: lines.append(f"- ✗ {c}")
        lines.append("")

    # Validate-next recommendation
    lines.append("## Before You Commit — Validate This One Thing")
    if verdict == "GO":
        lines.append(
            f"Run a **2-week pop-up** or add {concept} as a limited special. "
            "Track reorder rate (target: >30% of first-timers reorder). "
            "If reorder rate hits target, scale with confidence."
        )
    elif verdict == "RISKY":
        lines.append(
            f"Do a **price sensitivity test**: offer {concept} at 3 price points to 30 customers. "
            "Find the sweet spot before committing to equipment or full menu addition. "
            "Main risk to resolve: " + (cons[0] if cons else "market acceptance")
        )
    else:
        lines.append(
            f"This concept has too many red flags for Tirupur right now. "
            "Consider: (1) a different location, (2) a lower price point, "
            "or (3) pairing with a proven concept to reduce risk. "
            "Biggest blocker: " + (cons[0] if cons else "low demand")
        )

    return [types.TextContent(type="text", text="\n".join(lines))]


async def _handle_compare(args: dict) -> list[types.TextContent]:
    concepts  = args["concepts"][:4]
    location  = args.get("location", "Tirupur Tamil Nadu")
    price_lvl = args.get("price_level", 2)

    lines = [f"# Concept Comparison — {location}\n"]

    # Run all validations in parallel
    tasks = [
        _handle_validate({"concept": c, "location": location, "price_level": price_lvl})
        for c in concepts
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    scores = []
    for concept, result in zip(concepts, results):
        if isinstance(result, Exception):
            scores.append((concept, 0, "ERROR"))
            continue
        text = result[0].text if result else ""
        # Extract score from output
        import re
        m = re.search(r"Score:\s*([\d.]+)/100", text)
        sc = float(m.group(1)) if m else 50
        m2 = re.search(r"Verdict:\s*\*\*(\w+)\*\*", text)
        vd = m2.group(1) if m2 else "RISKY"
        scores.append((concept, sc, vd))

    scores.sort(key=lambda x: x[1], reverse=True)

    lines.append("## Ranked by Opportunity Score\n")
    for rank, (concept, score, verdict) in enumerate(scores, 1):
        emoji = {"GO": "✅", "RISKY": "⚠️", "NO-GO": "❌"}.get(verdict, "?")
        lines.append(f"### #{rank} {concept} — {emoji} {verdict} ({score}/100)")

    lines.append("\n---\n")
    lines.append(f"**Recommendation:** Focus on **{scores[0][0]}** first — highest opportunity score.")
    lines.append(f"If it fails validation testing, fall back to **{scores[1][0]}** if available.")

    return [types.TextContent(type="text", text="\n".join(lines))]


async def _handle_quick_screen(args: dict) -> list[types.TextContent]:
    concept  = args["concept"]
    location = args.get("location", "India")

    demand, competitors = await asyncio.gather(
        _fetch_demand_signal(concept, location),
        _fetch_competitor_signal(concept, location),
    )

    sat = competitors.get("saturation", "unknown")
    nr  = demand.get("news_results", 0)
    top = demand.get("top_news", [])

    lines = [
        f"## Quick Screen: {concept}\n",
        f"- Competition: {sat.title()} ({competitors.get('competitor_count', '?')} found)",
        f"- News signal: {nr} recent articles",
    ]
    if top:
        lines.append(f"- Top story: _{top[0][:80]}_")
    lines.append(f"\n_Run `validate_idea` for the full GO/NO-GO verdict._")

    return [types.TextContent(type="text", text="\n".join(lines))]


# ── entrypoint ───────────────────────────────────────────────────────────────
async def main():
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
