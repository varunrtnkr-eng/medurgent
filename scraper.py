"""
MedUrgent — scrapes Indian medical crowdfunding sites and ranks by days-to-deadline.

Run: python scraper.py
Output: data.json (consumed by index.html)

NOTE ON SELECTORS:
Each site scraper has a CONFIG block at the top with CSS selectors. These are starting
points — site HTML changes and selectors WILL need tuning. Run with HEADLESS = False
on first run, watch the browser, and inspect the DOM to update selectors.

The cleaner approach for most of these sites is intercepting their internal JSON API
calls (DevTools → Network → XHR). Each scraper class has a `_try_api_scrape` hook
for that path — populate the URL pattern and JSON parsing logic once you've found it.
"""

import asyncio
import json
import os
import re
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urljoin

from playwright.async_api import async_playwright, Page, BrowserContext
from dateutil import parser as dateparser

try:
    from anthropic import Anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HEADLESS = False                 # Set False to watch browser while debugging
MAX_CAMPAIGNS_PER_SITE = 30      # How many to attempt to pull per site
PAGE_TIMEOUT_MS = 30_000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
ONLY_VERIFIED = False            # Only emit campaigns marked verified by the platform
DROP_IF_FUNDED_OVER = 1.5       # Drop campaigns this fraction funded (don't dilute urgent need)
OUTPUT_PATH = "data.json"

