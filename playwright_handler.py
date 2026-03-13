"""Playwright browser automation handler for bridgebot.

Provides async screenshot and page-content extraction using Chromium (headless).
Used by /screenshot and /browse commands, and available for bots via shell.

Install: pip install playwright && playwright install chromium
"""

import asyncio
import logging
import tempfile
import os

logger = logging.getLogger("bridge.playwright")

# Default viewport
VIEWPORT = {"width": 1280, "height": 900}
# Max chars returned from get_page_text
MAX_TEXT_CHARS = 8000
# Page load timeout ms
PAGE_TIMEOUT = 30000


async def screenshot(url: str, full_page: bool = False) -> str:
    """Navigate to url and take a screenshot. Returns path to PNG temp file."""
    from playwright.async_api import async_playwright

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png", prefix="pw_shot_")
    tmp.close()
    path = tmp.name

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport=VIEWPORT)
            await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
            # Brief pause for JS-rendered content
            await asyncio.sleep(1.5)
            await page.screenshot(path=path, full_page=full_page)
            logger.info("Screenshot saved: %s (%d bytes)", path, os.path.getsize(path))
        finally:
            await browser.close()

    return path


async def get_page_text(url: str) -> str:
    """Navigate to url and extract readable text content. Returns plain text."""
    from playwright.async_api import async_playwright

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport=VIEWPORT)
            await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
            await asyncio.sleep(1.0)
            title = await page.title()
            # Extract inner text (strips HTML, keeps structure)
            text = await page.evaluate("""() => {
                // Remove script/style nodes
                document.querySelectorAll('script,style,nav,footer,header,[aria-hidden="true"]')
                    .forEach(el => el.remove());
                return document.body ? document.body.innerText : '';
            }""")
        finally:
            await browser.close()

    # Clean up whitespace
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    clean = "\n".join(lines)
    if len(clean) > MAX_TEXT_CHARS:
        clean = clean[:MAX_TEXT_CHARS] + f"\n\n[...truncated at {MAX_TEXT_CHARS} chars]"
    return f"**{title}**\n\n{clean}" if title else clean


async def screenshot_and_text(url: str) -> tuple[str, str]:
    """Take screenshot AND extract text. Returns (png_path, text)."""
    png_path, text = await asyncio.gather(
        screenshot(url),
        get_page_text(url),
    )
    return png_path, text
