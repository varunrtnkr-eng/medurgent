# MedUrgent

Aggregates urgent medical crowdfunding campaigns from four Indian platforms (Ketto, Milaap, ImpactGuru, GiveIndia), ranks them by days-to-deadline, and publishes a static dashboard. Each campaign card has a **Share** button that opens a WhatsApp-ready message in English, Hindi, and Kannada — generated automatically by the Claude API.

The dashboard URL is your shareable link.

---

## How it fits together

```
GitHub Actions (every 4h)
        │
        ▼
   scraper.py
   ├─ Playwright visits 4 sites
   ├─ Ranks by days-to-deadline
   └─ Claude API writes WhatsApp copy
            │
            ▼
       data.json   ───►   index.html (auto-refreshes every 60s)
                              │
                              ▼
                         GitHub Pages URL
                         (your shareable link)
```

Cost: zero hosting, around ₹50–₹100/month for Claude API tokens.

---

## The phased plan

You're on Windows + VS Code + Claude Code. The pattern below mirrors how MedDiary went: get one thing working end-to-end, then expand.

### Phase 1 — Get the scaffold running locally with a fake `data.json`

Goal: open the dashboard in your browser and see something, before you touch any scrapers.

```powershell
# In the project folder
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

Now create a fake `data.json` just so the dashboard has something to render. Ask Claude Code:

> *Create a `data.json` in this folder with 3 sample campaigns matching the schema in `scraper.py`. Each should have a `messages` dict containing `en`, `hi`, and `kn` WhatsApp messages. Make the campaigns realistic for Indian medical crowdfunding — variety of conditions, different days-left values, different platforms.*

Then serve the dashboard locally:

```powershell
python -m http.server 8000
```

Open http://localhost:8000 — you should see the dashboard with sample cards. Click **Share** on one, switch languages, tap **Send on WhatsApp** to confirm the wa.me link works.

✅ **Phase 1 done when:** dashboard renders, share modal opens, language tabs work, WhatsApp opens with the right text.

---

### Phase 2 — Get **Ketto** working end-to-end

This is the hard phase. We'll get one scraper fully working, then the others are repetition.

**Step 2a — find the selectors.**
1. Open https://www.ketto.org/fundraiser/medical in Chrome.
2. Right-click on a campaign card → **Inspect**.
3. In DevTools, hover over `<div>` elements above the highlighted one until you find the outermost element that contains exactly one campaign card. Right-click that → **Copy** → **Copy outerHTML**.
4. Paste that HTML into a file called `samples/ketto-card.html` (create the `samples/` folder).

**Step 2b — ask Claude Code to tune the selectors.**

In Claude Code, with `scraper.py` and `samples/ketto-card.html` open:

> *Look at `samples/ketto-card.html` — this is one campaign card from ketto.org. Update the `KettoScraper` class in `scraper.py` so the selectors (CARD_SELECTOR, TITLE_SELECTOR, URL_SELECTOR, IMAGE_SELECTOR, RAISED_SELECTOR, GOAL_SELECTOR, DAYS_LEFT_SELECTOR, VERIFIED_SELECTOR) match this HTML structure. Use stable selectors like class names or data attributes rather than position-based ones. Keep the existing code structure.*

**Step 2c — test the scraper.**

```powershell
# Temporarily switch HEADLESS to False at the top of scraper.py to watch the browser
python scraper.py
```

You'll see Chromium open, navigate to Ketto, do the scrolling, then close. Check `data.json` — should contain real Ketto campaigns.

If it's empty or broken, the selectors didn't match. Common issues to ask Claude Code to fix:

> *The scraper found campaign cards but `amount_raised_inr` is 0. Here's a snippet of where the amount appears in the HTML: [paste relevant HTML]. Update `RAISED_SELECTOR` in `KettoScraper` and the parsing if needed.*

> *Ketto loads more campaigns when you scroll. The scraper only sees the first few. Update `_wait_for_cards` in `KettoScraper` to scroll further and wait for new cards to load.*

**Step 2d — try the smarter approach: API interception.**

Most Indian crowdfunding sites are React/Next.js apps that fetch campaign data from internal JSON APIs. DOM scraping is fragile; API scraping is much more reliable.

In Chrome DevTools on the Ketto listing page: **Network** tab → filter by **Fetch/XHR** → reload page. Look for a response that contains the campaign list as JSON.

If you find one, copy the URL and a sample response. Then ask Claude Code:

> *I found Ketto's internal API: it returns JSON at `[URL]` and looks like this: [paste response]. Implement `_try_api_scrape` in `KettoScraper` to call this endpoint directly and parse out the campaign fields. This will be much more reliable than DOM scraping. The base class will automatically prefer this over the DOM fallback if it returns results.*

✅ **Phase 2 done when:** `python scraper.py` produces a `data.json` with at least 10 real Ketto campaigns with non-zero amounts and accurate days-left.

---

### Phase 3 — Add Hindi and Kannada copy

**Step 3a — get a Claude API key.**

1. Go to https://console.anthropic.com/
2. Sign up / sign in
3. Settings → API Keys → Create Key
4. Copy the key (starts with `sk-ant-...`). You only see it once.
5. Add a small amount of credit ($5 is more than enough for testing)

**Step 3b — set the key locally and re-run.**

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
python scraper.py
```

