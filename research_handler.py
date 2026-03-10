"""research_handler.py

Company intelligence research tool.
Sources: SEC EDGAR, USASpending.gov, Google News RSS
Analysis: uses the configured CLI runner (Claude / Gemini / Codex)

Usage:
    # In server.py lifespan startup:
    if research_handler:
        research_handler.init(runner)

    # Then call:
    report = await research_company("Apple Inc")
    report = await research_objective("improve voice-based AI")
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import httpx

logger = logging.getLogger("bridge.research")

# Runner is set via init() — uses whatever CLI the user configured (Claude/Gemini/Codex)
_runner = None

# Research reports are saved to MEMORY_DIR/Research/
# MEMORY_DIR is read from environment (same as config.py)
_MEMORY_DIR = os.environ.get("MEMORY_DIR", str(Path.home() / "memories"))
RESEARCH_DIR = Path(_MEMORY_DIR) / "Research"

# SEC EDGAR requires a User-Agent with contact info per their access policy.
# Set EDGAR_CONTACT in your .env to your email address.
_EDGAR_CONTACT = os.environ.get("EDGAR_CONTACT", "research@example.com")
EDGAR_HEADERS = {
    "User-Agent": f"TgCliBridgeResearch/1.0 ({_EDGAR_CONTACT})",
    "Accept": "application/json",
}

NEWS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}


def init(runner) -> None:
    """Set the AI runner used for analysis. Call this from server.py lifespan."""
    global _runner
    _runner = runner


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

async def fetch_edgar(company: str) -> list[dict]:
    """Search SEC EDGAR full-text search for recent filings mentioning vendors/contracts."""
    start_date = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
    url = "https://efts.sec.gov/LATEST/search-index"
    params = {
        "q": f'"{company}"',
        "forms": "8-K,10-K,10-Q",
        "dateRange": "custom",
        "startdt": start_date,
    }
    filings = []
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url, params=params, headers=EDGAR_HEADERS)
            if r.status_code == 200:
                data = r.json()
                hits = (data.get("hits") or {}).get("hits") or []
                for h in hits[:10]:
                    src = h.get("_source", {})
                    accession = h.get("_id", "")
                    acc_clean = accession.replace("-", "")
                    entity_id = src.get("entity_id", "")
                    if entity_id and acc_clean:
                        filing_url = (
                            f"https://www.sec.gov/Archives/edgar/data/"
                            f"{entity_id.lstrip('0')}/{acc_clean}/{accession}-index.htm"
                        )
                    else:
                        filing_url = ""
                    filings.append({
                        "form": src.get("form_type", ""),
                        "date": src.get("file_date", ""),
                        "entity": src.get("entity_name", company),
                        "period": src.get("period_of_report", ""),
                        "url": filing_url,
                    })
    except Exception as e:
        logger.warning(f"EDGAR fetch error: {e}")
    return filings


async def fetch_usaspending(company: str) -> list[dict]:
    """Search USASpending.gov for government contracts awarded to this company."""
    url = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
    body = {
        "filters": {
            "recipient_search_text": [company],
            "award_type_codes": ["A", "B", "C", "D"],
        },
        "fields": [
            "Award ID",
            "Recipient Name",
            "Award Amount",
            "Description",
            "Period of Performance Start Date",
            "awarding_agency_name",
        ],
        "sort": "Award Amount",
        "order": "desc",
        "limit": 8,
        "page": 1,
    }
    contracts = []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, json=body)
            if r.status_code == 200:
                data = r.json()
                for award in (data.get("results") or [])[:8]:
                    amount = award.get("Award Amount") or 0
                    contracts.append({
                        "id": award.get("Award ID") or "",
                        "recipient": award.get("Recipient Name") or "",
                        "amount": amount,
                        "description": (award.get("Description") or "")[:120],
                        "agency": award.get("awarding_agency_name") or "",
                        "date": award.get("Period of Performance Start Date") or "",
                    })
    except Exception as e:
        logger.warning(f"USASpending fetch error: {e}")
    return contracts


async def fetch_news(company: str) -> list[dict]:
    """Fetch recent news via Google News RSS (vendor/contract/partnership angle)."""
    query = quote_plus(f"{company} vendor OR contract OR partnership OR supplier OR acquisition")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    articles = []
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url, headers=NEWS_HEADERS)
            if r.status_code == 200:
                root = ET.fromstring(r.content)
                channel = root.find("channel")
                if channel is not None:
                    for item in channel.findall("item")[:12]:
                        title = item.findtext("title", "")
                        link = item.findtext("link", "")
                        pub_date = item.findtext("pubDate", "")
                        desc = re.sub(r"<[^>]+>", "", item.findtext("description", ""))
                        articles.append({
                            "title": title,
                            "url": link,
                            "date": pub_date[:25] if pub_date else "",
                            "summary": desc[:200].strip(),
                        })
    except Exception as e:
        logger.warning(f"News fetch error: {e}")
    return articles


async def fetch_sec_vendor_mentions(company: str) -> list[dict]:
    """Search EDGAR specifically for vendor/supplier relationship disclosures."""
    start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    url = "https://efts.sec.gov/LATEST/search-index"
    params = {
        "q": f'"{company}" ("key supplier" OR "strategic partner" OR "primary vendor" OR "material contract")',
        "forms": "10-K,10-Q",
        "dateRange": "custom",
        "startdt": start_date,
    }
    results = []
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url, params=params, headers=EDGAR_HEADERS)
            if r.status_code == 200:
                data = r.json()
                hits = (data.get("hits") or {}).get("hits") or []
                for h in hits[:6]:
                    src = h.get("_source", {})
                    results.append({
                        "form": src.get("form_type", ""),
                        "date": src.get("file_date", ""),
                        "entity": src.get("entity_name", company),
                    })
    except Exception as e:
        logger.warning(f"EDGAR vendor mention error: {e}")
    return results


# ---------------------------------------------------------------------------
# Analysis via CLI runner
# ---------------------------------------------------------------------------

async def _analyze(
    company: str,
    filings: list[dict],
    contracts: list[dict],
    news: list[dict],
    vendor_mentions: list[dict],
) -> str:
    """Build structured data and send to the configured CLI runner for analysis."""
    if _runner is None:
        return "⚠️ Research analysis unavailable — runner not initialized."

    filing_lines = []
    for f in filings[:6]:
        filing_lines.append(f"[{f['form']}] {f['entity']} | Filed: {f['date']} | Period: {f['period']}")
    filing_text = "\n".join(filing_lines) if filing_lines else "No recent filings found."

    vendor_lines = []
    for v in vendor_mentions[:4]:
        vendor_lines.append(f"[{v['form']}] {v['entity']} | {v['date']}")
    vendor_text = "\n".join(vendor_lines) if vendor_lines else "No vendor-specific disclosures found."

    contract_lines = []
    for c in contracts[:6]:
        amt = f"${c['amount']:,.0f}" if c["amount"] else "undisclosed"
        contract_lines.append(
            f"• {c['recipient']} | {amt} | {c['agency']} | {(c['date'] or '')[:10]}\n"
            f"  Desc: {c['description']}"
        )
    contract_text = "\n".join(contract_lines) if contract_lines else "No government contracts found."

    news_lines = []
    for n in news[:8]:
        news_lines.append(f"• {n['title']} [{n['date']}]\n  {n['summary']}")
    news_text = "\n".join(news_lines) if news_lines else "No recent news found."

    prompt = f"""You are a senior business intelligence analyst specializing in investment research and vendor ecosystem mapping.

