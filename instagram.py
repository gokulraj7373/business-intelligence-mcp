"""
instagram-intel MCP Server
Provides Instagram profile intelligence, post analysis, competitor comparison,
and hashtag research using Playwright (primary) and Imginn/httpx (fallback).
"""

import asyncio
import random
import re
from collections import Counter
from typing import Any

import httpx
from bs4 import BeautifulSoup
from mcp.server.stdio import stdio_server
from mcp.server import Server
from mcp.types import Tool, TextContent

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.picuki.com/",
    "Origin": "https://www.picuki.com",
}

app = Server("instagram-intel")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _random_delay() -> float:
    return random.uniform(1.0, 2.0)


def _parse_count(text: str) -> int:
    """Convert '1.2M', '45.6K', '1,234' style strings to int."""
    if not text:
        return 0
    text = text.strip().replace(",", "")
    try:
        if text.endswith("M") or text.endswith("m"):
            return int(float(text[:-1]) * 1_000_000)
        if text.endswith("K") or text.endswith("k"):
            return int(float(text[:-1]) * 1_000)
        return int(float(text))
    except (ValueError, IndexError):
        return 0


def _extract_hashtags(text: str) -> list[str]:
    """Extract all hashtags from a text string."""
    return re.findall(r"#\w+", text.lower())


def _is_blocked(content: str) -> bool:
    """Detect Instagram login wall or CAPTCHA."""
    blocked_signals = [
        "log in to instagram",
        "login_required",
        "checkpoint_required",
        "captcha",
        "please wait a few minutes",
        "we restrict certain activity",
        "sign up to see",
        "create an account",
    ]
    lower = content.lower()
    return any(sig in lower for sig in blocked_signals)


# ---------------------------------------------------------------------------
# Playwright scraper
# ---------------------------------------------------------------------------

async def _playwright_get_profile(handle: str) -> dict[str, Any] | None:
    """Try to scrape Instagram profile via Playwright. Returns None if blocked."""
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout

        url = f"https://www.instagram.com/{handle}/"
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT)
            page = await context.new_page()

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                await page.wait_for_timeout(2000)
                content = await page.content()

                if _is_blocked(content):
                    await browser.close()
                    return None

                result = {}

                # Full name from og:title
                title_el = await page.query_selector('meta[property="og:title"]')
                if title_el:
                    og_title = await title_el.get_attribute("content") or ""
                    name_match = re.match(r"^(.+?)\s*\(", og_title)
                    result["full_name"] = name_match.group(1).strip() if name_match else og_title

                # Bio from og:description
                desc_el = await page.query_selector('meta[property="og:description"]')
                if desc_el:
                    og_desc = await desc_el.get_attribute("content") or ""
                    result["bio"] = og_desc

                # Stats from header section list items
                header_spans = await page.eval_on_selector_all(
                    'header section ul li',
                    "els => els.map(e => e.innerText)"
                )

                followers = 0
                following = 0
                post_count = 0

                for item in header_spans:
                    lines = [l.strip() for l in item.split("\n") if l.strip()]
                    if len(lines) >= 2:
                        val_str = lines[0]
                        label = lines[-1].lower()
                        if "follower" in label:
                            followers = _parse_count(val_str)
                        elif "following" in label:
                            following = _parse_count(val_str)
                        elif "post" in label:
                            post_count = _parse_count(val_str)

                # Verified badge
                verified_el = await page.query_selector('[aria-label="Verified"]')
                result["is_verified"] = verified_el is not None

                result["followers"] = followers
                result["following"] = following
                result["post_count"] = post_count
                result.setdefault("full_name", handle)
                result.setdefault("bio", "")

            except PWTimeout:
                await browser.close()
                return None
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

        return result if result.get("followers") or result.get("full_name") != handle else None

    except Exception:
        return None


# ---------------------------------------------------------------------------
# Picuki scrapers (Playwright — bypasses Cloudflare protection)
# ---------------------------------------------------------------------------

