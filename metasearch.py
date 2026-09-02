#!/usr/bin/env python3
"""
MetaSearch MCP — Free multi-engine search aggregator
Aggregates: DuckDuckGo + Bing + Google News + Bing News (all free, no API keys)
Replaces SearXNG without needing Docker.
Returns token-efficient ranked, deduplicated results.
"""

import asyncio
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

app = Server("metasearch")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

DDG_HEADERS = {
    **HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://duckduckgo.com/",
}


# ── source scrapers ──────────────────────────────────────────────────────────

async def _ddg_json_fallback(query: str, limit: int = 10) -> list[dict]:
    """DuckDuckGo Instant Answer JSON API fallback — no API key needed."""
    url = f"https://api.duckduckgo.com/?q={quote_plus(query)}&format=json&no_html=1&skip_disambig=1"
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
            r = await client.get(url)
        data = r.json()
        results = []
        # AbstractText + AbstractURL for the main result
        if data.get("AbstractText") and data.get("AbstractURL"):
            results.append({
                "title":   data.get("Heading", query),
                "url":     data["AbstractURL"],
                "snippet": data["AbstractText"][:200],
                "source":  "DuckDuckGo",
            })
        # Results list
        for item in data.get("Results", [])[:limit]:
            if item.get("FirstURL") and item.get("Text"):
                results.append({
                    "title":   item["Text"][:120],
                    "url":     item["FirstURL"],
                    "snippet": "",
                    "source":  "DuckDuckGo",
                })
        # RelatedTopics
        for topic in data.get("RelatedTopics", [])[:limit]:
            if isinstance(topic, dict) and topic.get("FirstURL") and topic.get("Text"):
                results.append({
                    "title":   topic["Text"][:120],
                    "url":     topic["FirstURL"],
                    "snippet": "",
                    "source":  "DuckDuckGo",
                })
        return results[:limit]
    except Exception:
        return []


async def _ddg_search(query: str, limit: int = 10) -> list[dict]:
    """DuckDuckGo HTML search (no API key). Tries multiple CSS selectors,
    falls back to the JSON Instant Answer API if HTML yields nothing."""
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        async with httpx.AsyncClient(headers=DDG_HEADERS, timeout=15, follow_redirects=True) as client:
            r = await client.get(url)
        soup = BeautifulSoup(r.text, "lxml")
        results = []

        def _extract_real_url(href: str) -> str:
            from urllib.parse import unquote
            m = re.search(r'uddg=([^&]+)', href)
            raw = m.group(1) if m else href
            return unquote(raw)

        # Strategy 1 — classic result__body / result__title structure
        for el in soup.select(".result__body")[:limit]:
            title_el = el.select_one(".result__title a, a.result__a")
            snip_el  = el.select_one(".result__snippet")
            if not title_el:
                continue
            results.append({
                "title":   title_el.get_text(strip=True),
                "url":     _extract_real_url(title_el.get("href", "")),
                "snippet": snip_el.get_text(strip=True) if snip_el else "",
                "source":  "DuckDuckGo",
            })

        # Strategy 2 — h2.result__title a (some DDG variants)
        if not results:
            for title_el in soup.select("h2.result__title a")[:limit]:
                snip_el = title_el.find_parent("div")
                snip_el = snip_el.select_one(".result__snippet") if snip_el else None
                results.append({
                    "title":   title_el.get_text(strip=True),
                    "url":     _extract_real_url(title_el.get("href", "")),
                    "snippet": snip_el.get_text(strip=True) if snip_el else "",
                    "source":  "DuckDuckGo",
                })

        # Strategy 3 — data-testid attribute (newer DDG UI)
        if not results:
            for title_el in soup.select('[data-testid="result-title-a"]')[:limit]:
                parent = title_el.find_parent("li") or title_el.find_parent("div")
                snip_el = parent.select_one('[data-testid="result-snippet"]') if parent else None
                results.append({
                    "title":   title_el.get_text(strip=True),
                    "url":     _extract_real_url(title_el.get("href", "")),
                    "snippet": snip_el.get_text(strip=True) if snip_el else "",
                    "source":  "DuckDuckGo",
                })

        # Strategy 4 — any <a> inside a div whose class contains "result"
        if not results:
            for container in soup.find_all("div", class_=re.compile(r"result", re.I))[:limit]:
                title_el = container.find("a", href=True)
                if not title_el or not title_el.get_text(strip=True):
                    continue
                href = title_el.get("href", "")
                if href.startswith("/") or "duckduckgo.com" in href:
                    href = _extract_real_url(href)
                if not href.startswith("http"):
                    continue
                results.append({
                    "title":   title_el.get_text(strip=True)[:120],
                    "url":     href,
                    "snippet": "",
                    "source":  "DuckDuckGo",
                })

        # Fallback — JSON Instant Answer API
        if not results:
            results = await _ddg_json_fallback(query, limit)

        return results[:limit]
    except Exception:
        # Last-resort: try the JSON API
        return await _ddg_json_fallback(query, limit)


