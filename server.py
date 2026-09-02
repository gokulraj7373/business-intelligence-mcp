#!/usr/bin/env python3
"""
CafeMellow Research MCP Server
Token-efficient web scraper + URL indexer built on Crawl4AI.
Replaces: Firecrawl, mcp-fetch, search-scraper
"""

import asyncio
import json
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, BrowserConfig
from crawl4ai.content_filter_strategy import BM25ContentFilter, PruningContentFilter
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

app = Server("cafemellow-research")

# ── shared browser config ────────────────────────────────────────────────────
BROWSER = BrowserConfig(headless=True, java_script_enabled=True)


def _make_config(query: str | None, mode: str) -> CrawlerRunConfig:
    """Build a token-efficient crawler config."""
    if mode == "raw":
        return CrawlerRunConfig(word_count_threshold=10)

    # Pruning filter removes boilerplate (nav, footer, sidebar)
    prune = PruningContentFilter(threshold=0.45, threshold_type="fixed")

    if query and mode == "fit":
        # BM25 further narrows to query-relevant paragraphs → fewest tokens
        bm25 = BM25ContentFilter(user_query=query, bm25_threshold=1.0)
        md_gen = DefaultMarkdownGenerator(content_filter=bm25)
    else:
        md_gen = DefaultMarkdownGenerator(content_filter=prune)

    return CrawlerRunConfig(
        markdown_generator=md_gen,
        word_count_threshold=20,       # skip micro-blocks
        excluded_tags=["nav", "footer", "header", "aside", "script", "style"],
        exclude_external_links=True,
        remove_overlay_elements=True,
        cache_mode="enabled",          # repeat fetches cost 0 tokens
    )


# ── tool definitions ─────────────────────────────────────────────────────────
@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="fetch_page",
            description=(
                "Fetch a URL and return clean token-efficient markdown. "
                "Strips ads, navbars, footers, and boilerplate. "
                "Pass a query to return ONLY content relevant to that query (80% fewer tokens). "
                "Modes: fit=minimal (default), full=cleaned markdown, raw=everything."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "query": {
                        "type": "string",
                        "description": "Filter content to only what's relevant to this query (saves most tokens)",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["fit", "full", "raw"],
                        "default": "fit",
                        "description": "fit=fewest tokens, full=cleaned, raw=unfiltered",
                    },
                },
                "required": ["url"],
            },
        ),
        types.Tool(
            name="crawl_site",
            description=(
                "Crawl a website up to N pages deep, follow internal links, "
                "and return indexed content. Great for research, competitor analysis, "
                "or building a knowledge base from a site."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Starting URL"},
                    "max_pages": {
                        "type": "integer",
                        "default": 5,
                        "description": "Max pages to crawl (keep low to save tokens)",
                    },
                    "query": {
                        "type": "string",
                        "description": "Only return content relevant to this query",
                    },
                },
                "required": ["url"],
            },
        ),
        types.Tool(
            name="extract_links",
            description=(
                "Extract and categorise all internal + external links from a page. "
                "Use for URL indexing, site mapping, or finding specific sub-pages."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Page to extract links from"},
                    "filter_keyword": {
                        "type": "string",
                        "description": "Optional: only return links whose URL contains this keyword",
                    },
                },
                "required": ["url"],
            },
        ),
        types.Tool(
            name="batch_fetch",
            description=(
                "Fetch multiple URLs in parallel and return all results. "
                "Faster than calling fetch_page one by one. "
                "Always uses fit mode to minimise tokens."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of URLs to fetch (max 10)",
                    },
                    "query": {
                        "type": "string",
                        "description": "Filter all pages to content relevant to this query",
                    },
                },
                "required": ["urls"],
            },
        ),
        types.Tool(
            name="screenshot_page",
            description="Take a screenshot of a webpage. Returns base64 image.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to screenshot"},
                },
                "required": ["url"],
            },
        ),
    ]


# ── tool handlers ────────────────────────────────────────────────────────────
@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        if name == "fetch_page":
            return await _fetch_page(arguments)
        elif name == "crawl_site":
            return await _crawl_site(arguments)
        elif name == "extract_links":
            return await _extract_links(arguments)
        elif name == "batch_fetch":
            return await _batch_fetch(arguments)
        elif name == "screenshot_page":
            return await _screenshot_page(arguments)
        else:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"Error: {e}")]