async def _playwright_picuki_profile(handle: str) -> dict[str, Any] | None:
    """Scrape Instagram profile stats via Picuki using Playwright (Cloudflare-safe)."""
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
        url = f"https://www.picuki.com/profile/{handle}"
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT)
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                await page.wait_for_timeout(2000)
                content = await page.content()
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(content, "html.parser")
                result: dict[str, Any] = {"full_name": "", "bio": "", "followers": 0, "following": 0, "post_count": 0, "is_verified": False}
                name_el = soup.select_one(".profile-info-name h1, .profile-name, h1")
                if name_el:
                    result["full_name"] = name_el.get_text(strip=True)
                bio_el = soup.select_one(".profile-info-bio, .bio-text, .bio")
                if bio_el:
                    result["bio"] = bio_el.get_text(strip=True)
                page_text = soup.get_text(" ")
                for pattern, key in [
                    (r"([\d.,]+[KkMm]?)\s*[Ff]ollowers", "followers"),
                    (r"([\d.,]+[KkMm]?)\s*[Ff]ollowing", "following"),
                    (r"([\d.,]+[KkMm]?)\s*[Pp]osts?", "post_count"),
                ]:
                    m = re.search(pattern, page_text)
                    if m:
                        result[key] = _parse_count(m.group(1))
                return result if (result["followers"] or result["full_name"]) else None
            except PWTimeout:
                return None
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
    except Exception:
        return None


async def _playwright_picuki_hashtag(tag: str) -> dict[str, Any]:
    """Scrape hashtag page on Picuki via Playwright."""
    try:
        from playwright.async_api import async_playwright
        url = f"https://www.picuki.com/tag/{tag}"
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT)
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_timeout(2000)
            content = await page.content()
            await browser.close()
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(content, "html.parser")
            result: dict[str, Any] = {"hashtag": f"#{tag}", "post_count": 0, "recent_captions": [], "co_occurring_hashtags": []}
            page_text = soup.get_text(" ")
            count_m = re.search(r"([\d.,]+[KkMm]?)\s*(?:posts?|publications?)", page_text, re.IGNORECASE)
            if count_m:
                result["post_count"] = _parse_count(count_m.group(1))
            post_items = soup.select(".box-photo, .media-item, .post, .item, article")
            all_hashtags: list[str] = []
            for item in post_items[:20]:
                caption_el = item.select_one(".editable-text, .photo-description, .caption, .desc, .text")
                if caption_el:
                    caption_text = caption_el.get_text(strip=True)
                    if caption_text:
                        result["recent_captions"].append(caption_text[:200])
                        all_hashtags.extend(_extract_hashtags(caption_text))
            tag_counter = Counter(h for h in all_hashtags if h != f"#{tag.lower()}")
            result["co_occurring_hashtags"] = [t for t, _ in tag_counter.most_common(15)]
            return result
    except Exception as exc:
        return {"hashtag": f"#{tag}", "post_count": 0, "recent_captions": [], "co_occurring_hashtags": [], "_error": str(exc)}


# ---------------------------------------------------------------------------
# Picuki scrapers (httpx + BeautifulSoup) — kept as reference, Playwright preferred
# ---------------------------------------------------------------------------