async def _bing_search(query: str, limit: int = 10) -> list[dict]:
    """Bing web search (scraped, no API key). Tries multiple CSS selectors."""
    url = f"https://www.bing.com/search?q={quote_plus(query)}&count={limit}"
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
            r = await client.get(url)
        soup = BeautifulSoup(r.text, "lxml")
        results = []

        def _try_bing_selectors(soup: BeautifulSoup) -> list[dict]:
            found = []
            # Strategy 1 — li.b_algo h2 a  (current Bing format)
            for el in soup.select("li.b_algo")[:limit]:
                title_el = el.select_one("h2 a")
                snip_el  = el.select_one(".b_caption p") or el.select_one("p")
                if title_el and title_el.get("href", "").startswith("http"):
                    found.append({
                        "title":   title_el.get_text(strip=True),
                        "url":     title_el.get("href", ""),
                        "snippet": snip_el.get_text(strip=True) if snip_el else "",
                        "source":  "Bing",
                    })
            if found:
                return found

            # Strategy 2 — [class="b_algo"] h2 a  (exact class match variant)
            for el in soup.find_all(class_="b_algo")[:limit]:
                title_el = el.find("h2")
                title_el = title_el.find("a") if title_el else None
                snip_el  = el.find(class_="b_caption")
                snip_el  = snip_el.find("p") if snip_el else el.find("p")
                if title_el and title_el.get("href", "").startswith("http"):
                    found.append({
                        "title":   title_el.get_text(strip=True),
                        "url":     title_el.get("href", ""),
                        "snippet": snip_el.get_text(strip=True) if snip_el else "",
                        "source":  "Bing",
                    })
            if found:
                return found

            # Strategy 3 — any element with "algo" in class, pick h2 a inside
            for el in soup.find_all(class_=re.compile(r"algo", re.I))[:limit]:
                h2 = el.find("h2")
                title_el = h2.find("a") if h2 else None
                if title_el and title_el.get("href", "").startswith("http"):
                    snip_el = el.find("p")
                    found.append({
                        "title":   title_el.get_text(strip=True),
                        "url":     title_el.get("href", ""),
                        "snippet": snip_el.get_text(strip=True) if snip_el else "",
                        "source":  "Bing",
                    })
            return found

        results = _try_bing_selectors(soup)
        return results[:limit]
    except Exception:
        return []


async def _google_news_rss(query: str, limit: int = 10) -> list[dict]:
    """Google News RSS — no API key, very reliable, returns structured data."""
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
            r = await client.get(url)
        root = ET.fromstring(r.text)
        results = []
        for item in root.findall(".//item")[:limit]:
            title = item.findtext("title", "").split(" - ")[0]  # strip source name
            link  = item.findtext("link", "")
            desc  = item.findtext("description", "")
            pub   = item.findtext("pubDate", "")
            # Clean HTML from description
            desc  = re.sub(r"<[^>]+>", "", desc)[:200]
            results.append({
                "title":     title,
                "url":       link,
                "snippet":   desc,
                "published": pub,
                "source":    "Google News",
            })
        return results
    except Exception:
        return []


async def _bing_news_rss(query: str, limit: int = 10) -> list[dict]:
    """Bing News RSS — no API key, good for local business news."""
    url = f"https://www.bing.com/news/search?q={quote_plus(query)}&format=rss"
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
            r = await client.get(url)
        root = ET.fromstring(r.text)
        results = []
        for item in root.findall(".//item")[:limit]:
            title = item.findtext("title", "")
            link  = item.findtext("link", "")
            desc  = item.findtext("description", "")
            pub   = item.findtext("pubDate", "")
            desc  = re.sub(r"<[^>]+>", "", desc)[:200]
            results.append({
                "title":     title,
                "url":       link,
                "snippet":   desc,
                "published": pub,
                "source":    "Bing News",
            })
        return results
    except Exception:
        return []


