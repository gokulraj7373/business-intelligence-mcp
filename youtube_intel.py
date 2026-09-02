#!/usr/bin/env python3
"""
YouTube Intelligence MCP Server
Tools: search_videos, get_transcript, analyze_channel, research_topic
Uses: youtube-transcript-api, httpx, BeautifulSoup, mcp
"""

import asyncio
import json
import re
import sys
from typing import Any
from urllib.parse import quote_plus, urlparse, parse_qs

import httpx
from bs4 import BeautifulSoup
from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

app = Server("youtube-intel")

# ── shared browser-like headers ────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# ── helpers ────────────────────────────────────────────────────────────────────

def _extract_video_id(video_id_or_url: str) -> str:
    """
    Extract a YouTube video ID from a URL or return the raw string if it
    already looks like an ID (11 chars, no slash).
    Handles:
      - https://www.youtube.com/watch?v=VIDEO_ID
      - https://youtu.be/VIDEO_ID
      - https://youtube.com/shorts/VIDEO_ID
      - plain VIDEO_ID
    """
    s = video_id_or_url.strip()
    if "youtube.com" in s or "youtu.be" in s:
        parsed = urlparse(s)
        if parsed.netloc in ("youtu.be",):
            return parsed.path.lstrip("/").split("/")[0]
        qs = parse_qs(parsed.query)
        if "v" in qs:
            return qs["v"][0]
        # shorts / embed / live paths
        parts = parsed.path.lstrip("/").split("/")
        for i, part in enumerate(parts):
            if part in ("shorts", "embed", "live", "v") and i + 1 < len(parts):
                return parts[i + 1]
    # Assume plain video ID
    return s.split("?")[0].split("&")[0]


def _extract_yt_initial_data(html: str) -> dict | None:
    """
    YouTube embeds page data as:  var ytInitialData = {...};
    We grab that JSON blob. Tries multiple regex patterns to handle YouTube's
    changing page structure.
    """
    patterns = [
        r'var ytInitialData\s*=\s*(\{.*?\});\s*</script>',
        r'ytInitialData\s*=\s*(\{.*?\});',
        r'"ytInitialData"\s*,\s*(\{.*?\})\s*\)',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except Exception:
                continue

    # Last-resort: grab everything after the assignment and strip trailing junk
    match = re.search(r"var ytInitialData\s*=\s*(\{.+)", html, re.DOTALL)
    if match:
        raw = match.group(1)
        raw = re.split(r";\s*\n", raw, maxsplit=1)[0]
        try:
            return json.loads(raw)
        except Exception:
            pass
    return None


def _find_video_renderers(obj: Any, depth: int = 0) -> list[dict]:
    """
    Recursively walk a ytInitialData structure and collect every dict that
    looks like a videoRenderer (has both 'videoId' and 'title').
    """
    if depth > 15 or not isinstance(obj, (dict, list)):
        return []
    results: list[dict] = []
    if isinstance(obj, list):
        for item in obj:
            results.extend(_find_video_renderers(item, depth + 1))
    elif isinstance(obj, dict):
        if "videoId" in obj and "title" in obj:
            results.append(obj)
        else:
            for v in obj.values():
                results.extend(_find_video_renderers(v, depth + 1))
    return results


def _safe_get(obj: Any, *keys, default=None) -> Any:
    """Safely navigate nested dicts/lists."""
    cur = obj
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k, default)
        elif isinstance(cur, list) and isinstance(k, int):
            try:
                cur = cur[k]
            except IndexError:
                return default
        else:
            return default
        if cur is None:
            return default
    return cur


def _runs_text(runs_list: list) -> str:
    """Concatenate YouTube 'runs' text objects."""
    return "".join(r.get("text", "") for r in runs_list if isinstance(r, dict))