async def _imginn_get_profile(handle: str) -> dict[str, Any]:
    """Scrape profile data from Picuki (replaces blocked Imginn)."""
    url = f"https://www.picuki.com/profile/{handle}"
    await asyncio.sleep(_random_delay())

    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    result: dict[str, Any] = {
        "full_name": "",
        "bio": "",
        "followers": 0,
        "following": 0,
        "post_count": 0,
        "is_verified": False,
    }

    # Full name — picuki uses .profile-info-name h1 or page h1
    name_el = soup.select_one(".profile-info-name h1, .profile-name, h1")
    if name_el:
        result["full_name"] = name_el.get_text(strip=True)
    if not result["full_name"]:
        title_el = soup.find("title")
        if title_el:
            result["full_name"] = title_el.get_text(strip=True).split("@")[0].split("|")[0].strip()

    # Bio — picuki uses .profile-info-bio or .bio-text
    bio_el = soup.select_one(".profile-info-bio, .bio-text, .bio, .description")
    if bio_el:
        result["bio"] = bio_el.get_text(strip=True)

    # Stats — picuki uses .profile-info-sum li or .counters-data li
    stat_items = soup.select(".profile-info-sum li, .counters-data li, .profile-counts li, ul.counts li")
    for item in stat_items:
        text = item.get_text(" ", strip=True).lower()
        num_match = re.search(r"[\d.,kmKM]+", item.get_text())
        if not num_match:
            continue
        count = _parse_count(num_match.group())
        if "follower" in text:
            result["followers"] = count
        elif "following" in text:
            result["following"] = count
        elif "post" in text or "media" in text:
            result["post_count"] = count

    # Fallback: search page text for counts near keywords
    if result["followers"] == 0:
        page_text = soup.get_text(" ")
        for pattern, key in [
            (r"([\d.,]+[KkMm]?)\s*[Ff]ollowers", "followers"),
            (r"([\d.,]+[KkMm]?)\s*[Ff]ollowing", "following"),
            (r"([\d.,]+[KkMm]?)\s*[Pp]osts?", "post_count"),
        ]:
            m = re.search(pattern, page_text)
            if m:
                result[key] = _parse_count(m.group(1))

    # Verified badge
    verified_el = soup.select_one('[class*="verified"], [title*="verified"], [alt*="verified"]')
    result["is_verified"] = verified_el is not None

    return result


async def _imginn_get_posts(handle: str, limit: int = 12) -> list[dict[str, Any]]:
    """Scrape recent posts from Picuki profile page (replaces blocked Imginn)."""
    url = f"https://www.picuki.com/profile/{handle}"
    await asyncio.sleep(_random_delay())

    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    posts = []

    # Picuki post containers
    post_items = soup.select(".box-photo, .media-item, .post, .item, article")
    if not post_items:
        post_items = soup.select("[class*='photo'], [class*='post'], [class*='item']")

    for item in post_items[:limit]:
        post: dict[str, Any] = {
            "caption": "",
            "likes": 0,
            "comments": 0,
            "date": "",
            "hashtags": [],
        }

        # Caption — picuki uses .editable-text or .photo-description
        caption_el = item.select_one(".editable-text, .photo-description, .caption, .desc, .text, [class*='caption'], [class*='description']")
        if caption_el:
            full_caption = caption_el.get_text(strip=True)
            post["caption"] = full_caption[:200]
            post["hashtags"] = _extract_hashtags(full_caption)

        # Likes — imginn shows like counts in spans near heart/like icons
        likes_el = item.select_one("[class*='like'], [class*='heart'], [class*='count']")
        if likes_el:
            likes_text = likes_el.get_text(strip=True)
            post["likes"] = _parse_count(re.sub(r"[^\d.,KkMm]", "", likes_text))

        # Comments
        comments_el = item.select_one("[class*='comment']")
        if comments_el:
            comments_text = comments_el.get_text(strip=True)
            post["comments"] = _parse_count(re.sub(r"[^\d.,KkMm]", "", comments_text))

        # Date
        date_el = item.select_one("time, [class*='date'], [class*='time']")
        if date_el:
            post["date"] = date_el.get("datetime") or date_el.get_text(strip=True)

        # Fallback text scan for likes/comments
        if post["likes"] == 0 and post["comments"] == 0:
            item_text = item.get_text(" ")
            likes_m = re.search(r"([\d.,]+[KkMm]?)\s*[Ll]ikes?", item_text)
            if likes_m:
                post["likes"] = _parse_count(likes_m.group(1))
            comments_m = re.search(r"([\d.,]+[KkMm]?)\s*[Cc]omments?", item_text)
            if comments_m:
                post["comments"] = _parse_count(comments_m.group(1))

        posts.append(post)

    return posts


