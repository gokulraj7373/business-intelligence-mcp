"""
signal_monitor.py — Competitor Signal Monitor MCP Server
=========================================================
Detects competitor moves early via job postings, social momentum,
and news mentions. Exposes 3 MCP tools:

  • scan_job_postings     — hiring signals on Naukri + Indeed India
  • social_momentum       — Instagram engagement momentum scoring
  • weekly_alert_digest   — combined weekly digest with threat level

Server name : signal-monitor
Transport   : stdio (MCP SDK default)
Python      : C:\\c4\\venv\\Scripts\\python.exe
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import httpx
from bs4 import BeautifulSoup
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

SIGNAL_RULES: list[dict[str, Any]] = [
    {
        "pattern": re.compile(r"\bhead\s+chef\b|\bexecutive\s+chef\b|\bchef\s+de\s+cuisine\b", re.I),
        "signal": "Menu overhaul or cuisine repositioning — likely 6-8 weeks to launch",
        "threat": "medium",
    },
    {
        "pattern": re.compile(r"\b(5|six|6|seven|7|8|9|10)\+?\s*(kitchen|cook|chef|commis)\b", re.I),
        "signal": "Large kitchen expansion — new outlet or capacity increase incoming",
        "threat": "high",
    },
    {
        "pattern": re.compile(r"\brestaurant\s+manager\b|\boutlet\s+manager\b|\bgeneral\s+manager\b", re.I),
        "signal": "New outlet management hire — expansion highly likely within 4-6 weeks",
        "threat": "high",
    },
    {
        "pattern": re.compile(r"\bbarista\b|\bcoffee\s+specialist\b|\bbrew\s+master\b", re.I),
        "signal": "Specialty coffee focus — potential café concept or beverage menu upgrade",
        "threat": "medium",
    },
    {
        "pattern": re.compile(r"\bsocial\s+media\b|\bcontent\s+creator\b|\bdigital\s+market", re.I),
        "signal": "Digital marketing push — expect increased online presence and promotions",
        "threat": "low",
    },
    {
        "pattern": re.compile(r"\bcatering\s+manager\b|\bevent\s+coordinat", re.I),
        "signal": "Catering/events vertical launch — new revenue stream competition",
        "threat": "medium",
    },
    {
        "pattern": re.compile(r"\bfranchise\b|\bfranchisee\b|\bmaster\s+franchise\b", re.I),
        "signal": "Franchise expansion — aggressive geographic growth planned",
        "threat": "high",
    },
]


def random_ua() -> str:
    return random.choice(USER_AGENTS)


async def polite_delay() -> None:
    """Wait 1-3 seconds between requests — human-like pacing."""
    await asyncio.sleep(random.uniform(1.0, 3.0))


def interpret_signals(title: str, skills: str) -> dict[str, str]:
    """Match job title/skills against signal rules; return first match."""
    combined = f"{title} {skills}"
    for rule in SIGNAL_RULES:
        if rule["pattern"].search(combined):
            return {"signal": rule["signal"], "threat": rule["threat"]}
    return {"signal": "General hiring activity — monitor for follow-up roles", "threat": "low"}


# ---------------------------------------------------------------------------
# Tool 1: scan_job_postings
# ---------------------------------------------------------------------------

async def _scrape_naukri(keyword: str, location: str, browser_context: Any) -> list[dict]:
    """Scrape Naukri.com for job postings."""
    results: list[dict] = []
    slug_kw = keyword.lower().replace(" ", "-")
    slug_loc = location.lower().replace(" ", "-")
    url = f"https://www.naukri.com/{slug_kw}-jobs-in-{slug_loc}"

    try:
        page = await browser_context.new_page()
        await page.set_extra_http_headers({"User-Agent": random_ua()})
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)

        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")

        # Naukri job cards: article.jobTuple or div[data-job-id]
        cards = soup.select("article.jobTuple, div.job-post-day")
        if not cards:
            cards = soup.select("[data-job-id]")

        for card in cards[:15]:
            title_el = card.select_one("a.title, .jobTitle, h2 a, [class*='title']")
            company_el = card.select_one("a.subTitle, .companyName, [class*='company']")
            date_el = card.select_one("span.fleft.grey-text, .jobPostDate, [class*='date']")
            skills_el = card.select_one("ul.tags-gt, .skill-container, [class*='skill']")

            title = title_el.get_text(strip=True) if title_el else "Unknown Title"
            company = company_el.get_text(strip=True) if company_el else "Unknown Company"
            posted = date_el.get_text(strip=True) if date_el else "Recent"
            skills = skills_el.get_text(" ", strip=True) if skills_el else ""

            if title == "Unknown Title" and company == "Unknown Company":
                continue

            sig = interpret_signals(title, skills)
            results.append(
                {
                    "source": "naukri",
                    "keyword": keyword,
                    "title": title,
                    "company": company,
                    "posted": posted,
                    "skills": skills[:200],
                    "signal": sig["signal"],
                    "threat_level": sig["threat"],
                    "url": url,
                }
            )
        await page.close()
    except Exception as exc:
        results.append({"source": "naukri", "keyword": keyword, "error": str(exc), "url": url})

    return results


async def _scrape_indeed(keyword: str, location: str, browser_context: Any) -> list[dict]:
    """Scrape Indeed India for job postings."""
    results: list[dict] = []
    url = f"https://in.indeed.com/jobs?q={keyword.replace(' ', '+')}&l={location.replace(' ', '+')}"

    try:
        page = await browser_context.new_page()
        await page.set_extra_http_headers({"User-Agent": random_ua()})
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)

        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")

        # Indeed job cards
        cards = soup.select("div.job_seen_beacon, div.tapItem, li[class*='css-']")
        if not cards:
            cards = soup.select("[data-jk]")

        for card in cards[:15]:
            title_el = card.select_one("h2.jobTitle span, a.jcs-JobTitle span, span[title]")
            company_el = card.select_one(
                "span.companyName, [data-testid='company-name'], .css-1h7lukg"
            )
            date_el = card.select_one("span.date, [data-testid='myJobsStateDate']")
            skills_el = card.select_one(".metadata, .jobCardShelfContainer")

            title = title_el.get_text(strip=True) if title_el else "Unknown Title"
            company = company_el.get_text(strip=True) if company_el else "Unknown Company"
            posted = date_el.get_text(strip=True) if date_el else "Recent"
            skills = skills_el.get_text(" ", strip=True) if skills_el else ""

            if title == "Unknown Title" and company == "Unknown Company":
                continue

            sig = interpret_signals(title, skills)
            results.append(
                {
                    "source": "indeed_india",
                    "keyword": keyword,
                    "title": title,
                    "company": company,
                    "posted": posted,
                    "skills": skills[:200],
                    "signal": sig["signal"],
                    "threat_level": sig["threat"],
                    "url": url,
                }
            )
        await page.close()
    except Exception as exc:
        results.append({"source": "indeed_india", "keyword": keyword, "error": str(exc), "url": url})

    return results


async def scan_job_postings(keywords: list[str], location: str = "tirupur") -> dict:
    """
    Scrape Naukri.com and Indeed India for job postings matching keywords.
    Returns postings with signal interpretations revealing competitor expansion plans.
    """
    all_postings: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=random_ua(),
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={
                "Accept-Language": "en-IN,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )

        tasks = []
        for kw in keywords:
            tasks.append(_scrape_naukri(kw, location, context))
            tasks.append(_scrape_indeed(kw, location, context))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, list):
                all_postings.extend(r)

        await browser.close()

    # Group signals by company
    company_signals: dict[str, list[str]] = {}
    for p in all_postings:
        if "error" not in p:
            company = p.get("company", "Unknown")
            if company not in company_signals:
                company_signals[company] = []
            company_signals[company].append(p.get("signal", ""))

    # Deduplicate signals per company
    company_intel = {
        company: list(dict.fromkeys(sigs))  # preserve order, deduplicate
        for company, sigs in company_signals.items()
        if company != "Unknown Company"
    }

    return {
        "scan_date": datetime.now(timezone.utc).isoformat(),
        "location": location,
        "keywords_searched": keywords,
        "total_postings_found": sum(1 for p in all_postings if "error" not in p),
        "postings": all_postings,
        "competitor_intelligence": company_intel,
        "insight": (
            "Job postings reveal competitor expansion plans 4-6 weeks before public announcement. "
            "High-threat signals (manager + kitchen hires together) indicate imminent new outlet."
        ),
    }


# ---------------------------------------------------------------------------
# Tool 2: social_momentum
# ---------------------------------------------------------------------------

def _calculate_momentum(
    followers: int | None,
    post_count: int | None,
    recent_visible: int,
) -> int:
    """
    Score 0-100 based on follower count relative to expected range,
    total posts, and recent activity density.
    """
    score = 0

    # Follower contribution (0-40 pts): log-scaled for café scale
    if followers:
        if followers >= 50_000:
            score += 40
        elif followers >= 20_000:
            score += 30
        elif followers >= 10_000:
            score += 22
        elif followers >= 5_000:
            score += 15
        elif followers >= 1_000:
            score += 8
        else:
            score += 3

    # Post count contribution (0-30 pts)
    if post_count:
        if post_count >= 500:
            score += 30
        elif post_count >= 200:
            score += 22
        elif post_count >= 100:
            score += 15
        elif post_count >= 50:
            score += 10
        else:
            score += 5

    # Recent activity (0-30 pts): posts visible in grid (last 12 typically shown)
    # More recent posts = higher recency score
    if recent_visible >= 12:
        score += 30
    elif recent_visible >= 9:
        score += 22
    elif recent_visible >= 6:
        score += 14
    elif recent_visible >= 3:
        score += 7
    else:
        score += 0

    return min(score, 100)


def _parse_count(text: str) -> int | None:
    """Parse '12.3K', '1.2M', '456' into integer."""
    if not text:
        return None
    text = text.strip().replace(",", "")
    m = re.search(r"([\d.]+)\s*([KkMm]?)", text)
    if not m:
        return None
    num = float(m.group(1))
    suffix = m.group(2).upper()
    if suffix == "K":
        num *= 1_000
    elif suffix == "M":
        num *= 1_000_000
    return int(num)


async def _scrape_instagram_direct(handle: str, page: Any) -> dict:
    """Try Instagram directly; return partial dict or raise."""
    url = f"https://www.instagram.com/{handle}/"
    await page.goto(url, wait_until="domcontentloaded", timeout=25_000)

    # Wait for profile to load — try avatar or meta description
    try:
        await page.wait_for_selector(
            '[data-testid="user-avatar"], header section img, ._aaar img, '
            'meta[property="og:description"]',
            timeout=10_000,
        )
    except Exception:
        pass  # continue and try to parse whatever loaded

    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")

    # Check for login wall
    login_wall = soup.select_one('[data-testid="login-button"], a[href="/accounts/login/"]')
    if login_wall:
        raise ValueError("Instagram login wall — switching to Picuki fallback")

    # Try JSON embedded data first (most reliable)
    followers = following = post_count = None
    bio = ""

    script_tags = soup.find_all("script", type="application/json")
    for script in script_tags:
        try:
            data = json.loads(script.string or "")
            text = json.dumps(data)
            # Look for follower count patterns in JSON blob
            m_fol = re.search(r'"edge_followed_by"\s*:\s*\{"count"\s*:\s*(\d+)', text)
            m_fng = re.search(r'"edge_follow"\s*:\s*\{"count"\s*:\s*(\d+)', text)
            m_pst = re.search(r'"edge_owner_to_timeline_media"\s*:\s*\{"count"\s*:\s*(\d+)', text)
            m_bio = re.search(r'"biography"\s*:\s*"([^"]*)"', text)
            if m_fol:
                followers = int(m_fol.group(1))
            if m_fng:
                following = int(m_fng.group(1))
            if m_pst:
                post_count = int(m_pst.group(1))
            if m_bio:
                bio = m_bio.group(1)
            if followers is not None:
                break
        except Exception:
            continue

    # Fallback: try meta description ("X Followers, Y Following, Z Posts")
    if followers is None:
        meta_desc = soup.find("meta", attrs={"property": "og:description"})
        if meta_desc:
            content = meta_desc.get("content", "")
            m = re.search(r"([\d,.KkMm]+)\s*Followers", content, re.I)
            if m:
                followers = _parse_count(m.group(1))
            m = re.search(r"([\d,.KkMm]+)\s*Following", content, re.I)
            if m:
                following = _parse_count(m.group(1))
            m = re.search(r"([\d,.KkMm]+)\s*Posts", content, re.I)
            if m:
                post_count = _parse_count(m.group(1))

    # Count visible recent posts in grid
    recent_posts = soup.select(
        "article img, div._aagw img, div[style*='padding-bottom'] img, "
        "div.v1Nh3 img, div[class*='KL4Bh'] img"
    )
    recent_visible = min(len(recent_posts), 12)

    return {
        "source": "instagram_direct",
        "handle": handle,
        "url": url,
        "followers": followers,
        "following": following,
        "post_count": post_count,
        "bio": bio[:300] if bio else "",
        "recent_posts_visible": recent_visible,
        "momentum_score": _calculate_momentum(followers, post_count, recent_visible),
    }


async def _scrape_picuki(handle: str, client: httpx.AsyncClient) -> dict:
    """Fallback: scrape Picuki.com public Instagram viewer."""
    url = f"https://www.picuki.com/profile/{handle}"
    followers = following = post_count = None
    bio = ""

    try:
        r = await client.get(url, timeout=20.0)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Picuki profile stats
        stats = soup.select(".profile-stats-item")
        for stat in stats:
            label_el = stat.select_one(".profile-stats-label")
            value_el = stat.select_one(".profile-stats-number")
            if not label_el or not value_el:
                continue
            label = label_el.get_text(strip=True).lower()
            value = _parse_count(value_el.get_text(strip=True))
            if "follower" in label:
                followers = value
            elif "following" in label:
                following = value
            elif "post" in label:
                post_count = value

        bio_el = soup.select_one(".profile-description, .biography")
        if bio_el:
            bio = bio_el.get_text(strip=True)[:300]

        recent_imgs = soup.select(".box-photo img, .photo img, .photos-item img")
        recent_visible = min(len(recent_imgs), 12)

    except Exception as exc:
        return {
            "source": "picuki_fallback",
            "handle": handle,
            "url": url,
            "error": str(exc),
            "followers": None,
            "following": None,
            "post_count": None,
            "bio": "",
            "recent_posts_visible": 0,
            "momentum_score": 0,
        }

    return {
        "source": "picuki_fallback",
        "handle": handle,
        "url": url,
        "followers": followers,
        "following": following,
        "post_count": post_count,
        "bio": bio,
        "recent_posts_visible": recent_visible,
        "momentum_score": _calculate_momentum(followers, post_count, recent_visible),
    }


async def social_momentum(instagram_handles: list[str]) -> dict:
    """
    Scrape public Instagram profiles. Falls back to Picuki if Instagram blocks.
    Returns follower counts, post frequency estimate, and momentum score (0-100).
    """
    results: list[dict] = []
    picuki_handles: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=random_ua(),
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            viewport={"width": 1440, "height": 900},
        )

        for handle in instagram_handles:
            page = await context.new_page()
            try:
                data = await _scrape_instagram_direct(handle, page)
                results.append(data)
            except ValueError:
                # Login wall — queue for Picuki fallback
                picuki_handles.append(handle)
                results.append({"handle": handle, "_needs_picuki": True})
            except Exception as exc:
                # Other error — also try Picuki
                picuki_handles.append(handle)
                results.append({"handle": handle, "_needs_picuki": True, "_ig_error": str(exc)})
            finally:
                await page.close()

            await polite_delay()

        await browser.close()

    # Picuki fallback for blocked handles
    if picuki_handles:
        headers = {
            "User-Agent": random_ua(),
            "Accept-Language": "en-IN,en;q=0.9",
        }
        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            picuki_tasks = [_scrape_picuki(h, client) for h in picuki_handles]
            picuki_results = await asyncio.gather(*picuki_tasks, return_exceptions=True)

        # Replace placeholder entries with Picuki results
        picuki_map = {}
        for pr in picuki_results:
            if isinstance(pr, dict):
                picuki_map[pr["handle"]] = pr

        results = [
            picuki_map.get(r.get("handle", ""), r)
            if r.get("_needs_picuki")
            else r
            for r in results
        ]

    # Sort by momentum score descending
    clean_results = [r for r in results if not r.get("_needs_picuki")]
    clean_results.sort(key=lambda x: x.get("momentum_score", 0), reverse=True)

    # Interpretation
    interpretations = []
    for r in clean_results:
        score = r.get("momentum_score", 0)
        handle = r.get("handle", "?")
        if score >= 70:
            interp = f"@{handle}: HIGH momentum — active marketing push, expect promotions/launches soon"
        elif score >= 40:
            interp = f"@{handle}: MODERATE momentum — steady presence, watch for spikes"
        else:
            interp = f"@{handle}: LOW momentum — minimal recent activity"
        interpretations.append(interp)

    return {
        "scan_date": datetime.now(timezone.utc).isoformat(),
        "handles_scanned": len(instagram_handles),
        "profiles": clean_results,
        "momentum_interpretations": interpretations,
        "scoring_guide": {
            "0-39": "Low activity — competitor not investing in social",
            "40-69": "Moderate — normal operations, monitor weekly",
            "70-100": "High — marketing push active, respond with own campaign",
        },
    }


# ---------------------------------------------------------------------------
# Tool 3: weekly_alert_digest
# ---------------------------------------------------------------------------

async def _fetch_google_news(competitor_name: str, client: httpx.AsyncClient) -> list[dict]:
    """Fetch Google News RSS for a competitor name."""
    news_items: list[dict] = []
    query = competitor_name.replace(" ", "+") + "+tirupur"
    url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"

    try:
        r = await client.get(url, timeout=15.0)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        channel = root.find("channel")
        if channel is None:
            return news_items

        for item in channel.findall("item")[:5]:
            title_el = item.find("title")
            link_el = item.find("link")
            pub_el = item.find("pubDate")
            desc_el = item.find("description")

            news_items.append(
                {
                    "title": title_el.text if title_el is not None else "",
                    "url": link_el.text if link_el is not None else "",
                    "published": pub_el.text if pub_el is not None else "",
                    "snippet": BeautifulSoup(
                        desc_el.text or "", "html.parser"
                    ).get_text(strip=True)[:200]
                    if desc_el is not None
                    else "",
                }
            )
    except Exception as exc:
        news_items.append({"error": str(exc), "query": query})

    return news_items


def _threat_level(job_postings: list[dict], momentum: dict | None, news: list[dict]) -> str:
    """Compute overall threat level: LOW / MEDIUM / HIGH / CRITICAL."""
    threat_score = 0

    # Job posting signals
    for p in job_postings:
        t = p.get("threat_level", "low")
        if t == "high":
            threat_score += 3
        elif t == "medium":
            threat_score += 2
        else:
            threat_score += 1

    # Social momentum
    if momentum:
        ms = momentum.get("momentum_score", 0)
        if ms >= 70:
            threat_score += 3
        elif ms >= 40:
            threat_score += 2
        else:
            threat_score += 1

    # News mentions
    real_news = [n for n in news if "error" not in n and n.get("title")]
    threat_score += len(real_news)

    if threat_score >= 8:
        return "CRITICAL"
    elif threat_score >= 5:
        return "HIGH"
    elif threat_score >= 3:
        return "MEDIUM"
    else:
        return "LOW"


def _build_report(
    competitor_name: str,
    job_data: list[dict],
    momentum_data: dict | None,
    news_data: list[dict],
) -> str:
    """Build a markdown section for one competitor."""
    threat = _threat_level(job_data, momentum_data, news_data)

    threat_emoji = {
        "CRITICAL": "🔴",
        "HIGH": "🟠",
        "MEDIUM": "🟡",
        "LOW": "🟢",
    }.get(threat, "⚪")

    lines: list[str] = []
    lines.append(f"## {competitor_name}  {threat_emoji} Threat: {threat}")
    lines.append("")

    # Job postings
    lines.append("### Hiring Signals (Job Postings)")
    real_jobs = [p for p in job_data if "error" not in p]
    if real_jobs:
        for p in real_jobs[:8]:
            lines.append(f"- **{p['title']}** at {p['company']} ({p['source']})")
            lines.append(f"  - Posted: {p['posted']}")
            if p.get("skills"):
                lines.append(f"  - Skills: {p['skills'][:100]}")
            lines.append(f"  - Signal: _{p['signal']}_")
    else:
        lines.append("- No job postings found for this period.")
    lines.append("")

    # Social momentum
    lines.append("### Social Media Momentum")
    if momentum_data and "error" not in momentum_data:
        score = momentum_data.get("momentum_score", 0)
        followers = momentum_data.get("followers", "N/A")
        posts = momentum_data.get("post_count", "N/A")
        recent = momentum_data.get("recent_posts_visible", 0)
        lines.append(f"- **Momentum Score:** {score}/100")
        lines.append(f"- **Followers:** {followers:,}" if isinstance(followers, int) else f"- **Followers:** {followers}")
        lines.append(f"- **Total Posts:** {posts}")
        lines.append(f"- **Recent Posts Visible:** {recent}")
        if momentum_data.get("bio"):
            lines.append(f"- **Bio:** {momentum_data['bio'][:150]}")
    else:
        lines.append("- Instagram data unavailable or handle not provided.")
    lines.append("")

    # News
    lines.append("### News Mentions")
    real_news = [n for n in news_data if "error" not in n and n.get("title")]
    if real_news:
        for n in real_news:
            lines.append(f"- [{n['title']}]({n['url']})")
            if n.get("snippet"):
                lines.append(f"  _{n['snippet'][:150]}_")
    else:
        lines.append("- No news mentions found this week.")
    lines.append("")

    return "\n".join(lines)


def _generate_key_actions(all_digests: list[dict]) -> str:
    """Generate 2-3 recommended actions based on aggregate threat signals."""
    high_threats = [d for d in all_digests if d["threat"] in ("HIGH", "CRITICAL")]
    medium_threats = [d for d in all_digests if d["threat"] == "MEDIUM"]

    actions: list[str] = []

    if high_threats:
        names = ", ".join(d["name"] for d in high_threats)
        actions.append(
            f"**URGENT — Counter {names}:** Run a limited-time offer or loyalty bonus "
            f"this week to lock in regulars before their new push lands."
        )
    if medium_threats or high_threats:
        actions.append(
            "**Content Push:** Schedule 3-5 Instagram posts this week showcasing your "
            "signature items, ambiance, and customer stories to maintain mindshare advantage."
        )

    expansion_signals = any(
        "new outlet" in (d.get("top_signal", "")).lower() or "expansion" in (d.get("top_signal", "")).lower()
        for d in all_digests
    )
    if expansion_signals:
        actions.append(
            "**Loyalty Lock-in:** Email or WhatsApp your top 20% customers with an "
            "exclusive perk — expansion by a competitor is imminent, lock in loyalty now."
        )
    else:
        actions.append(
            "**Monitor & Prepare:** No immediate threat. Schedule next digest for next "
            "week. Prep a 'new arrival' post to stay top-of-mind."
        )

    return "\n".join(f"{i+1}. {a}" for i, a in enumerate(actions[:3]))


async def weekly_alert_digest(
    competitor_names: list[str],
    location: str = "tirupur",
    instagram_handles: list[str] | None = None,
) -> dict:
    """
    Combine job postings + social momentum + news into a weekly digest.
    Returns a clean markdown report with threat levels and key actions.
    """
    if instagram_handles is None:
        instagram_handles = []

    handle_map: dict[str, str] = {}
    for i, name in enumerate(competitor_names):
        if i < len(instagram_handles):
            handle_map[name] = instagram_handles[i]

    # Determine keywords from competitor names
    food_keywords = ["restaurant manager", "head chef", "kitchen staff", "barista", "outlet manager"]

    # Gather all data concurrently where possible
    async def gather_for_competitor(name: str) -> dict:
        result = {"name": name, "jobs": [], "momentum": None, "news": []}

        # Job postings — use competitor name + generic food keywords
        search_terms = [name] + food_keywords[:3]
        try:
            job_data = await scan_job_postings(search_terms, location)
            # Filter postings relevant to this competitor
            comp_jobs = [
                p for p in job_data.get("postings", [])
                if name.lower() in p.get("company", "").lower()
                or name.lower() in p.get("title", "").lower()
            ]
            # If no direct match, keep all (generic food jobs in area still signal market activity)
            result["jobs"] = comp_jobs if comp_jobs else job_data.get("postings", [])[:5]
        except Exception as exc:
            result["jobs"] = [{"error": str(exc)}]

        return result

    # News + momentum can run in parallel with job scanning
    async def gather_news_and_momentum(name: str, handle: str | None) -> tuple[list, dict | None]:
        news: list[dict] = []
        momentum: dict | None = None

        headers = {"User-Agent": random_ua(), "Accept-Language": "en-IN,en;q=0.9"}
        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            news = await _fetch_google_news(name, client)

        if handle:
            try:
                mom_result = await social_momentum([handle])
                profiles = mom_result.get("profiles", [])
                if profiles:
                    momentum = profiles[0]
            except Exception as exc:
                momentum = {"error": str(exc), "handle": handle, "momentum_score": 0}

        return news, momentum

    # Run sequentially to avoid overwhelming anti-bot systems
    report_sections: list[str] = []
    all_digests: list[dict] = []

    report_sections.append("# Weekly Competitor Intelligence Digest")
    report_sections.append(f"**Generated:** {datetime.now(timezone.utc).strftime('%A, %d %B %Y %H:%M UTC')}")
    report_sections.append(f"**Location:** {location.title()}")
    report_sections.append(f"**Competitors Tracked:** {', '.join(competitor_names)}")
    report_sections.append("")
    report_sections.append("---")
    report_sections.append("")

    for name in competitor_names:
        handle = handle_map.get(name)

        jobs: list[dict] = []
        news: list[dict] = []
        momentum: dict | None = None

        # Job postings
        try:
            search_terms = [name] + food_keywords[:3]
            job_data = await scan_job_postings(search_terms, location)
            comp_jobs = [
                p for p in job_data.get("postings", [])
                if name.lower() in p.get("company", "").lower()
            ]
            jobs = comp_jobs if comp_jobs else job_data.get("postings", [])[:5]
        except Exception as exc:
            jobs = [{"error": f"Job scan failed: {exc}"}]

        # News + momentum in parallel
        try:
            news, momentum = await gather_news_and_momentum(name, handle)
        except Exception as exc:
            news = [{"error": str(exc)}]
            momentum = None

        threat = _threat_level(jobs, momentum, news)
        top_signal = ""
        for j in jobs:
            if j.get("threat_level") in ("high", "medium"):
                top_signal = j.get("signal", "")
                break

        all_digests.append(
            {
                "name": name,
                "threat": threat,
                "top_signal": top_signal,
                "job_count": sum(1 for j in jobs if "error" not in j),
                "news_count": sum(1 for n in news if "error" not in n and n.get("title")),
                "momentum_score": momentum.get("momentum_score", 0) if momentum else 0,
            }
        )

        section = _build_report(name, jobs, momentum, news)
        report_sections.append(section)
        report_sections.append("---")
        report_sections.append("")

        await polite_delay()

    # Executive summary
    report_sections.insert(6, "## Executive Summary")
    summary_lines = []
    for d in sorted(all_digests, key=lambda x: {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}.get(x["threat"], 0), reverse=True):
        summary_lines.append(
            f"- **{d['name']}**: {d['threat']} — "
            f"{d['job_count']} job signals, {d['news_count']} news mentions, "
            f"momentum {d['momentum_score']}/100"
        )
    report_sections.insert(7, "\n".join(summary_lines))
    report_sections.insert(8, "")
    report_sections.insert(9, "---")
    report_sections.insert(10, "")

    # Key actions
    key_actions = _generate_key_actions(all_digests)
    report_sections.append("## Key Actions for Cafe Mellow")
    report_sections.append("")
    report_sections.append(key_actions)
    report_sections.append("")

    markdown_report = "\n".join(report_sections)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "location": location,
        "competitors": all_digests,
        "report_markdown": markdown_report,
    }


# ---------------------------------------------------------------------------
# MCP Server wiring
# ---------------------------------------------------------------------------

server = Server("signal-monitor")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="scan_job_postings",
            description=(
                "Scrape Naukri.com and Indeed India for job postings matching keywords "
                "in a given location. Interprets hiring signals to reveal competitor "
                "expansion plans 4-6 weeks before public announcement."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": 'Keywords to search (e.g. ["head chef", "restaurant manager"])',
                    },
                    "location": {
                        "type": "string",
                        "default": "tirupur",
                        "description": "City/location to search in",
                    },
                },
                "required": ["keywords"],
            },
        ),
        Tool(
            name="social_momentum",
            description=(
                "Scrape public Instagram profiles (no login required) to measure "
                "competitor social media activity. Falls back to Picuki.com if Instagram "
                "blocks. Returns follower count, post frequency, and momentum score 0-100."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "instagram_handles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": 'Instagram handles without @ (e.g. ["cafemellow", "rival_cafe"])',
                    },
                },
                "required": ["instagram_handles"],
            },
        ),
        Tool(
            name="weekly_alert_digest",
            description=(
                "Generate a combined weekly competitor intelligence digest. "
                "Combines job postings (expansion signals), Instagram momentum, "
                "and Google News mentions. Returns a markdown report with threat "
                "levels and recommended actions for Cafe Mellow."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "competitor_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": 'Competitor business names (e.g. ["Brew House", "The Chai Club"])',
                    },
                    "location": {
                        "type": "string",
                        "default": "tirupur",
                        "description": "City/location context",
                    },
                    "instagram_handles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                        "description": "Instagram handles matching competitor_names order (optional)",
                    },
                },
                "required": ["competitor_names"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "scan_job_postings":
            result = await scan_job_postings(
                keywords=arguments["keywords"],
                location=arguments.get("location", "tirupur"),
            )
        elif name == "social_momentum":
            result = await social_momentum(
                instagram_handles=arguments["instagram_handles"],
            )
        elif name == "weekly_alert_digest":
            result = await weekly_alert_digest(
                competitor_names=arguments["competitor_names"],
                location=arguments.get("location", "tirupur"),
                instagram_handles=arguments.get("instagram_handles", []),
            )
        else:
            result = {"error": f"Unknown tool: {name}"}
    except Exception as exc:
        result = {"error": str(exc), "tool": name}

    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


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