def _parse_search_results(data: dict, limit: int) -> list[dict]:
    """
    Navigate ytInitialData to extract video cards from search results.
    """
    videos = []
    try:
        contents = (
            data["contents"]
            ["twoColumnSearchResultsRenderer"]
            ["primaryContents"]
            ["sectionListRenderer"]
            ["contents"]
        )
    except (KeyError, TypeError):
        return videos

    for section in contents:
        items = _safe_get(section, "itemSectionRenderer", "contents", default=[])
        for item in items:
            renderer = item.get("videoRenderer") or item.get("compactVideoRenderer")
            if not renderer:
                continue
            video_id = renderer.get("videoId", "")
            if not video_id:
                continue

            # Title
            title_runs = _safe_get(renderer, "title", "runs", default=[])
            title = _runs_text(title_runs) if title_runs else _safe_get(renderer, "title", "simpleText", default="")

            # Channel
            channel = (
                _safe_get(renderer, "ownerText", "runs", 0, "text", default="")
                or _safe_get(renderer, "longBylineText", "runs", 0, "text", default="")
                or _safe_get(renderer, "shortBylineText", "runs", 0, "text", default="")
            )

            # View count
            views = (
                _safe_get(renderer, "viewCountText", "simpleText", default="")
                or _safe_get(renderer, "shortViewCountText", "simpleText", default="")
                or _runs_text(_safe_get(renderer, "viewCountText", "runs", default=[]))
            )

            # Upload date
            date = (
                _safe_get(renderer, "publishedTimeText", "simpleText", default="")
                or _safe_get(renderer, "videoInfo", "runs", 0, "text", default="")
            )

            videos.append({
                "title": title,
                "channel": channel,
                "video_id": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "views": views,
                "date": date,
            })

            if len(videos) >= limit:
                return videos

    return videos


