"""
Google Trends Intelligence MCP Server
Server name: trend-intelligence
Tools: trend_score, compare_keywords, seasonal_forecast, demand_gap_finder
"""

import time
import asyncio
from datetime import datetime
from typing import Optional

# urllib3 >= 2.0 renamed method_whitelist → allowed_methods; patch before pytrends loads
try:
    from urllib3.util.retry import Retry as _Retry
    _orig_retry_init = _Retry.__init__
    def _patched_retry_init(self, *a, **kw):
        if "method_whitelist" in kw:
            kw["allowed_methods"] = kw.pop("method_whitelist")
        _orig_retry_init(self, *a, **kw)
    _Retry.__init__ = _patched_retry_init
except Exception:
    pass

from pytrends.request import TrendReq
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Server setup
# ---------------------------------------------------------------------------

mcp = FastMCP("trend-intelligence")


# ---------------------------------------------------------------------------
# Internal helper — returns structured dict (used by idea_validator)
# ---------------------------------------------------------------------------

async def _trend_score_impl(keyword: str, location: str = "India") -> dict:
    """Dict-returning version of trend_score for cross-module orchestration."""
    import statistics
    try:
        geo = _geo_code(location)
        pt  = _make_pytrends()
        df  = await asyncio.to_thread(_fetch_with_retry, pt, [keyword], "today 12-m", geo)

        if df is None or df.empty or keyword not in df.columns:
            return {"error": "no_data", "score": 0, "direction": "unknown", "peak_month": "N/A"}

        series = df[keyword].dropna()
        if len(series) == 0 or series.max() == 0:
            return {"score": 0, "direction": "falling", "peak_month": "N/A", "seasonal": False}

        current_score = int(series.iloc[-1])
        avg_score     = float(series.mean())

        if len(series) >= 8:
            recent_avg = float(series.iloc[-4:].mean())
            prior_avg  = float(series.iloc[-8:-4].mean())
        else:
            recent_avg, prior_avg = float(series.iloc[-1]), float(series.iloc[0])

        if prior_avg == 0:
            direction = "flat"
        else:
            pct = ((recent_avg - prior_avg) / prior_avg) * 100
            direction = "rising" if pct > 5 else "falling" if pct < -5 else "flat"

        peak_idx   = series.idxmax()
        peak_month = peak_idx.strftime("%B %Y") if hasattr(peak_idx, "strftime") else str(peak_idx)

        vals = list(series)
        is_seasonal = False
        if len(vals) >= 4 and avg_score > 0:
            try:
                is_seasonal = statistics.stdev(vals) / avg_score > 0.35
            except Exception:
                pass

        return {
            "score":      current_score,
            "avg_score":  round(avg_score, 1),
            "direction":  direction,
            "peak_month": peak_month,
            "seasonal":   is_seasonal,
        }
    except Exception as e:
        return {"error": str(e), "score": 50, "direction": "unknown"}

# ---------------------------------------------------------------------------
# Pytrends helper — single shared instance with retry wrapper
# ---------------------------------------------------------------------------

def _make_pytrends() -> TrendReq:
    return TrendReq(
        hl="en-US",
        tz=360,
        timeout=(10, 25),
        retries=3,
        backoff_factor=0.5,
    )


def _geo_code(location: str) -> str:
    """Convert human-friendly location strings to pytrends geo codes."""
    mapping = {
        "india": "IN",
        "us": "US",
        "usa": "US",
        "united states": "US",
        "uk": "GB",
        "united kingdom": "GB",
        "australia": "AU",
        "canada": "CA",
        "global": "",
        "worldwide": "",
    }
    lower = location.lower().strip()
    if lower in mapping:
        return mapping[lower]
    # Already a valid code like "IN-TN", "US-CA" etc.
    return location.upper()