async def _fetch_page(args: dict) -> list[types.TextContent]:
    url = args["url"]
    query = args.get("query")
    mode = args.get("mode", "fit")
    config = _make_config(query, mode)

    async with AsyncWebCrawler(config=BROWSER) as crawler:
        result = await crawler.arun(url=url, config=config)

    if not result.success:
        return [types.TextContent(type="text", text=f"Failed to fetch {url}: {result.error_message}")]

    # prefer fit_markdown (most token-efficient) then fallback
    content = (
        result.markdown.fit_markdown
        or result.markdown.raw_markdown
        or result.markdown
        or "No content extracted."
    )
    meta = f"Source: {url}\nTitle: {result.metadata.get('title', 'N/A')}\n\n"
    return [types.TextContent(type="text", text=meta + str(content))]


async def _crawl_site(args: dict) -> list[types.TextContent]:
    url = args["url"]
    max_pages = min(args.get("max_pages", 5), 20)
    query = args.get("query")
    config = _make_config(query, "fit")

    pages: list[str] = []
    visited: set[str] = set()
    queue = [url]

    async with AsyncWebCrawler(config=BROWSER) as crawler:
        while queue and len(pages) < max_pages:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            result = await crawler.arun(url=current, config=config)
            if not result.success:
                continue

            content = (
                result.markdown.fit_markdown
                or result.markdown.raw_markdown
                or result.markdown
                or ""
            )
            title = result.metadata.get("title", current)
            pages.append(f"## {title}\nURL: {current}\n\n{str(content)}\n\n---")

            # queue internal links for next depth
            for link in (result.links or {}).get("internal", []):
                href = link.get("href", "")
                if href and href not in visited:
                    queue.append(href)

    summary = f"Crawled {len(pages)} pages from {url}\n\n"
    return [types.TextContent(type="text", text=summary + "\n".join(pages))]


async def _extract_links(args: dict) -> list[types.TextContent]:
    url = args["url"]
    keyword = args.get("filter_keyword", "").lower()
    config = CrawlerRunConfig(word_count_threshold=5, cache_mode="enabled")

    async with AsyncWebCrawler(config=BROWSER) as crawler:
        result = await crawler.arun(url=url, config=config)

    if not result.success:
        return [types.TextContent(type="text", text=f"Failed: {result.error_message}")]

    all_links = result.links or {}
    internal = all_links.get("internal", [])
    external = all_links.get("external", [])

    def fmt(links, label):
        items = []
        for lnk in links:
            href = lnk.get("href", "")
            text = lnk.get("text", "").strip()
            if keyword and keyword not in href.lower():
                continue
            items.append(f"  - [{text or href}]({href})")
        return f"### {label} ({len(items)})\n" + ("\n".join(items) or "  none")

    out = f"Links from: {url}\n\n{fmt(internal, 'Internal')}\n\n{fmt(external, 'External')}"
    return [types.TextContent(type="text", text=out)]


async def _batch_fetch(args: dict) -> list[types.TextContent]:
    urls = args["urls"][:10]  # cap at 10
    query = args.get("query")
    config = _make_config(query, "fit")

    results: list[str] = []

    async with AsyncWebCrawler(config=BROWSER) as crawler:
        tasks = [crawler.arun(url=u, config=config) for u in urls]
        fetched = await asyncio.gather(*tasks, return_exceptions=True)

    for url, result in zip(urls, fetched):
        if isinstance(result, Exception):
            results.append(f"### {url}\nError: {result}\n")
            continue
        if not result.success:
            results.append(f"### {url}\nFailed: {result.error_message}\n")
            continue
        content = (
            result.markdown.fit_markdown
            or result.markdown.raw_markdown
            or result.markdown
            or "No content."
        )
        title = result.metadata.get("title", url)
        results.append(f"### {title}\nURL: {url}\n\n{str(content)}\n\n---")

    return [types.TextContent(type="text", text="\n".join(results))]


async def _screenshot_page(args: dict) -> list[types.TextContent]:
    url = args["url"]
    config = CrawlerRunConfig(screenshot=True, cache_mode="bypass")

    async with AsyncWebCrawler(config=BROWSER) as crawler:
        result = await crawler.arun(url=url, config=config)

    if not result.success:
        return [types.TextContent(type="text", text=f"Failed: {result.error_message}")]

    if result.screenshot:
        return [
            types.TextContent(
                type="text",
                text=f"Screenshot captured for {url} (base64 PNG, {len(result.screenshot)} chars)",
            )
        ]
    return [types.TextContent(type="text", text="No screenshot returned.")]


# ── entrypoint ───────────────────────────────────────────────────────────────
async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