async def _reddit_search(query: str, limit: int = 5) -> list[dict]:
    """Reddit JSON search — useful for organic customer sentiment."""
    url = f"https://www.reddit.com/search.json?q={quote_plus(query)}&sort=relevance&limit={limit}"
    try:
        async with httpx.AsyncClient(
            headers={**HEADERS, "Accept": "application/json"}, timeout=15
        ) as client:
            r = await client.get(url)
        posts = r.json().get("data", {}).get("children", [])
        return [
            {
                "title":   p["data"].get("title", ""),
                "url":     f"https://reddit.com{p['data'].get('permalink','')}",
                "snippet": p["data"].get("selftext", "")[:200] or p["data"].get("url",""),
                "source":  f"Reddit r/{p['data'].get('subreddit','')}",
                "score":   p["data"].get("score", 0),
            }
            for p in posts
        ]
    except Exception:
        return []


def _deduplicate(results: list[dict]) -> list[dict]:
    """Remove duplicate URLs and near-duplicate titles."""
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    unique = []
    for r in results:
        url   = r.get("url", "").split("?")[0].rstrip("/")
        title = re.sub(r"\W+", " ", r.get("title", "").lower()).strip()[:60]
        if url in seen_urls or title in seen_titles:
            continue
        seen_urls.add(url)
        seen_titles.add(title)
        unique.append(r)
    return unique


def _format_results(results: list[dict], show_source: bool = True) -> str:
    lines = []
    for i, r in enumerate(results, 1):
        source = f" [{r.get('source','?')}]" if show_source else ""
        pub    = f" · {r.get('published','')[:16]}" if r.get("published") else ""
        lines.append(
            f"{i}. **{r.get('title','(no title)')}**{source}{pub}\n"
            f"   {r.get('url','')}\n"
            f"   {r.get('snippet','')}\n"
        )
    return "\n".join(lines)