def _fetch_with_retry(pt: TrendReq, kw_list: list[str], timeframe: str, geo: str, max_retries: int = 3):
    """Build payload and fetch interest_over_time with exponential backoff on 429."""
    for attempt in range(max_retries):
        try:
            pt.build_payload(kw_list, cat=0, timeframe=timeframe, geo=geo, gprop="")
            df = pt.interest_over_time()
            return df
        except Exception as exc:
            err_str = str(exc).lower()
            if "429" in err_str or "too many requests" in err_str:
                wait = (2 ** attempt) * 5  # 5, 10, 20 seconds — Google is aggressive
                time.sleep(wait)
                if attempt == max_retries - 1:
                    raise
            else:
                raise
    return None  # unreachable but satisfies type checkers


# ---------------------------------------------------------------------------
# Tool 1: trend_score
# ---------------------------------------------------------------------------

@mcp.tool()
def trend_score(
    keyword: str,
    location: str = "India",
    timeframe: str = "today 12-m",
) -> str:
    """
    Returns the current Google Trends interest score for a keyword.

    Provides: current score (0-100), direction (rising/flat/falling),
    % change vs 3 months ago, peak month, and seasonality flag.

    Args:
        keyword: Search term to analyse (e.g. "cold brew coffee")
        location: Country or region — "India", "IN-TN" for Tamil Nadu, "US" etc.
        timeframe: Pytrends timeframe string, default "today 12-m"
    """
    try:
        geo = _geo_code(location)
        pt = _make_pytrends()
        df = _fetch_with_retry(pt, [keyword], timeframe, geo)

        if df is None or df.empty or keyword not in df.columns:
            return f"**trend_score({keyword})**\n\nNo data returned by Google Trends for this keyword/location combination. Try broadening the location or timeframe."

        series = df[keyword].dropna()
        if len(series) == 0:
            return f"**trend_score({keyword})**\n\nAll values are zero — this keyword has negligible search interest in the selected region."

        current_score = int(series.iloc[-1])
        avg_score = float(series.mean())

        # Direction: compare last 4 weeks vs prior 4 weeks
        if len(series) >= 8:
            recent_avg = float(series.iloc[-4:].mean())
            prior_avg = float(series.iloc[-8:-4].mean())
        else:
            recent_avg = float(series.iloc[-1])
            prior_avg = float(series.iloc[0])

        if prior_avg == 0:
            pct_change = 0.0
            direction = "flat"
        else:
            pct_change = ((recent_avg - prior_avg) / prior_avg) * 100
            if pct_change > 5:
                direction = "rising"
            elif pct_change < -5:
                direction = "falling"
            else:
                direction = "flat"

        # % change vs 3 months ago (approx 13 weeks)
        if len(series) >= 13:
            three_months_ago_score = float(series.iloc[-13])
            if three_months_ago_score == 0:
                vs_3m = "N/A (was zero 3 months ago)"
            else:
                vs_3m_pct = ((current_score - three_months_ago_score) / three_months_ago_score) * 100
                vs_3m = f"{vs_3m_pct:+.1f}%"
        else:
            vs_3m = "Insufficient data for 3-month comparison"

        # Peak month
        peak_idx = series.idxmax()
        peak_month = peak_idx.strftime("%B %Y") if hasattr(peak_idx, "strftime") else str(peak_idx)

        # Seasonality heuristic: coefficient of variation
        import statistics
        values = list(series)
        if len(values) >= 4 and avg_score > 0:
            try:
                stdev = statistics.stdev(values)
                cv = stdev / avg_score
                is_seasonal = cv > 0.35
            except Exception:
                is_seasonal = False
        else:
            is_seasonal = False

        seasonal_note = "Yes (high variance detected)" if is_seasonal else "No (relatively stable)"

        direction_emoji = {"rising": "↑", "falling": "↓", "flat": "→"}[direction]

        result = f"""## Trend Score: "{keyword}"
**Location:** {location} | **Timeframe:** {timeframe}

| Metric | Value |
|--------|-------|
| Current Interest Score | {current_score} / 100 |
| Average Score (period) | {avg_score:.1f} |
| Direction | {direction_emoji} {direction.upper()} |
| Change vs 3 months ago | {vs_3m} |
| Peak Month | {peak_month} |
| Seasonal Pattern | {seasonal_note} |

*Score of 100 = peak popularity; 0 = no data or near zero interest.*
"""
        return result

    except Exception as exc:
        return f"**trend_score error:** {exc}\n\nTip: Check that the keyword is valid and try again. Google Trends may be temporarily rate-limiting requests."