Analyze the following publicly available data about {company} and produce a structured intelligence report.

--- SEC EDGAR FILINGS (recent) ---
{filing_text}

--- EDGAR VENDOR/SUPPLIER DISCLOSURES ---
{vendor_text}

--- GOVERNMENT CONTRACTS (USASpending.gov) ---
{contract_text}

--- RECENT NEWS (vendor/contract/partnership focus) ---
{news_text}

---

Produce the following report sections:

## 🤝 VENDOR RELATIONSHIPS
List known vendors, suppliers, key partners, and their relationship type. Be specific — name companies where possible. If data is sparse, note what types of vendors they likely rely on given their industry.

## 💡 KEY INSIGHTS
5 sharp bullet points on what this data reveals about {company}'s strategic direction, spending priorities, and supplier dependencies.

## 📋 CONTRACTS & SPENDING PATTERNS
Summarize notable contracts, procurement patterns, or spending trends. Include dollar amounts where available.

## 🔮 TACTICAL FORECAST
Based on the patterns above, speculate on:
1. What smaller companies {company} is likely to partner with or acquire next
2. Which sectors they are investing resources into
3. One or two specific investment themes for opportunistic plays

Keep the tone analytical, direct, and concise. Format for easy scanning. No fluff."""

    try:
        return await _runner.run_query(prompt, timeout_secs=180)
    except asyncio.TimeoutError:
        logger.warning("Research analysis timed out")
        return "⚠️ AI analysis timed out. Raw data was collected — try asking for a summary manually."
    except Exception as e:
        logger.warning(f"Research analysis error: {e}")
        return "⚠️ AI analysis unavailable. Raw data collected above."


async def _analyze_objective(objective: str, articles: list[dict]) -> str:
    """Use the CLI runner to extract companies and their approaches toward an objective."""
    if _runner is None:
        return "⚠️ Objective analysis unavailable — runner not initialized."

    news_lines = []
    for n in articles:
        news_lines.append(f"• {n['title']} [{n['date']}]\n  {n['summary']}")
    news_text = "\n".join(news_lines) if news_lines else "No articles found."

    prompt = f"""You are a senior market intelligence analyst. Your task: identify companies working toward a specific objective and describe exactly what each company is doing to achieve it.