# ── tool 1: search_videos ──────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search_videos",
            description=(
                "Search YouTube for videos matching a query. "
                "Returns titles, channels, video IDs, view counts, and upload dates."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Maximum number of results to return (default 10)",
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="get_transcript",
            description=(
                "Fetch and return the transcript for a YouTube video. "
                "Accepts a full URL or a plain video ID. "
                "Tries English first, then Hindi, then Tamil. "
                "Returns up to 3000 words of clean text."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "video_id_or_url": {
                        "type": "string",
                        "description": "YouTube video ID or full URL",
                    },
                    "language": {
                        "type": "string",
                        "default": "en",
                        "description": "Preferred language code (default 'en')",
                    },
                },
                "required": ["video_id_or_url"],
            },
        ),
        types.Tool(
            name="analyze_channel",
            description=(
                "Analyze a YouTube channel: subscriber count, video count, "
                "description, and recent video titles. "
                "Accepts a channel handle (@name), /c/name URL, or full channel URL."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "channel_name_or_url": {
                        "type": "string",
                        "description": "Channel handle like @MrBeast, or full channel URL",
                    },
                },
                "required": ["channel_name_or_url"],
            },
        ),
        types.Tool(
            name="research_topic",
            description=(
                "Full research pipeline: search YouTube for a topic, fetch transcripts "
                "from the top videos, and return per-video summaries plus common themes. "
                "Great for: 'what do food bloggers say about cafes in Coimbatore', "
                "'bubble tea trend India'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Research topic / search query"},
                    "max_videos": {
                        "type": "integer",
                        "default": 3,
                        "description": "Number of top videos to analyse (default 3)",
                    },
                },
                "required": ["query"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "search_videos":
        result = await _search_videos(
            query=arguments["query"],
            limit=int(arguments.get("limit", 10)),
        )
    elif name == "get_transcript":
        result = await _get_transcript(
            video_id_or_url=arguments["video_id_or_url"],
            language=arguments.get("language", "en"),
        )
    elif name == "analyze_channel":
        result = await _analyze_channel(
            channel_name_or_url=arguments["channel_name_or_url"],
        )
    elif name == "research_topic":
        result = await _research_topic(
            query=arguments["query"],
            max_videos=int(arguments.get("max_videos", 3)),
        )
    else:
        result = {"error": f"Unknown tool: {name}"}

    return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


# ── implementations ────────────────────────────────────────────────────────────

async def _search_videos(query: str, limit: int = 10) -> dict:
    search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    fallback_url = (
        f"https://www.youtube.com/results?search_query={quote_plus(query)}"
        "&sp=EgIQAQ%3D%3D"
    )
    fetch_headers = {**HEADERS, "Accept-Language": "en-US,en;q=0.9"}

    async with httpx.AsyncClient(headers=fetch_headers, follow_redirects=True, timeout=20) as client:
        try:
            resp = await client.get(search_url)
            resp.raise_for_status()
        except Exception as exc:
            return {"error": f"HTTP request failed: {exc}", "query": query}

    html = resp.text
    data = _extract_yt_initial_data(html)

    videos: list[dict] = []

    if data:
        # --- Primary path: fixed nav structure ---
        videos = _parse_search_results(data, limit)

        # --- Secondary path: recursive walker (handles structural changes) ---
        if not videos:
            renderers = _find_video_renderers(data)
            seen: set[str] = set()
            for obj in renderers:
                video_id = obj.get("videoId", "")
                if not video_id or video_id in seen:
                    continue
                seen.add(video_id)

                # Title
                title_obj = obj.get("title", {})
                if isinstance(title_obj, dict):
                    title_runs = title_obj.get("runs", [])
                    title = (
                        _runs_text(title_runs)
                        if title_runs
                        else title_obj.get("simpleText", "")
                    )
                else:
                    title = str(title_obj)

                # Channel
                channel = (
                    _safe_get(obj, "ownerText", "runs", 0, "text", default="")
                    or _safe_get(obj, "longBylineText", "runs", 0, "text", default="")
                    or _safe_get(obj, "shortBylineText", "runs", 0, "text", default="")
                )

                # Views
                views = (
                    _safe_get(obj, "viewCountText", "simpleText", default="")
                    or _safe_get(obj, "shortViewCountText", "simpleText", default="")
                    or _runs_text(_safe_get(obj, "viewCountText", "runs", default=[]))
                )

                # Date
                date = (
                    _safe_get(obj, "publishedTimeText", "simpleText", default="")
                    or _safe_get(obj, "videoInfo", "runs", 0, "text", default="")
                )

                videos.append({
                    "title": title,
                    "channel": channel,
                    "video_id": video_id,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "views": views,
                    "date": date,
                })
                if len(videos) >= limit:
                    break

    # --- Fallback: regex scrape for video IDs when JSON parsing fails entirely ---
    if not videos:
        try:
            async with httpx.AsyncClient(
                headers=fetch_headers, follow_redirects=True, timeout=20
            ) as client:
                fb_resp = await client.get(fallback_url)
                fb_resp.raise_for_status()
            fb_html = fb_resp.text
            raw_ids = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', fb_html)
            seen_ids: list[str] = []
            for vid_id in raw_ids:
                if vid_id not in seen_ids:
                    seen_ids.append(vid_id)
                if len(seen_ids) >= limit:
                    break
            for vid_id in seen_ids:
                videos.append({
                    "title": "",
                    "channel": "",
                    "video_id": vid_id,
                    "url": f"https://www.youtube.com/watch?v={vid_id}",
                    "views": "",
                    "date": "",
                })
        except Exception:
            pass

    if not videos:
        return {
            "error": (
                "Could not parse ytInitialData from YouTube search page and "
                "fallback regex found no video IDs. "
                "YouTube may have changed its page structure."
            ),
            "query": query,
        }

    return {
        "query": query,
        "count": len(videos),
        "videos": videos,
    }


async def _get_transcript(video_id_or_url: str, language: str = "en") -> dict:
    video_id = _extract_video_id(video_id_or_url)
    # Language priority: requested → en → hi → ta
    lang_priority = list(dict.fromkeys([language, "en", "hi", "ta"]))
    transcript_list = None
    lang_used = None
    error_msg = None

    _ytt = YouTubeTranscriptApi()
    try:
        fetched = _ytt.fetch(video_id, languages=lang_priority)
        transcript_list = [{"text": s.text} for s in fetched]
        lang_used = language
    except TranscriptsDisabled:
        error_msg = "Transcripts are disabled for this video."
    except NoTranscriptFound:
        # Try each language individually to get the one that works
        for lang in lang_priority:
            try:
                fetched = _ytt.fetch(video_id, languages=[lang])
                transcript_list = [{"text": s.text} for s in fetched]
                lang_used = lang
                break
            except (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable):
                continue
        if transcript_list is None:
            error_msg = f"No transcript found in any of: {lang_priority}."
    except VideoUnavailable:
        error_msg = "Video is unavailable."
    except Exception as exc:
        error_msg = f"Unexpected error fetching transcript: {exc}"

    if error_msg:
        return {
            "video_id": video_id,
            "language_used": None,
            "transcript": None,
            "error": error_msg,
        }

    # Clean: join all text segments, collapse whitespace
    raw_text = " ".join(seg.get("text", "") for seg in transcript_list)
    raw_text = re.sub(r"\s+", " ", raw_text).strip()

    # Truncate at 3000 words
    words = raw_text.split()
    truncated = False
    if len(words) > 3000:
        words = words[:3000]
        truncated = True
    text = " ".join(words)

    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "language_used": lang_used,
        "word_count": len(words),
        "truncated": truncated,
        "transcript": text,
        **({"note": "Transcript truncated to 3000 words to save tokens."} if truncated else {}),
    }