# ---------------------------------------------------------------------------
# Tool 2: compare_keywords
# ---------------------------------------------------------------------------

@mcp.tool()
def compare_keywords(
    keywords: list,
    location: str = "India",
    timeframe: str = "today 12-m",
) -> str:
    """
    Compares up to 5 keywords side by side using Google Trends data.

    Returns each keyword's average interest score, trend direction, and declares a winner.

    Args:
        keywords: List of 2-5 search terms to compare
        location: Country or region — "India", "IN-TN", "US" etc.
        timeframe: Pytrends timeframe string, default "today 12-m"
    """
    try:
        if not keywords or len(keywords) < 2:
            return "**compare_keywords error:** Please provide at least 2 keywords."
        if len(keywords) > 5:
            keywords = keywords[:5]
            note = "\n> Note: Only the first 5 keywords were used (pytrends limit)."
        else:
            note = ""

        geo = _geo_code(location)
        pt = _make_pytrends()

        time.sleep(2)  # polite delay before call
        df = _fetch_with_retry(pt, keywords, timeframe, geo)

        if df is None or df.empty:
            return f"**compare_keywords**\n\nNo data returned by Google Trends. Try different keywords or a broader location/timeframe."

        rows = []
        results = {}

        for kw in keywords:
            if kw not in df.columns:
                results[kw] = {"avg": 0.0, "current": 0, "direction": "no data"}
                continue

            series = df[kw].dropna()
            if len(series) == 0:
                results[kw] = {"avg": 0.0, "current": 0, "direction": "no data"}
                continue

            avg = float(series.mean())
            current = int(series.iloc[-1])

            # Direction
            if len(series) >= 8:
                recent_avg = float(series.iloc[-4:].mean())
                prior_avg = float(series.iloc[-8:-4].mean())
            else:
                recent_avg = float(series.iloc[-1])
                prior_avg = float(series.iloc[0])

            if prior_avg == 0:
                direction = "flat"
            else:
                pct = ((recent_avg - prior_avg) / prior_avg) * 100
                if pct > 5:
                    direction = "rising ↑"
                elif pct < -5:
                    direction = "falling ↓"
                else:
                    direction = "flat →"

            results[kw] = {"avg": avg, "current": current, "direction": direction}

        # Determine winner by avg score
        valid = {k: v for k, v in results.items() if v["avg"] > 0}
        if valid:
            winner = max(valid, key=lambda k: valid[k]["avg"])
        else:
            winner = None

        # Build table
        table_rows = []
        for kw, data in results.items():
            winner_flag = " **[WINNER]**" if kw == winner else ""
            table_rows.append(
                f"| {kw}{winner_flag} | {data['avg']:.1f} | {data['current']} | {data['direction']} |"
            )

        table = "\n".join(table_rows)

        result = f"""## Keyword Comparison
**Location:** {location} | **Timeframe:** {timeframe}

| Keyword | Avg Score | Current Score | Direction |
|---------|-----------|---------------|-----------|
{table}

**Winner:** {winner if winner else "No clear winner (all zero)"}
{note}
*Avg Score = mean interest over the full period (0-100 scale).*
"""
        return result

    except Exception as exc:
        return f"**compare_keywords error:** {exc}\n\nTip: Reduce the number of keywords or check your location code."


# ---------------------------------------------------------------------------
# Tool 3: seasonal_forecast
# ---------------------------------------------------------------------------

