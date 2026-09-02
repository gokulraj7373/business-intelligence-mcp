# Business Intelligence MCP Servers

Eleven [Model Context Protocol](https://modelcontextprotocol.io) servers that give an AI assistant live access to the data a small business actually needs — competitors, reviews, delivery platforms, search demand, hiring signals.

I run a café in Tiruppur, Tamil Nadu, and I'm opening a restaurant. I built these because the decisions in front of me — which locality, what to price a dish at, which competitor is actually growing — needed evidence, and no product would give me it. These aren't demos. I use them every week.

## The servers

| File | What it answers |
|---|---|
| `local_seo.py` | Who are my competitors within N km, how many reviews are they gaining per month, what's their estimated foot traffic and revenue band. Google Places + Popular Times. |
| `review_analytics.py` | What are people actually complaining about, by category, over time — and when did sentiment turn. Scrapes Maps reviews, classifies with Gemini. |
| `delivery_intel.py` | Is this restaurant on Swiggy and Zomato, what's on the menu, what do order-volume signals suggest. |
| `justdial.py` | Business listings and reviews from JustDial, and how they compare against Google. |
| `trends.py` | Search-demand scoring, keyword comparison, seasonal forecasting, demand-gap finding. Built on pytrends. |
| `signal_monitor.py` | Hiring signals from job boards, social momentum, and a weekly digest — early indicators that a competitor is expanding. |
| `metasearch.py` | Multi-engine web, news and social search with deduplication, so one query doesn't return the same story five times. |
| `youtube_intel.py` | Channel analysis, transcripts, topic research. |
| `instagram.py` | Profile and recent-post analysis, hashtag research, profile comparison. |
| `idea_validator.py` | Scores a business idea against trend, competitor, demand and delivery signals plus unit economics, and returns a verdict with a confidence band. |
| `server.py` | Page fetching, site crawling, link extraction, batch fetch, screenshots. |

Roughly 7,700 lines of Python. All of them speak MCP over stdio and drop into Claude Desktop, Claude Code, or any MCP client.

## Install

```bash
git clone https://github.com/gokulraj7373/business-intelligence-mcp.git
cd business-intelligence-mcp
python -m venv venv
source venv/bin/activate          # Windows: .\venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # then add your keys
```

Point your MCP client at whichever servers you want:

```json
{
  "mcpServers": {
    "local-seo": {
      "command": "/absolute/path/to/venv/bin/python",
      "args": ["/absolute/path/to/local_seo.py"],
      "env": { "GOOGLE_PLACES_API_KEY": "..." }
    }
  }
}
```

## Keys

| Variable | Used by | From |
|---|---|---|
| `GOOGLE_PLACES_API_KEY` | `local_seo`, `review_analytics`, `justdial`, `idea_validator` | Google Cloud Console |
| `GEMINI_API_KEY` | `review_analytics`, `idea_validator` | Google AI Studio |

Everything else runs without a key. Set a spend cap on both.

## A note on scraping

Several servers read public pages from platforms that offer no API for what I needed. They rate-limit themselves and use polite delays, but you are responsible for how you point them — check the terms of the platform first.

## Why they look the way they do

Every server returns *shaped* answers, not raw dumps. `local_seo` doesn't hand back a JSON blob of places; it returns a ranked competitor table with a computed competitive score, because that's what I need in order to decide something. Working out what a useful answer looks like is the actual work. The scraping is the easy part.

---

Built with Claude Code. MIT licensed.