OBJECTIVE: {objective}

--- NEWS ARTICLES ---
{news_text}

---

From the articles above, extract every distinct company (startups, public companies, research labs, etc.) that is actively working toward the objective: "{objective}"

For each company, write ONE clear sentence describing their specific approach, product, or investment toward that objective. Be concrete — name the actual technology, product, or action, not generic statements.

Format your response EXACTLY like this (one company per line, no extra text before or after):

Company Name | What they are doing toward the objective
Company Name | What they are doing toward the objective

If a company appears in multiple articles, merge into one line with the most complete description.
List in order of most to least relevant. Include at minimum 5 companies, up to 15.
Only include companies where you have clear evidence from the articles above."""

    try:
        return await _runner.run_query(prompt, timeout_secs=180)
    except asyncio.TimeoutError:
        logger.warning("Objective analysis timed out")
        return "⚠️ AI analysis timed out."
    except Exception as e:
        logger.warning(f"Objective analysis error: {e}")
        return "⚠️ AI analysis unavailable."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def fetch_objective_news(objective: str) -> list[dict]:
    """Fetch news articles about companies working toward a specific objective."""
    queries = [
        f"{objective} company startup progress",
        f"{objective} technology investment funding",
        f"{objective} breakthrough development",
    ]
    articles = []
    seen_titles = set()
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for query in queries:
            try:
                url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
                r = await client.get(url, headers=NEWS_HEADERS)
                if r.status_code == 200:
                    root = ET.fromstring(r.content)
                    channel = root.find("channel")
                    if channel is not None:
                        for item in channel.findall("item")[:10]:
                            title = item.findtext("title", "")
                            if title in seen_titles:
                                continue
                            seen_titles.add(title)
                            link = item.findtext("link", "")
                            pub_date = item.findtext("pubDate", "")
                            desc = re.sub(r"<[^>]+>", "", item.findtext("description", ""))
                            articles.append({
                                "title": title,
                                "url": link,
                                "date": pub_date[:25] if pub_date else "",
                                "summary": desc[:300].strip(),
                            })
            except Exception as e:
                logger.warning(f"Objective news fetch error ({query}): {e}")
    return articles[:30]


def _save_research(filename: str, content: str) -> None:
    """Strip HTML tags and save research result to MEMORY_DIR/Research/."""
    try:
        RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
        plain = re.sub(r"<[^>]+>", "", content)
        plain = plain.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        filepath = RESEARCH_DIR / filename
        filepath.write_text(plain, encoding="utf-8")
        logger.info(f"Research saved to {filepath}")
    except Exception as e:
        logger.warning(f"Failed to save research file: {e}")


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

async def research_company(company: str) -> str:
    """
    Main entry point for /research. Returns a Telegram-ready HTML string.
    Gathers SEC filings, government contracts, and news, then runs
    AI analysis to produce a structured intelligence report.
    """
    logger.info(f"Starting research for: {company}")

    results = await asyncio.gather(
        fetch_edgar(company),
        fetch_usaspending(company),
        fetch_news(company),
        fetch_sec_vendor_mentions(company),
        return_exceptions=True,
    )

    filings     = results[0] if not isinstance(results[0], Exception) else []
    contracts   = results[1] if not isinstance(results[1], Exception) else []
    news        = results[2] if not isinstance(results[2], Exception) else []
    vendor_ment = results[3] if not isinstance(results[3], Exception) else []

    logger.info(
        f"Data gathered — filings: {len(filings)}, contracts: {len(contracts)}, "
        f"news: {len(news)}, vendor mentions: {len(vendor_ment)}"
    )

    analysis = await _analyze(company, filings, contracts, news, vendor_ment)

    enc = quote_plus(company)
    edgar_browse = (
        f"https://www.sec.gov/cgi-bin/browse-edgar?company={enc}"
        f"&type=8-K&dateb=&owner=include&count=40&search_text=&action=getcompany"
    )
    edgar_fts = (
        f"https://efts.sec.gov/LATEST/search-index?q=%22{enc}%22"
        f"&forms=8-K%2C10-K&dateRange=custom"
        f"&startdt={(datetime.now() - timedelta(days=730)).strftime('%Y-%m-%d')}"
    )
    spending_url = f"https://www.usaspending.gov/recipient?keyword={enc}"
    news_url = f"https://news.google.com/search?q={enc}+vendor+contract&hl=en-US"

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = (
        f"<b>📊 INTEL REPORT: {company.upper()}</b>\n"
        f"<i>Generated: {now} | Sources: EDGAR + USASpending + News</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    sources_section = (
        "\n\n━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>📎 SOURCES & LINKS</b>\n"
        f"• <a href='{edgar_browse}'>SEC EDGAR — Recent Filings</a>\n"
        f"• <a href='{edgar_fts}'>EDGAR Full-Text Search</a>\n"
        f"• <a href='{spending_url}'>USASpending.gov Contracts</a>\n"
        f"• <a href='{news_url}'>Google News — Vendor/Contract</a>\n"
    )

    news_links = [n for n in news[:4] if n.get("url")]
    if news_links:
        sources_section += "\n<b>Latest Articles:</b>\n"
        for n in news_links:
            title = (n["title"] or "")[:55] + ("…" if len(n["title"] or "") > 55 else "")
            sources_section += f"• <a href='{n['url']}'>{title}</a>\n"

    report = header + analysis + sources_section

    safe_name = re.sub(r"[^\w\s-]", "", company).strip().replace(" ", "_")
    date_str = datetime.now().strftime("%Y-%m-%d")
    _save_research(f"{safe_name}_{date_str}.md", report)

    return report


async def research_objective(objective: str) -> str:
    """
    Main entry point for /objective. Given a goal/theme, finds companies actively
    working toward it and describes what each one is doing.
    Returns a Telegram-ready HTML string.
    """
    logger.info(f"Starting objective research for: {objective}")

    articles = await fetch_objective_news(objective)
    logger.info(f"Objective research — {len(articles)} articles gathered")

    raw = await _analyze_objective(objective, articles)

    entries = []
    for line in raw.splitlines():
        line = line.strip()
        if "|" in line and len(line) > 5:
            parts = line.split("|", 1)
            if len(parts) == 2:
                company = parts[0].strip().lstrip("•-0123456789. ")
                desc = parts[1].strip()
                if company and desc:
                    entries.append((company, desc))

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = (
        f"<b>🎯 OBJECTIVE RESEARCH: {objective.upper()}</b>\n"
        f"<i>Generated: {now} | {len(entries)} companies identified</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    if entries:
        lines = []
        for company, desc in entries:
            lines.append(f"<b>{company}</b>\n{desc}\n")
        body = "\n".join(lines)
    else:
        body = raw

    enc = quote_plus(objective)
    sources = (
        "\n━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>📎 Sources</b>\n"
        f"• <a href='https://news.google.com/search?q={enc}&hl=en-US'>Google News — {objective}</a>\n"
    )
    news_links = [n for n in articles[:4] if n.get("url")]
    for n in news_links:
        title = (n["title"] or "")[:55] + ("…" if len(n["title"] or "") > 55 else "")
        sources += f"• <a href='{n['url']}'>{title}</a>\n"

    report = header + body + sources

    safe_name = re.sub(r"[^\w\s-]", "", objective).strip().replace(" ", "_")[:60]
    date_str = datetime.now().strftime("%Y-%m-%d")
    _save_research(f"OBJECTIVE_{safe_name}_{date_str}.md", report)

    return report