async def _imginn_hashtag(hashtag: str) -> dict[str, Any]:
    """Scrape hashtag data from Picuki (replaces blocked Imginn)."""
    tag = hashtag.lstrip("#")
    url = f"https://www.picuki.com/tag/{tag}"
    await asyncio.sleep(_random_delay())

    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    result: dict[str, Any] = {
        "hashtag": f"#{tag}",
        "post_count": 0,
        "recent_captions": [],
        "co_occurring_hashtags": [],
    }

    # Post count
    page_text = soup.get_text(" ")
    count_m = re.search(r"([\d.,]+[KkMm]?)\s*(?:posts?|publications?)", page_text, re.IGNORECASE)
    if count_m:
        result["post_count"] = _parse_count(count_m.group(1))

    # Recent posts — imginn uses .post or .item containers
    post_items = soup.select(".post, .item, article, .feed-item")
    all_hashtags: list[str] = []

    for item in post_items[:20]:
        caption_el = item.select_one(".caption, .desc, .text, [class*='caption'], [class*='description']")
        if caption_el:
            caption_text = caption_el.get_text(strip=True)
            if caption_text:
                result["recent_captions"].append(caption_text[:200])
                all_hashtags.extend(_extract_hashtags(caption_text))

    # Top co-occurring hashtags (excluding the searched tag)
    tag_counter = Counter(h for h in all_hashtags if h != f"#{tag.lower()}")
    result["co_occurring_hashtags"] = [t for t, _ in tag_counter.most_common(15)]

    return result


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def get_profile(handle: str) -> str:
    """Get Instagram profile info. Instagram Playwright → Picuki Playwright → Picuki httpx."""
    handle = handle.lstrip("@").strip()

    # Try Instagram directly via Playwright
    profile = await _playwright_get_profile(handle)
    source = "Instagram (Playwright)"

    # Fallback: Picuki via Playwright (bypasses Cloudflare)
    if not profile:
        profile = await _playwright_picuki_profile(handle)
        source = "Picuki (Playwright fallback)"

    # Last resort: Picuki via httpx
    if not profile:
        try:
            profile = await _imginn_get_profile(handle)
            source = "Picuki (httpx fallback)"
        except Exception as e:
            return f"Error fetching profile for @{handle}: {e}"

    lines = [
        f"## Instagram Profile: @{handle}",
        f"**Source:** {source}",
        "",
        f"**Full Name:** {profile.get('full_name', 'N/A')}",
        f"**Verified:** {'Yes' if profile.get('is_verified') else 'No'}",
        f"**Bio:** {profile.get('bio', 'N/A')}",
        "",
        f"**Followers:** {profile.get('followers', 0):,}",
        f"**Following:** {profile.get('following', 0):,}",
        f"**Posts:** {profile.get('post_count', 0):,}",
    ]
    return "\n".join(lines)


async def get_recent_posts(handle: str, limit: int = 12) -> str:
    """Get recent posts with engagement metrics from Imginn."""
    handle = handle.lstrip("@").strip()

    try:
        profile = await _imginn_get_profile(handle)
    except Exception:
        profile = {"followers": 0}

    try:
        posts = await _imginn_get_posts(handle, limit)
    except Exception as e:
        return f"Error fetching posts for @{handle}: {e}"

    if not posts:
        return f"No posts found for @{handle}. Imginn may not have indexed this profile."

    followers = profile.get("followers", 1) or 1

    total_likes = sum(p["likes"] for p in posts)
    total_comments = sum(p["comments"] for p in posts)
    count = len(posts)

    avg_likes = total_likes / count if count else 0
    avg_comments = total_comments / count if count else 0
    engagement_rate = (avg_likes + avg_comments) / followers * 100

    all_hashtags: list[str] = []
    for p in posts:
        all_hashtags.extend(p["hashtags"])
    top_hashtags = [tag for tag, _ in Counter(all_hashtags).most_common(10)]

    lines = [
        f"## Recent Posts: @{handle}",
        f"**Analysed:** {count} posts | **Followers:** {followers:,}",
        f"**Avg Likes:** {avg_likes:.0f} | **Avg Comments:** {avg_comments:.0f}",
        f"**Estimated Engagement Rate:** {engagement_rate:.2f}%",
        "",
        f"**Top 10 Hashtags:** {', '.join(top_hashtags) if top_hashtags else 'None detected'}",
        "",
        "---",
        "### Posts",
    ]

    for i, post in enumerate(posts, 1):
        lines.append(f"\n**Post {i}**")
        lines.append(f"- **Date:** {post['date'] or 'Unknown'}")
        lines.append(f"- **Likes:** {post['likes']:,} | **Comments:** {post['comments']:,}")
        if post["caption"]:
            lines.append(f"- **Caption:** {post['caption']}")
        if post["hashtags"]:
            lines.append(f"- **Hashtags:** {', '.join(post['hashtags'][:8])}")

    return "\n".join(lines)