# WhatsApp copy generation via Claude API
# Set ANTHROPIC_API_KEY in env (locally) or as a GitHub Actions secret (in CI)
# Keep this small; each campaign = 1 API call
GENERATE_COPY = True
MAX_COPY_GEN = 30                # Only generate copy for the top N ranked campaigns (cost control)
COPY_MODEL = "claude-sonnet-4-20250514"
LANGUAGES = {
    "en": "English",
    "hi": "Hindi (use Devanagari script)",
    "kn": "Kannada (use Kannada script)",
    "ta": "Tamil (use Tamil script)",
    "te": "Telugu (use Telugu script)",
    "mr": "Marathi (use Devanagari script)",
    "bn": "Bengali (use Bengali script)",
    "gu": "Gujarati (use Gujarati script)",
    "pa": "Punjabi (use Gurmukhi script)",
    "ml": "Malayalam (use Malayalam script)",
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Campaign:
    id: str
    source: str
    title: str
    description: str
    patient_name: Optional[str]
    condition: Optional[str]
    image_url: Optional[str]
    amount_raised_inr: float
    amount_goal_inr: float
    deadline_iso: Optional[str]
    days_left: Optional[int]
    campaign_url: str
    verified: bool
    messages: dict = field(default_factory=dict)   # lang_code -> WhatsApp text
    scraped_at_iso: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def funded_fraction(self) -> float:
        if self.amount_goal_inr <= 0:
            return 0.0
        return min(1.0, self.amount_raised_inr / self.amount_goal_inr)

    @property
    def funding_gap_inr(self) -> float:
        return max(0.0, self.amount_goal_inr - self.amount_raised_inr)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_inr(text: str) -> float:
    """Parse '₹3,45,678' or '34.5 Lakhs' or '1.2 Cr' into a float."""
    if not text:
        return 0.0
    t = text.replace("\xa0", " ").replace(",", "").strip()
    t = re.sub(r"[₹Rs.]", "", t, flags=re.IGNORECASE).strip()
    multiplier = 1.0
    if re.search(r"\bcr\b|crore", t, re.IGNORECASE):
        multiplier = 10_000_000
        t = re.sub(r"cr|crore", "", t, flags=re.IGNORECASE)
    elif re.search(r"\blakh\b|\blac\b|\bl\b", t, re.IGNORECASE):
        multiplier = 100_000
        t = re.sub(r"lakh|lac|\bl\b", "", t, flags=re.IGNORECASE)
    elif re.search(r"\bk\b", t, re.IGNORECASE):
        multiplier = 1_000
        t = re.sub(r"\bk\b", "", t, flags=re.IGNORECASE)
    try:
        return float(re.findall(r"[\d.]+", t)[0]) * multiplier
    except (IndexError, ValueError):
        return 0.0


def parse_days_left(text: str) -> Optional[int]:
    """Extract days from things like '12 days left', '3 days remaining'."""
    if not text:
        return None
    m = re.search(r"(\d+)\s*day", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*hour", text, re.IGNORECASE)
    if m:
        return 0
    return None


def deadline_from_days_left(days: Optional[int]) -> Optional[str]:
    if days is None:
        return None
    return (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()


# ---------------------------------------------------------------------------
# Base scraper
# ---------------------------------------------------------------------------
class BaseScraper(ABC):
    source_name: str = "base"
    listing_url: str = ""

    async def scrape(self, context: BrowserContext) -> list[Campaign]:
        # Prefer API path if subclass has implemented it
        try:
            api_results = await self._try_api_scrape(context)
            if api_results:
                return api_results
        except NotImplementedError:
            pass
        except Exception as e:
            print(f"[{self.source_name}] API path failed: {e}; falling back to DOM scrape", file=sys.stderr)

        # DOM fallback
        page = await context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)
        try:
            await page.goto(self.listing_url, wait_until="domcontentloaded")
            await self._wait_for_cards(page)
            return await self._scrape_dom(page)
        finally:
            await page.close()

    async def _try_api_scrape(self, context: BrowserContext) -> list[Campaign]:
        raise NotImplementedError

    @abstractmethod
    async def _wait_for_cards(self, page: Page) -> None:
        ...

    @abstractmethod
    async def _scrape_dom(self, page: Page) -> list[Campaign]:
        ...


# ---------------------------------------------------------------------------
# Ketto
# ---------------------------------------------------------------------------
class KettoScraper(BaseScraper):
    source_name = "ketto"
    listing_url = "https://www.ketto.org/crowdfunding/fundraisers"
    API_HOST = "msearch.ketto.org"

    async def _try_api_scrape(self, context: BrowserContext) -> list[Campaign]:
        captured: dict = {}

        async def handle_response(response):
            if self.API_HOST in response.url and not captured:
                try:
                    body = await response.json()
                    if isinstance(body, dict) and "hits" in body:
                        captured["data"] = body
                except Exception:
                    pass

        page = await context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)
        try:
            page.on("response", handle_response)
            await page.goto(self.listing_url, wait_until="domcontentloaded")
            # Wait up to 15s for the API response to be captured
            for _ in range(30):
                if captured:
                    break
                await page.wait_for_timeout(500)
        finally:
            await page.close()

        if not captured:
            print("[ketto] API response not captured; will fall back to DOM", file=sys.stderr)
            return []

        hits = captured["data"].get("hits", [])
        if hits:
            print(f"[ketto] first raw hit: {hits[0]}", file=sys.stderr)
            print(f"FIELDS: {list(hits[0].keys())}", file=sys.stderr)

        today = datetime.now(timezone.utc).date()
        out: list[Campaign] = []
        for i, hit in enumerate(hits[:MAX_CAMPAIGNS_PER_SITE]):
            try:
                title = (hit.get("title") or "").strip()
                if not title:
                    continue

                custom_tag = hit.get("custom_tag") or ""
                url = f"https://www.ketto.org/fundraiser/{custom_tag}" if custom_tag else self.listing_url

                # Parse end_date: "2026-05-25 23:59:59"
                end_date_str = hit.get("end_date") or ""
                days_left: Optional[int] = None
                deadline_iso: Optional[str] = None
                if end_date_str:
                    try:
                        end_date = datetime.strptime(end_date_str[:10], "%Y-%m-%d").date()
                        days_left = (end_date - today).days
                        deadline_iso = end_date.isoformat()
                    except ValueError:
                        pass

                goal = float(hit.get("amount_requested") or 0)

                raised_data = hit.get("raised") or {}
                raised = float(raised_data.get("raised") or 0)

                gallery = hit.get("gallery") or []
                img = gallery[0]["cdn_path"] if gallery and gallery[0].get("cdn_path") else None

                patient_name = (hit.get("beneficiaryname") or {}).get("info_1") or None
                condition = (hit.get("disease") or {}).get("info_1") or None
                description = (hit.get("shortdescription") or {}).get("info_1") or title

                out.append(Campaign(
                    id=f"ketto-{i}-{abs(hash(url)) % 10_000_000}",
                    source=self.source_name,
                    title=title,
                    description=description,
                    patient_name=patient_name,
                    condition=condition,
                    image_url=img,
                    amount_raised_inr=raised,
                    amount_goal_inr=goal,
                    deadline_iso=deadline_iso,
                    days_left=days_left,
                    campaign_url=url,
                    verified=True,
                ))
            except Exception as e:
                print(f"[ketto] hit {i} error: {e}", file=sys.stderr)
                continue
        return out

    # DOM fallback kept in case the API endpoint changes or moves
    async def _wait_for_cards(self, page: Page) -> None:
        await page.wait_for_selector(".card-wrap", timeout=PAGE_TIMEOUT_MS)
        for _ in range(3):
            await page.mouse.wheel(0, 2000)
            await page.wait_for_timeout(500)

    async def _scrape_dom(self, page: Page) -> list[Campaign]:
        cards = await page.query_selector_all(".card-wrap")
        out: list[Campaign] = []
        for i, card in enumerate(cards[:MAX_CAMPAIGNS_PER_SITE]):
            try:
                title_el = await card.query_selector("h2, h3, [class*='title'], [class*='name']")
                title = (await title_el.inner_text()).strip() if title_el else ""
                title = title.split('Funds Required')[0].strip()
                if not title:
                    continue

                link_el = await card.query_selector("a[href*='/fundraiser/']")
                href = await link_el.get_attribute("href") if link_el else None
                url = ("https://www.ketto.org" + href) if href and href.startswith("/") else (href or self.listing_url)

                img_el = await card.query_selector("img")
                img = None
                if img_el:
                    img = await img_el.get_attribute("data-src") or await img_el.get_attribute("src")
                    if img and not img.startswith("http"):
                        img = None

                goal_el = await card.query_selector(".require")
                goal_txt = (await goal_el.inner_text()).strip() if goal_el else ""
                days_el = await card.query_selector(".days")
                days_txt = (await days_el.inner_text()).strip() if days_el else ""
                days_left = parse_days_left(days_txt)

                out.append(Campaign(
                    id=f"ketto-{i}-{abs(hash(url)) % 10_000_000}",
                    source=self.source_name,
                    title=title,
                    description=title,
                    patient_name=None,
                    condition=None,
                    image_url=img,
                    amount_raised_inr=0.0,
                    amount_goal_inr=parse_inr(goal_txt),
                    deadline_iso=deadline_from_days_left(days_left),
                    days_left=days_left,
                    campaign_url=url,
                    verified=True,
                ))
            except Exception as e:
                print(f"[ketto] card {i} error: {e}", file=sys.stderr)
                continue
        return out


# ---------------------------------------------------------------------------
# Milaap
# ---------------------------------------------------------------------------
class MilaapScraper(BaseScraper):
    source_name = "milaap"
    listing_url = "https://milaap.org/fundraisers/medical"

    CARD_SELECTOR = ".fundraiser-card, .campaign-tile, article"
    TITLE_SELECTOR = ".fundraiser-title, h3, h2"
    URL_SELECTOR = "a"
    IMAGE_SELECTOR = "img"
    RAISED_SELECTOR = ".raised, .amount-raised"
    GOAL_SELECTOR = ".goal, .amount-goal"
    DAYS_LEFT_SELECTOR = ".days-left, .time-remaining"
    VERIFIED_SELECTOR = ".verified, .trust-badge"

    async def _wait_for_cards(self, page: Page) -> None:
        await page.wait_for_selector(self.CARD_SELECTOR, timeout=PAGE_TIMEOUT_MS)
        for _ in range(3):
            await page.mouse.wheel(0, 2000)
            await page.wait_for_timeout(500)

    async def _scrape_dom(self, page: Page) -> list[Campaign]:
        # Implementation mirrors Ketto — kept separate so per-site selectors can diverge
        cards = await page.query_selector_all(self.CARD_SELECTOR)
        out: list[Campaign] = []
        for i, card in enumerate(cards[:MAX_CAMPAIGNS_PER_SITE]):
            try:
                title_el = await card.query_selector(self.TITLE_SELECTOR)
                title = (await title_el.inner_text()).strip() if title_el else ""
                if not title:
                    continue
                link_el = await card.query_selector(self.URL_SELECTOR)
                href = await link_el.get_attribute("href") if link_el else None
                url = urljoin(self.listing_url, href) if href else self.listing_url
                img_el = await card.query_selector(self.IMAGE_SELECTOR)
                img = await img_el.get_attribute("src") if img_el else None
                raised_txt = await (await card.query_selector(self.RAISED_SELECTOR) or _NullEl()).inner_text() if await card.query_selector(self.RAISED_SELECTOR) else ""
                goal_txt = await (await card.query_selector(self.GOAL_SELECTOR) or _NullEl()).inner_text() if await card.query_selector(self.GOAL_SELECTOR) else ""
                days_el = await card.query_selector(self.DAYS_LEFT_SELECTOR)
                days_txt = await days_el.inner_text() if days_el else ""
                days_left = parse_days_left(days_txt)
                verified_el = await card.query_selector(self.VERIFIED_SELECTOR)
                verified = verified_el is not None

                out.append(Campaign(
                    id=f"milaap-{i}-{abs(hash(url)) % 10_000_000}",
                    source=self.source_name,
                    title=title,
                    description=title,
                    patient_name=None,
                    condition=None,
                    image_url=img,
                    amount_raised_inr=parse_inr(raised_txt),
                    amount_goal_inr=parse_inr(goal_txt),
                    deadline_iso=deadline_from_days_left(days_left),
                    days_left=days_left,
                    campaign_url=url,
                    verified=verified,
                ))
            except Exception as e:
                print(f"[milaap] card {i} error: {e}", file=sys.stderr)
                continue
        return out


class _NullEl:
    async def inner_text(self):
        return ""


# ---------------------------------------------------------------------------
# ImpactGuru
# ---------------------------------------------------------------------------
class ImpactGuruScraper(BaseScraper):
    source_name = "impactguru"
    listing_url = "https://www.impactguru.com/medical-crowdfunding"

    CARD_SELECTOR = ".campaign-card, .fundraiser-item, article"
    TITLE_SELECTOR = "h3, h2, .campaign-title"
    URL_SELECTOR = "a"
    IMAGE_SELECTOR = "img"
    RAISED_SELECTOR = ".raised-amount"
    GOAL_SELECTOR = ".goal-amount"
    DAYS_LEFT_SELECTOR = ".days-left"
    VERIFIED_SELECTOR = ".verified-badge"

    async def _wait_for_cards(self, page: Page) -> None:
        await page.wait_for_selector(self.CARD_SELECTOR, timeout=PAGE_TIMEOUT_MS)
        for _ in range(3):
            await page.mouse.wheel(0, 2000)
            await page.wait_for_timeout(500)

    async def _scrape_dom(self, page: Page) -> list[Campaign]:
        cards = await page.query_selector_all(self.CARD_SELECTOR)
        out: list[Campaign] = []
        for i, card in enumerate(cards[:MAX_CAMPAIGNS_PER_SITE]):
            try:
                title_el = await card.query_selector(self.TITLE_SELECTOR)
                title = (await title_el.inner_text()).strip() if title_el else ""
                if not title:
                    continue
                link_el = await card.query_selector(self.URL_SELECTOR)
                href = await link_el.get_attribute("href") if link_el else None
                url = urljoin(self.listing_url, href) if href else self.listing_url
                img_el = await card.query_selector(self.IMAGE_SELECTOR)
                img = await img_el.get_attribute("src") if img_el else None
                raised_el = await card.query_selector(self.RAISED_SELECTOR)
                goal_el = await card.query_selector(self.GOAL_SELECTOR)
                days_el = await card.query_selector(self.DAYS_LEFT_SELECTOR)
                verified_el = await card.query_selector(self.VERIFIED_SELECTOR)
                days_left = parse_days_left(await days_el.inner_text()) if days_el else None

                out.append(Campaign(
                    id=f"impactguru-{i}-{abs(hash(url)) % 10_000_000}",
                    source=self.source_name,
                    title=title,
                    description=title,
                    patient_name=None,
                    condition=None,
                    image_url=img,
                    amount_raised_inr=parse_inr(await raised_el.inner_text()) if raised_el else 0.0,
                    amount_goal_inr=parse_inr(await goal_el.inner_text()) if goal_el else 0.0,
                    deadline_iso=deadline_from_days_left(days_left),
                    days_left=days_left,
                    campaign_url=url,
                    verified=verified_el is not None,
                ))
            except Exception as e:
                print(f"[impactguru] card {i} error: {e}", file=sys.stderr)
                continue
        return out


# ---------------------------------------------------------------------------
# GiveIndia (NGO aggregator — limited fit for individual-urgency model)
# ---------------------------------------------------------------------------
class GiveIndiaScraper(BaseScraper):
    source_name = "giveindia"
    listing_url = "https://www.giveindia.org/fundraisers"

    CARD_SELECTOR = ".fundraiser-card, article"
    TITLE_SELECTOR = "h3, h2"
    URL_SELECTOR = "a"
    IMAGE_SELECTOR = "img"
    RAISED_SELECTOR = ".raised"
    GOAL_SELECTOR = ".goal"
    DAYS_LEFT_SELECTOR = ".days-left"

    async def _wait_for_cards(self, page: Page) -> None:
        await page.wait_for_selector(self.CARD_SELECTOR, timeout=PAGE_TIMEOUT_MS)
        for _ in range(2):
            await page.mouse.wheel(0, 2000)
            await page.wait_for_timeout(500)

    async def _scrape_dom(self, page: Page) -> list[Campaign]:
        # GiveIndia mostly hosts NGO campaigns, often without hard deadlines.
        # Returned with days_left=None unless deadline is found — these will rank last
        # under the days-left primary signal.
        cards = await page.query_selector_all(self.CARD_SELECTOR)
        out: list[Campaign] = []
        for i, card in enumerate(cards[:MAX_CAMPAIGNS_PER_SITE]):
            try:
                title_el = await card.query_selector(self.TITLE_SELECTOR)
                title = (await title_el.inner_text()).strip() if title_el else ""
                if not title:
                    continue
                # Only include if the title/category suggests medical
                if not re.search(r"medical|surgery|cancer|hospital|treatment|patient",
                                 title, re.IGNORECASE):
                    continue
                link_el = await card.query_selector(self.URL_SELECTOR)
                href = await link_el.get_attribute("href") if link_el else None
                url = urljoin(self.listing_url, href) if href else self.listing_url
                img_el = await card.query_selector(self.IMAGE_SELECTOR)
                img = await img_el.get_attribute("src") if img_el else None
                raised_el = await card.query_selector(self.RAISED_SELECTOR)
                goal_el = await card.query_selector(self.GOAL_SELECTOR)
                days_el = await card.query_selector(self.DAYS_LEFT_SELECTOR)
                days_left = parse_days_left(await days_el.inner_text()) if days_el else None

                out.append(Campaign(
                    id=f"giveindia-{i}-{abs(hash(url)) % 10_000_000}",
                    source=self.source_name,
                    title=title,
                    description=title,
                    patient_name=None,
                    condition=None,
                    image_url=img,
                    amount_raised_inr=parse_inr(await raised_el.inner_text()) if raised_el else 0.0,
                    amount_goal_inr=parse_inr(await goal_el.inner_text()) if goal_el else 0.0,
                    deadline_iso=deadline_from_days_left(days_left),
                    days_left=days_left,
                    campaign_url=url,
                    verified=True,  # GiveIndia vets all NGOs
                ))
            except Exception as e:
                print(f"[giveindia] card {i} error: {e}", file=sys.stderr)
                continue
        return out


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------
def rank_campaigns(campaigns: list[Campaign]) -> list[Campaign]:
    """Primary: days_left ascending. Tiebreaker: larger remaining gap first."""
    filtered = []
    for c in campaigns:
        if ONLY_VERIFIED and not c.verified:
            print(f"DEBUG SKIP(unverified)   {c.title[:30]} | days={c.days_left} | verified={c.verified} | goal={c.amount_goal_inr} | pct={c.funded_fraction:.2f}")
            continue
        if c.days_left is not None and c.days_left < 0:
            print(f"DEBUG SKIP(expired)      {c.title[:30]} | days={c.days_left} | verified={c.verified} | goal={c.amount_goal_inr} | pct={c.funded_fraction:.2f}")
            continue
        if c.amount_goal_inr > 0 and c.funded_fraction >= DROP_IF_FUNDED_OVER:
            print(f"DEBUG SKIP(funded)       {c.title[:30]} | days={c.days_left} | verified={c.verified} | goal={c.amount_goal_inr} | pct={c.funded_fraction:.2f}")
            continue
        print(f"DEBUG {c.title[:30]} | days={c.days_left} | verified={c.verified} | goal={c.amount_goal_inr} | pct={c.funded_fraction:.2f}")
        filtered.append(c)

    # Campaigns without days_left sort last
    def sort_key(c: Campaign):
        primary = c.days_left if c.days_left is not None else 10_000
        secondary = -c.funding_gap_inr  # larger gap = more urgent need
        return (primary, secondary)

    return sorted(filtered, key=sort_key)


# ---------------------------------------------------------------------------
# WhatsApp copy generation (Claude API)
# ---------------------------------------------------------------------------
def _fmt_inr(amount: float) -> str:
    """Format INR with Indian conventions (lakh / crore)."""
    if amount >= 10_000_000:
        return f"₹{amount / 10_000_000:.1f} crore"
    if amount >= 100_000:
        return f"₹{amount / 100_000:.1f} lakh"
    if amount >= 1000:
        return f"₹{amount / 1000:.0f}K"
    return f"₹{amount:.0f}"


def _build_copy_prompt(c: Campaign) -> str:
    languages_list = "\n".join(f"  - {code}: {name}" for code, name in LANGUAGES.items())
    days_text = (
        f"{c.days_left} days left"
        if c.days_left is not None
        else "deadline not specified"
    )
    return f"""Write SHORT WhatsApp messages to help a verified medical fundraiser get donations and shares.

CAMPAIGN
Patient/title: {c.title}
Deadline: {days_text}
Raised: {_fmt_inr(c.amount_raised_inr)} of {_fmt_inr(c.amount_goal_inr)} goal
Still needed: {_fmt_inr(c.funding_gap_inr)}
Verified platform: {c.source}
Donation link: {c.campaign_url}

TASK
Write one WhatsApp message in each of these languages:
{languages_list}

REQUIREMENTS
- Maximum 5 lines per message
- Open with the human story — who this person is, not just the medical condition
- Lead with empathy, like a concerned friend sharing news with family
- Mention days remaining and amount still needed — but after the human story
- Use warm, conversational language — not a press release
- Add "Even ₹100 helps" or equivalent in each language
- End with: donate, OR share with 5 people — both options
- Include the donation link on the last line
- Use the native script for each language
- Indian number format (lakh/crore)
- Plain text only — no asterisks, no markdown

OUTPUT FORMAT
Return ONLY a JSON object with language codes as keys and message strings as values.
Example: {{"en": "...", "hi": "...", "kn": "..."}}
No preamble, no code fences, just the JSON object."""


def generate_whatsapp_copy(client, campaign: Campaign) -> dict:
    """Call Claude to generate WhatsApp share copy in the configured languages."""
    try:
        response = client.messages.create(
            model=COPY_MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": _build_copy_prompt(campaign)}],
        )
        text = response.content[0].text.strip()
        # Strip code fences if model added them despite instructions
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        result = json.loads(text)
        # Sanity check: ensure it's a dict of strings
        if isinstance(result, dict) and all(isinstance(v, str) for v in result.values()):
            return result
        return {}
    except Exception as e:
        print(f"[copy-gen] {campaign.id}: {e}", file=sys.stderr)
        return {}


def enrich_with_copy(campaigns: list[Campaign]) -> None:
    """Generate WhatsApp copy for the top N ranked campaigns. Mutates in place."""
    if not GENERATE_COPY:
        return
    if not _ANTHROPIC_AVAILABLE:
        print("[copy-gen] anthropic package not installed; skipping", file=sys.stderr)
        return
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("[copy-gen] ANTHROPIC_API_KEY not set; skipping", file=sys.stderr)
        return

    client = Anthropic()
    targets = campaigns[:MAX_COPY_GEN]
    print(f"[copy-gen] generating copy for {len(targets)} campaigns", file=sys.stderr)
    for i, c in enumerate(targets, 1):
        c.messages = generate_whatsapp_copy(client, c)
        if i % 5 == 0:
            print(f"[copy-gen] {i}/{len(targets)} done", file=sys.stderr)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
SCRAPERS: list[type[BaseScraper]] = [
    KettoScraper,
    MilaapScraper,
    ImpactGuruScraper,
    GiveIndiaScraper,
]


async def run_all() -> dict:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(user_agent=USER_AGENT)

        all_campaigns: list[Campaign] = []
        per_source_counts: dict[str, int] = {}

        for scraper_cls in SCRAPERS:
            scraper = scraper_cls()
            try:
                results = await scraper.scrape(context)
                all_campaigns.extend(results)
                per_source_counts[scraper.source_name] = len(results)
                print(f"[{scraper.source_name}] got {len(results)} campaigns", file=sys.stderr)
            except Exception as e:
                print(f"[{scraper.source_name}] FATAL: {e}", file=sys.stderr)
                per_source_counts[scraper.source_name] = 0

        await browser.close()

    ranked = rank_campaigns(all_campaigns)
    enrich_with_copy(ranked)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": per_source_counts,
        "languages": list(LANGUAGES.keys()),
        "total": len(ranked),
        "campaigns": [asdict(c) for c in ranked],
    }


def main():
    result = asyncio.run(run_all())
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"Wrote {OUTPUT_PATH}: {result['total']} campaigns", file=sys.stderr)


if __name__ == "__main__":
    main()