@mcp.tool()
def seasonal_forecast(
    keyword: str,
    location: str = "India",
) -> str:
    """
    Analyses 5 years of Google Trends data to identify seasonal patterns.

    Returns: best months to launch, worst months, and peak season name.

    Args:
        keyword: Search term to analyse
        location: Country or region — "India", "IN-TN", "US" etc.
    """
    try:
        geo = _geo_code(location)
        pt = _make_pytrends()

        time.sleep(2)
        df = _fetch_with_retry(pt, [keyword], "today 5-y", geo)

        if df is None or df.empty or keyword not in df.columns:
            return f"**seasonal_forecast({keyword})**\n\nNo 5-year data available. Google Trends may not have enough history for this keyword/region."

        series = df[keyword].dropna()
        if len(series) < 12:
            return f"**seasonal_forecast({keyword})**\n\nInsufficient data points ({len(series)}) for reliable seasonal analysis. Need at least 12 weeks."

        # Aggregate by calendar month (average across all years)
        monthly = series.groupby(series.index.month).mean()

        month_names = {
            1: "January", 2: "February", 3: "March", 4: "April",
            5: "May", 6: "June", 7: "July", 8: "August",
            9: "September", 10: "October", 11: "November", 12: "December",
        }

        month_scores = {month_names[m]: float(v) for m, v in monthly.items()}

        sorted_months = sorted(month_scores.items(), key=lambda x: x[1], reverse=True)
        best_months = [m for m, _ in sorted_months[:3]]
        worst_months = [m for m, _ in sorted_months[-3:]]

        # Peak season heuristic
        peak_month_num = monthly.idxmax()
        if peak_month_num in [11, 12, 1]:
            season = "Winter / Festive Season"
        elif peak_month_num in [3, 4, 5]:
            season = "Spring / Pre-Summer"
        elif peak_month_num in [6, 7, 8]:
            season = "Summer"
        else:
            season = "Autumn / Post-Monsoon"

        # Build monthly table
        table_rows = []
        for month, score in sorted_months:
            bar = "#" * int(score / 5) if score > 0 else "-"
            table_rows.append(f"| {month:<12} | {score:5.1f} | {bar} |")

        table = "\n".join(table_rows)

        # Launch timing recommendation: aim for 4-6 weeks before peak
        peak_month_name = month_names[int(peak_month_num)]
        peak_m_index = list(month_names.values()).index(peak_month_name) + 1
        launch_m_index = ((peak_m_index - 2) % 12) + 1  # 1 month before peak
        launch_month = month_names[launch_m_index]

        result = f"""## Seasonal Forecast: "{keyword}"
**Location:** {location} | **Data:** 5-year history

### Summary
| Field | Value |
|-------|-------|
| Peak Season | {season} |
| Best Months to Launch | {", ".join(best_months)} |
| Worst Months | {", ".join(worst_months)} |
| Recommended Launch Window | {launch_month} (one month before peak) |

### Monthly Interest Breakdown (avg over 5 years)
| Month        | Score | Relative Volume |
|--------------|-------|-----------------|
{table}

*Scores are normalised 0-100. Higher = more search interest that month.*
"""
        return result

    except Exception as exc:
        return f"**seasonal_forecast error:** {exc}\n\nTip: Try a more popular keyword or a broader location."


# ---------------------------------------------------------------------------
# Tool 4: demand_gap_finder
# ---------------------------------------------------------------------------