async def compare_profiles(handles: list[str]) -> str:
    """Compare multiple Instagram profiles and rank them."""
    if not handles:
        return "No handles provided."

    import math

    results = []
    for handle in handles:
        h = handle.lstrip("@").strip()
        try:
            profile = await _imginn_get_profile(h)
            posts = await _imginn_get_posts(h, limit=12)
        except Exception as e:
            results.append({
                "handle": h,
                "error": str(e),
                "followers": 0,
                "engagement_rate": 0.0,
                "post_count": 0,
                "threat_score": 0,
            })
            continue

        followers = profile.get("followers", 1) or 1
        post_count = profile.get("post_count", 0)

        total_likes = sum(p["likes"] for p in posts)
        total_comments = sum(p["comments"] for p in posts)
        count = len(posts) or 1
        avg_likes = total_likes / count
        avg_comments = total_comments / count
        engagement_rate = (avg_likes + avg_comments) / followers * 100

        # Threat score: 0-100
        # 40% follower score (log scale, 1M followers = 40pts)
        follower_score = min(40, (math.log10(max(followers, 1)) / 6) * 40)
        # 40% engagement score (5% ER = 40pts)
        engagement_score = min(40, (engagement_rate / 5) * 40)
        # 20% activity score (100+ posts = 20pts)
        activity_score = min(20, (post_count / 100) * 20)
        threat_score = int(follower_score + engagement_score + activity_score)

        results.append({
            "handle": h,
            "full_name": profile.get("full_name", h),
            "followers": followers,
            "following": profile.get("following", 0),
            "post_count": post_count,
            "engagement_rate": round(engagement_rate, 2),
            "threat_score": threat_score,
            "is_verified": profile.get("is_verified", False),
        })

    valid = [r for r in results if "error" not in r]
    errors = [r for r in results if "error" in r]

    by_followers = sorted(valid, key=lambda x: x["followers"], reverse=True)
    by_engagement = sorted(valid, key=lambda x: x["engagement_rate"], reverse=True)
    by_posts = sorted(valid, key=lambda x: x["post_count"], reverse=True)
    by_threat = sorted(valid, key=lambda x: x["threat_score"], reverse=True)

    lines = [
        "## Profile Comparison Report",
        "",
        "### Rankings",
    ]

    if by_followers:
        lines.append(f"**Most Followers:** @{by_followers[0]['handle']} ({by_followers[0]['followers']:,})")
    if by_engagement:
        lines.append(f"**Highest Engagement:** @{by_engagement[0]['handle']} ({by_engagement[0]['engagement_rate']}%)")
    if by_posts:
        lines.append(f"**Most Active (posts):** @{by_posts[0]['handle']} ({by_posts[0]['post_count']:,} posts)")
    if by_threat:
        lines.append(f"**Biggest Threat:** @{by_threat[0]['handle']} (score: {by_threat[0]['threat_score']}/100)")

    lines += ["", "---", "### Individual Profiles"]

    for r in by_threat:
        verified_mark = " (Verified)" if r.get("is_verified") else ""
        lines.append(f"\n#### @{r['handle']} — Threat Score: {r['threat_score']}/100")
        lines.append(f"- **Name:** {r.get('full_name', 'N/A')}{verified_mark}")
        lines.append(f"- **Followers:** {r['followers']:,}")
        lines.append(f"- **Following:** {r['following']:,}")
        lines.append(f"- **Posts:** {r['post_count']:,}")
        lines.append(f"- **Engagement Rate:** {r['engagement_rate']}%")

    if errors:
        lines.append("\n### Errors")
        for r in errors:
            lines.append(f"- @{r['handle']}: {r['error']}")

    return "\n".join(lines)