# ── tools ────────────────────────────────────────────────────────────────────
@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="web_search",
            description=(
                "Search the web using multiple engines (DuckDuckGo + Bing) simultaneously. "
                "Returns merged, deduplicated results ranked by relevance. "
                "Better recall than single-engine search."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query":  {"type": "string", "description": "Search query"},
                    "limit":  {"type": "integer", "default": 10, "description": "Results per engine (5-20)"},
                    "engines": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["ddg", "bing"]},
                        "default": ["ddg", "bing"],
                        "description": "Which search engines to use"
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="news_search",
            description=(
                "Search news from Google News + Bing News simultaneously. "
                "Great for tracking competitor announcements, local food trends, "
                "market news, and Tirupur business news. No API key needed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "News search query"},
                    "limit": {"type": "integer", "default": 8, "description": "Results per source"},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="social_search",
            description=(
                "Search Reddit for organic customer opinions, complaints, and trends. "
                "Useful for: what customers say about a cafe type, food trends, "
                "pricing sentiment, service expectations."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Topic to search on Reddit"},
                    "limit": {"type": "integer", "default": 8},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="deep_research",
            description=(
                "Full research pipeline: search across all engines + news + social, "
                "then fetch and summarise the top N result pages. "
                "Use for in-depth competitor or market research. Token-aware — fetches only relevant content."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query":       {"type": "string", "description": "Research topic"},
                    "fetch_top_n": {"type": "integer", "default": 3, "description": "How many pages to read in full (1-5)"},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="local_market_pulse",
            description=(
                "Quick pulse check on a local market: searches news + web for a business type "
                "in a specific city. Returns trends, sentiment, and notable stories. "
                "Great for weekly cafe market monitoring."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "business_type": {"type": "string", "description": "e.g. 'cafe', 'coffee shop', 'bakery'"},
                    "city":          {"type": "string", "description": "e.g. 'Tirupur', 'Coimbatore'},"},
                },
                "required": ["business_type", "city"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        if name == "web_search":
            return await _handle_web_search(arguments)
        elif name == "news_search":
            return await _handle_news_search(arguments)
        elif name == "social_search":
            return await _handle_social_search(arguments)
        elif name == "deep_research":
            return await _handle_deep_research(arguments)
        elif name == "local_market_pulse":
            return await _handle_market_pulse(arguments)
        return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"Error: {e}")]


async def _handle_web_search(args: dict) -> list[types.TextContent]:
    query   = args["query"]
    limit   = min(args.get("limit", 10), 20)
    engines = args.get("engines", ["ddg", "bing"])

    tasks = []
    if "ddg"  in engines: tasks.append(_ddg_search(query, limit))
    if "bing" in engines: tasks.append(_bing_search(query, limit))

    all_results = []
    for batch in await asyncio.gather(*tasks):
        all_results.extend(batch)

    unique = _deduplicate(all_results)[:limit]
    out = f"## Web Search: {query}\n_{len(unique)} results from {', '.join(engines)}_\n\n"
    out += _format_results(unique)
    return [types.TextContent(type="text", text=out)]


async def _handle_news_search(args: dict) -> list[types.TextContent]:
    query = args["query"]
    limit = min(args.get("limit", 8), 20)

    gnews, bnews = await asyncio.gather(
        _google_news_rss(query, limit),
        _bing_news_rss(query, limit),
    )
    all_news = _deduplicate(gnews + bnews)[:limit * 2]
    out = f"## News: {query}\n_{len(all_news)} articles from Google News + Bing News_\n\n"
    out += _format_results(all_news)
    return [types.TextContent(type="text", text=out)]


async def _handle_social_search(args: dict) -> list[types.TextContent]:
    query = args["query"]
    limit = min(args.get("limit", 8), 25)
    posts = await _reddit_search(query, limit)
    if not posts:
        return [types.TextContent(type="text", text=f"No Reddit results found for: {query}")]
    out = f"## Reddit: {query}\n_{len(posts)} posts_\n\n"
    out += _format_results(posts)
    return [types.TextContent(type="text", text=out)]


async def _handle_deep_research(args: dict) -> list[types.TextContent]:
    query    = args["query"]
    fetch_n  = min(args.get("fetch_top_n", 3), 5)

    # Gather search results from all sources
    ddg, bing, gnews = await asyncio.gather(
        _ddg_search(query, 8),
        _bing_search(query, 8),
        _google_news_rss(query, 5),
    )
    all_results = _deduplicate(ddg + bing + gnews)
    out_lines = [
        f"# Deep Research: {query}",
        f"_{len(all_results)} total results found. Fetching top {fetch_n} pages..._\n",
        "## Search Results Overview",
        _format_results(all_results[:8]),
        "---",
        "## Page Content (Top Results)",
    ]

    # Fetch top N pages via crawl4ai
    try:
        from crawl4ai import AsyncWebCrawler, CrawlerRunConfig
        from crawl4ai.content_filter_strategy import BM25ContentFilter
        from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

        bm25   = BM25ContentFilter(user_query=query, bm25_threshold=1.0)
        md_gen = DefaultMarkdownGenerator(content_filter=bm25)
        config = CrawlerRunConfig(
            markdown_generator=md_gen,
            word_count_threshold=20,
            excluded_tags=["nav","footer","header","aside"],
            cache_mode="enabled",
        )
        async with AsyncWebCrawler() as crawler:
            top_urls = [r["url"] for r in all_results[:fetch_n] if r.get("url")]
            tasks    = [crawler.arun(url=u, config=config) for u in top_urls]
            pages    = await asyncio.gather(*tasks, return_exceptions=True)

        for url, page in zip(top_urls, pages):
            if isinstance(page, Exception):
                out_lines.append(f"### {url}\n_Error: {page}_\n")
                continue
            if not page.success:
                out_lines.append(f"### {url}\n_Failed to fetch_\n")
                continue
            content = page.markdown.fit_markdown or page.markdown.raw_markdown or ""
            title   = page.metadata.get("title", url)
            out_lines.append(f"### {title}\nSource: {url}\n\n{str(content)[:2000]}\n---")
    except Exception as e:
        out_lines.append(f"_Page fetch error: {e}_")

    return [types.TextContent(type="text", text="\n\n".join(out_lines))]


async def _handle_market_pulse(args: dict) -> list[types.TextContent]:
    biz  = args["business_type"]
    city = args["city"]
    q    = f"{biz} {city}"

    web_res, news_res, reddit_res = await asyncio.gather(
        _ddg_search(q, 6),
        _google_news_rss(q, 6),
        _reddit_search(f"{biz} Tamil Nadu {city} experience", 4),
    )

    all_web  = _deduplicate(web_res)[:6]
    all_news = _deduplicate(news_res)[:6]

    out = (
        f"# Market Pulse: {biz.title()} in {city}\n"
        f"_{datetime.now().strftime('%Y-%m-%d %H:%M')}_\n\n"
        f"## Web Results\n{_format_results(all_web, show_source=False)}\n"
        f"## News\n{_format_results(all_news, show_source=True)}\n"
    )
    if reddit_res:
        out += f"## Customer Sentiment (Reddit)\n{_format_results(reddit_res, show_source=True)}\n"

    return [types.TextContent(type="text", text=out)]


# ── entrypoint ───────────────────────────────────────────────────────────────
async def main():
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