@mcp.tool()
def demand_gap_finder(
    concepts: list,
    location: str = "India",
) -> str:
    """
    Finds high-demand, potentially under-served market opportunities.

    Ranks concepts by opportunity score (trend score x growth rate) and
    recommends launch timing: NOW / WAIT / TOO_LATE.

    Args:
        concepts: List of product/service concepts to evaluate (2-5 items)
        location: Country or region — "India", "IN-TN", "US" etc.
    """
    try:
        if not concepts or len(concepts) < 1:
            return "**demand_gap_finder error:** Please provide at least 1 concept."
        if len(concepts) > 5:
            concepts = concepts[:5]

        geo = _geo_code(location)
        pt = _make_pytrends()

        # Fetch 12-month data for growth rate
        time.sleep(2)
        df_12m = _fetch_with_retry(pt, concepts, "today 12-m", geo)

        # Fetch 3-month data for current momentum
        time.sleep(2)
        df_3m = _fetch_with_retry(pt, concepts, "today 3-m", geo)

        if (df_12m is None or df_12m.empty) and (df_3m is None or df_3m.empty):
            return "**demand_gap_finder**\n\nNo data returned. Try different concepts or check your location."

        evaluated = []

        for concept in concepts:
            # --- 12-month stats ---
            avg_12m = 0.0
            growth_rate = 0.0

            if df_12m is not None and not df_12m.empty and concept in df_12m.columns:
                s12 = df_12m[concept].dropna()
                if len(s12) >= 8:
                    avg_12m = float(s12.mean())
                    first_half = float(s12.iloc[:len(s12)//2].mean())
                    second_half = float(s12.iloc[len(s12)//2:].mean())
                    if first_half > 0:
                        growth_rate = (second_half - first_half) / first_half
                    elif second_half > 0:
                        growth_rate = 1.0  # went from 0 to something = 100% growth
                    else:
                        growth_rate = 0.0

            # --- 3-month current score ---
            current_score = 0
            if df_3m is not None and not df_3m.empty and concept in df_3m.columns:
                s3 = df_3m[concept].dropna()
                if len(s3) > 0:
                    current_score = int(s3.iloc[-1])

            # Opportunity score: blend avg interest with growth momentum
            # Use max(0, growth_rate) so negative growth doesn't boost score
            growth_factor = max(0.0, growth_rate + 1.0)  # growth_rate of 0 → factor 1.0
            opportunity_score = round(avg_12m * growth_factor, 1)

            # Launch timing
            if growth_rate > 0.20 and avg_12m > 30:
                timing = "NOW"
                timing_note = "Strong growth + solid demand — window is open"
            elif growth_rate > 0.05 and avg_12m >= 10:
                timing = "WAIT"
                timing_note = "Growing but not peak yet — launch in 1-3 months"
            elif growth_rate < -0.15 and avg_12m > 50:
                timing = "TOO_LATE"
                timing_note = "Demand peaked and declining — market may be saturated"
            elif avg_12m < 5:
                timing = "WAIT"
                timing_note = "Low current interest — may be an emerging niche"
            else:
                timing = "WAIT"
                timing_note = "Stable demand — good for steady business, less urgency"

            evaluated.append({
                "concept": concept,
                "avg_score": avg_12m,
                "current_score": current_score,
                "growth_rate": growth_rate,
                "opportunity_score": opportunity_score,
                "timing": timing,
                "timing_note": timing_note,
            })

        # Sort by opportunity score descending
        evaluated.sort(key=lambda x: x["opportunity_score"], reverse=True)

        # Build output table
        rows = []
        for rank, item in enumerate(evaluated, start=1):
            gr_pct = f"{item['growth_rate']*100:+.1f}%"
            rows.append(
                f"| #{rank} | {item['concept']} | {item['avg_score']:.1f} | "
                f"{item['current_score']} | {gr_pct} | {item['opportunity_score']:.1f} | "
                f"**{item['timing']}** |"
            )

        table = "\n".join(rows)

        # Notes section
        notes = []
        for item in evaluated:
            notes.append(f"- **{item['concept']}** ({item['timing']}): {item['timing_note']}")
        notes_str = "\n".join(notes)

        result = f"""## Demand Gap Analysis
**Location:** {location} | **Analysis:** 12-month trends + 3-month momentum

### Opportunity Ranking
| Rank | Concept | Avg Score | Current | Growth | Opp. Score | Timing |
|------|---------|-----------|---------|--------|------------|--------|
{table}

### Launch Timing Notes
{notes_str}

---
**How to read this:**
- **Opp. Score** = Avg Interest × Growth Factor (higher = bigger opportunity)
- **NOW** = Launch immediately, demand is rising fast
- **WAIT** = Monitor for 1-3 months before committing
- **TOO_LATE** = Market may already be saturated

*Note: High search interest does not guarantee low competition. Validate with market research.*
"""
        return result

    except Exception as exc:
        return f"**demand_gap_finder error:** {exc}\n\nTip: Ensure all concepts are valid search terms. Retry after a few seconds if rate-limited."


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main():
    await mcp.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(main())