async def hashtag_research(hashtag: str) -> str:
    """Research a hashtag via Picuki Playwright (primary) then httpx fallback."""
    tag = hashtag.lstrip("#")
    data = await _playwright_picuki_hashtag(tag)
    if data.get("_error") or (not data["recent_captions"] and not data["post_count"]):
        try:
            data = await _imginn_hashtag(hashtag)
        except Exception as e:
            return f"Error researching hashtag {hashtag}: {e}"

    lines = [
        f"## Hashtag Research: {data['hashtag']}",
        "",
    ]

    if data["post_count"]:
        lines.append(f"**Total Posts:** {data['post_count']:,}")
    else:
        lines.append("**Total Posts:** N/A")

    lines.append("")

    if data["co_occurring_hashtags"]:
        lines.append(f"**Top Co-occurring Hashtags:** {', '.join(data['co_occurring_hashtags'])}")
    else:
        lines.append("**Top Co-occurring Hashtags:** None detected")

    lines += ["", "### Recent Post Captions"]

    if data["recent_captions"]:
        for i, caption in enumerate(data["recent_captions"][:10], 1):
            lines.append(f"\n**{i}.** {caption}")
    else:
        lines.append("No captions extracted.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MCP Tool definitions & dispatch
# ---------------------------------------------------------------------------

TOOLS = [
    Tool(
        name="get_profile",
        description=(
            "Fetch an Instagram profile's key stats: followers, following, post count, "
            "bio, full name, and verified status. Uses Playwright on Instagram first; "
            "falls back to Imginn via httpx if blocked."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "description": "Instagram username (with or without @)",
                }
            },
            "required": ["handle"],
        },
    ),
    Tool(
        name="get_recent_posts",
        description=(
            "Fetch recent posts for an Instagram account via Imginn. Returns captions, "
            "likes, comments, dates, hashtags, estimated engagement rate, and top hashtags."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "description": "Instagram username (with or without @)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of posts to retrieve (default 12)",
                    "default": 12,
                },
            },
            "required": ["handle"],
        },
    ),
    Tool(
        name="compare_profiles",
        description=(
            "Compare multiple Instagram profiles side-by-side. Returns rankings by "
            "followers, engagement rate, post activity, and a threat score (0-100) per profile."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "handles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of Instagram usernames to compare",
                }
            },
            "required": ["handles"],
        },
    ),
    Tool(
        name="hashtag_research",
        description=(
            "Research a hashtag on Imginn. Returns post count, recent captions posted "
            "under that hashtag, and top co-occurring hashtags."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "hashtag": {
                    "type": "string",
                    "description": "Hashtag to research (with or without #)",
                }
            },
            "required": ["hashtag"],
        },
    ),
]


@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    if name == "get_profile":
        result = await get_profile(arguments["handle"])
    elif name == "get_recent_posts":
        result = await get_recent_posts(
            arguments["handle"],
            int(arguments.get("limit", 12)),
        )
    elif name == "compare_profiles":
        result = await compare_profiles(arguments["handles"])
    elif name == "hashtag_research":
        result = await hashtag_research(arguments["hashtag"])
    else:
        result = f"Unknown tool: {name}"

    return [TextContent(type="text", text=result)]


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