async def _analyze_channel(channel_name_or_url: str) -> dict:
    """
    Fetch a YouTube channel page and extract stats + recent video titles.
    Supports:
      - @handle  (e.g. @MrBeast or MrBeast without @)
      - /c/name  URL
      - /user/name URL
      - Full https://www.youtube.com/... URL
    """
    raw = channel_name_or_url.strip()

    # Build the URL to fetch
    if raw.startswith("http://") or raw.startswith("https://"):
        channel_url = raw
        # Normalize: ensure /videos suffix is NOT added yet
    elif raw.startswith("@"):
        channel_url = f"https://www.youtube.com/{raw}"
    elif "/" in raw:
        # Treat as path fragment
        channel_url = f"https://www.youtube.com/{raw.lstrip('/')}"
    else:
        # Plain name — try @handle form first
        channel_url = f"https://www.youtube.com/@{raw}"

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20) as client:
        try:
            resp = await client.get(channel_url)
            resp.raise_for_status()
        except Exception as exc:
            return {"error": f"HTTP request failed for {channel_url}: {exc}"}

    html = resp.text
    data = _extract_yt_initial_data(html)
    if not data:
        return {
            "error": "Could not parse ytInitialData from channel page.",
            "url": channel_url,
        }

    # --- Extract channel metadata ---
    # Header is typically at: header.c4TabbedHeaderRenderer
    header = (
        _safe_get(data, "header", "c4TabbedHeaderRenderer")
        or _safe_get(data, "header", "pageHeaderRenderer")
        or {}
    )

    channel_name = (
        _safe_get(header, "title")
        or _runs_text(_safe_get(header, "pageTitle", "runs", default=[]))
        or ""
    )

    # Subscriber count
    subscriber_text = (
        _safe_get(header, "subscriberCountText", "simpleText")
        or _runs_text(_safe_get(header, "subscriberCountText", "runs", default=[]))
        or ""
    )

    # Video count may appear in metadata rows
    video_count = ""
    metadata = _safe_get(data, "metadata", "channelMetadataRenderer", default={})
    description = _safe_get(metadata, "description", default="")

    # Try microformat for video count
    mf = _safe_get(data, "microformat", "microformatDataRenderer", default={})
    if not description:
        description = _safe_get(mf, "description", default="")

    # --- Extract recent video titles from the Videos tab content ---
    recent_videos = []

    def _walk_for_video_renderers(obj, depth=0):
        """Recursively walk ytInitialData looking for videoRenderer items."""
        if depth > 12 or len(recent_videos) >= 10:
            return
        if isinstance(obj, dict):
            renderer = obj.get("gridVideoRenderer") or obj.get("richItemRenderer")
            if renderer:
                inner = renderer.get("content", renderer)
                vid_id = inner.get("videoId") or renderer.get("videoId")
                title_obj = inner.get("title") or renderer.get("title") or {}
                title = (
                    _safe_get(title_obj, "simpleText")
                    or _runs_text(title_obj.get("runs", []))
                )
                if vid_id and title:
                    recent_videos.append({
                        "title": title,
                        "video_id": vid_id,
                        "url": f"https://www.youtube.com/watch?v={vid_id}",
                    })
                    return
            for v in obj.values():
                _walk_for_video_renderers(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                _walk_for_video_renderers(item, depth + 1)

    _walk_for_video_renderers(data)

    # Also try tabs → Videos tab
    if len(recent_videos) < 3:
        tabs = _safe_get(data, "contents", "twoColumnBrowseResultsRenderer", "tabs", default=[])
        for tab in tabs:
            tab_r = tab.get("tabRenderer", {})
            if tab_r.get("selected") or tab_r.get("title", "").lower() == "videos":
                tab_content = _safe_get(tab_r, "content", default={})
                _walk_for_video_renderers(tab_content)
                break

    return {
        "channel_name": channel_name,
        "channel_url": channel_url,
        "subscribers": subscriber_text,
        "description": description[:500] + ("..." if len(description) > 500 else ""),
        "recent_videos_count": len(recent_videos),
        "recent_videos": recent_videos[:10],
    }


async def _research_topic(query: str, max_videos: int = 3) -> dict:
    """
    Full pipeline: search → get transcripts → synthesize.
    Returns per-video excerpts (first 500 words) and common word themes.
    """
    # Step 1: Search
    search_result = await _search_videos(query, limit=max(max_videos + 5, 10))
    if "error" in search_result:
        return {"error": search_result["error"], "query": query}

    videos = search_result.get("videos", [])
    if not videos:
        return {"error": "No videos found for query.", "query": query}

    # Step 2: Fetch transcripts for top max_videos
    target_videos = videos[:max_videos]
    per_video = []
    all_words: list[str] = []

    for vid in target_videos:
        transcript_result = await _get_transcript(vid["video_id"], language="en")
        transcript_text = transcript_result.get("transcript") or ""
        transcript_error = transcript_result.get("error")

        # First 500 words as excerpt
        words = transcript_text.split() if transcript_text else []
        excerpt = " ".join(words[:500]) if words else ""
        all_words.extend(words)

        per_video.append({
            "title": vid["title"],
            "channel": vid["channel"],
            "video_id": vid["video_id"],
            "url": vid["url"],
            "views": vid["views"],
            "date": vid["date"],
            "transcript_available": bool(transcript_text),
            "transcript_error": transcript_error,
            "excerpt_500_words": excerpt,
        })

    # Step 3: Common themes — top meaningful words (rudimentary frequency)
    STOP_WORDS = {
        "the", "a", "an", "and", "or", "but", "is", "it", "in", "on", "at",
        "to", "for", "of", "with", "this", "that", "i", "we", "you", "he",
        "she", "they", "so", "as", "by", "be", "are", "was", "were", "have",
        "has", "had", "not", "from", "do", "does", "did", "will", "would",
        "can", "could", "should", "its", "our", "your", "their", "my", "me",
        "us", "him", "her", "if", "about", "like", "just", "also", "more",
        "very", "all", "into", "up", "out", "when", "there", "what", "which",
        "who", "how", "one", "some", "than", "then", "get", "know", "think",
        "go", "going", "really", "things", "thing", "good", "make", "way",
        "time", "see", "want", "right", "now", "even", "here", "been",
    }
    freq: dict[str, int] = {}
    for w in all_words:
        w_clean = re.sub(r"[^a-z]", "", w.lower())
        if len(w_clean) > 3 and w_clean not in STOP_WORDS:
            freq[w_clean] = freq.get(w_clean, 0) + 1

    top_themes = sorted(freq.items(), key=lambda x: x[1], reverse=True)[:20]
    theme_words = [word for word, _ in top_themes]

    return {
        "query": query,
        "videos_analysed": len(per_video),
        "common_themes": theme_words,
        "per_video_results": per_video,
    }


# ── entrypoint ────────────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