You'll now see lines like `[copy-gen] 5/20 done` as it works through campaigns. Each one takes 2-3 seconds. When done, open `data.json` and check that campaigns have a `messages` field with `en`, `hi`, and `kn` keys.

**Step 3c — verify in the dashboard.**

Reload http://localhost:8000 — Share buttons should now be active. Click one, switch tabs, send to WhatsApp.

If the Hindi or Kannada copy reads badly (e.g. too formal, awkward phrasing), iterate on the prompt:

> *In `scraper.py`, the function `_build_copy_prompt` controls how Claude generates WhatsApp messages. The Hindi output sounds too formal for WhatsApp. Update the prompt to specify casual, spoken-style Hindi that an Indian uncle/aunty would naturally use. Same for Kannada.*

✅ **Phase 3 done when:** Share modal shows three readable, native-script messages per campaign, and tapping Send on WhatsApp opens the app pre-filled.

---

### Phase 4 — Add the other three sites

Repeat Phase 2 for each:

- **Milaap**: https://milaap.org/fundraisers/medical → `MilaapScraper`
- **ImpactGuru**: https://www.impactguru.com/medical-crowdfunding → `ImpactGuruScraper`
- **GiveIndia**: https://www.giveindia.org/fundraisers → `GiveIndiaScraper` *(weakest fit — see note below)*

Same playbook each time: paste a card's HTML to Claude Code, have it update the selectors, run, iterate.

> **GiveIndia note:** It's an NGO aggregator, most listings don't have hard deadlines, and many aren't individual medical cases. The scraper filters listings by medical-keyword in the title. You'll likely get fewer results here than from the other three. That's fine — Ketto and Milaap will carry most of the dashboard.

✅ **Phase 4 done when:** `data.json` has campaigns from at least 3 of the 4 sites.

---

### Phase 5 — Deploy to GitHub + Pages

```powershell
git init
git add .
git commit -m "Initial commit"
gh repo create medurgent --public --source=. --push
```

Then in the GitHub repo settings (in the browser):

1. **Settings → Pages**: Source = `main` branch, folder = `/ (root)`. Save.
2. **Settings → Actions → General**: under "Workflow permissions" pick **Read and write permissions**. Save.
3. **Settings → Secrets and variables → Actions → New repository secret**: Name `ANTHROPIC_API_KEY`, value = your key. Save.

Test the workflow:

1. **Actions** tab → **scrape** workflow → **Run workflow** → **Run workflow** button.
2. Watch it run. If it succeeds, `data.json` gets committed back to the repo automatically.
3. Visit `https://<your-username>.github.io/medurgent/` — your dashboard, live.

Share this URL anywhere — WhatsApp, Twitter, anywhere.

✅ **Phase 5 done when:** the GitHub Pages URL shows real campaigns, the workflow runs successfully every 4 hours, and clicking Share works from anyone's phone.

---

## Files in this project

| File | What it does |
|------|--------------|
| `scraper.py` | All four scrapers + ranking + Claude API copy generation |
| `index.html` | Self-contained dashboard with share modal |
| `requirements.txt` | Python dependencies |
| `.github/workflows/scrape.yml` | Cron workflow that runs scraper every 4 hours |
| `data.json` | Generated by scraper, served to dashboard. Do not edit by hand. |
| `samples/` | Folder for HTML snippets while tuning selectors (gitignored). |

## Common gotchas (from previous Windows + Playwright work)

- **PowerShell execution policy**: if `Activate.ps1` is blocked, run PowerShell as admin once and execute `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
- **Setting API key**: `$env:ANTHROPIC_API_KEY = "..."` only lasts for the current PowerShell session. For permanent, use `[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-...", "User")` then restart the terminal.
- **Playwright browsers**: `playwright install chromium` downloads a separate browser binary (~150MB). It's a one-time install.

## Adjusting later

- Add more languages: edit `LANGUAGES` dict at the top of `scraper.py`. The dashboard auto-picks them up from `data.json`.
- Change urgency thresholds (critical/urgent/soon/calm): edit `urgencyClass` in `index.html`.
- Change refresh schedule: edit cron in `.github/workflows/scrape.yml` (`0 */4 * * *` = every 4 hours).
- Cost control: lower `MAX_COPY_GEN` in `scraper.py` if API costs climb.
