# Altitude Exhibits -- Lead Intelligence & Outreach Dashboard (MVP)

Paste a trade-show exhibitor directory URL, get back a scored list of mid-market
exhibitors ("Goldilocks" booths: 200-600 sq ft, $15M-$100M revenue, 50-500 staff),
and a timed 4-touch email sequence for each one pitching a free 3D LED rendering
from Altitude Exhibits.

Single file: `app.py`. Stack: Streamlit, pandas, requests, BeautifulSoup.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

Opens at http://localhost:8501. Click **Load sample data** to see the full workflow
without a URL (20 NAB-style exhibitors).

## Deploy (public URL, free, ~2 minutes)

Streamlit apps need a persistent Python server, so Vercel is not an option.
The equivalent one-click host is **Streamlit Community Cloud**:

1. Push this folder to a GitHub repo (public or private).
2. Go to https://share.streamlit.io -> **New app** -> pick the repo, branch `main`, main file `app.py`.
3. Deploy. You get a `https://<app-name>.streamlit.app` URL to share at the meeting.
4. Optional: App settings -> Secrets -> add `APOLLO_API_KEY = "..."` to switch enrichment from simulated to live.

Backup: the included `Dockerfile` runs anywhere Docker runs (Render, Railway, Fly, a VPS).

## What it does

| Module | What happens |
| --- | --- |
| Scraper | `requests` + BeautifulSoup against MapYourShow, A2Z Events, or any HTML directory. Parsers for card layouts, tables, and generic "exhibitor" markup; keeps whichever finds the most rows. Booth dimensions are parsed when published, otherwise estimated (flagged in the data). |
| Fallback | Any block (403, Cloudflare, JS-rendered list, timeout) loads a 20-company sample with identical columns, so the demo never dies. The badge at the top says which one you are looking at. |
| Enrichment | Simulated Apollo/Clearbit call adds revenue, headcount, industry, and the decision-maker titles to go after. Paste an Apollo key (sidebar or secrets) and it becomes a real organisation-enrich + people-search call, one company at a time. Live rows never get invented contacts. |
| Scoring | 0-100: booth 40 pts, revenue 30, headcount 30. Full marks inside the band, decaying outside. All three in range = Goldilocks (green row). Near-miss tier catches leads that miss one criterion. |
| Outreach | Target-show calendar (Las Vegas, March-June 2027) with the RFP window computed from the show date (prime = 5-9 months out). 4-touch sequence per lead: timeline hook + render offer, bump, the Las Vegas math (freight, drayage, local crew), close the loop. Persona angle changes with the contact's title. |
| Export | Leads CSV, per-email .txt, and a campaign CSV (one row per Goldilocks lead with all four subjects, bodies and send dates) for Apollo Sequences, Instantly, Lemlist or Clay. |

## 60-second demo script

1. Pick **NAB Show 2027** in the target-show dropdown, click **Load sample data**.
2. Point at the KPIs: 20 scraped, 9 qualified, pipeline value, outreach window "Open now".
3. Drag the booth slider to 400+ (or click **Islands** in the sidebar) and watch the grid and pipeline re-score live.
4. Click a green row. The expander shows why it qualifies, who to reach, the timing, and the four emails.
5. Click **Export campaign CSV**: that file is what gets loaded into the sequencer.
6. Paste a real directory URL and click **Scrape & Analyze** to show the live path (and the graceful fallback if the site blocks it).

## Production notes

- Booth geometry: MapYourShow and A2Z both expose booth width/depth through their floor-plan endpoints; wire that in and the size estimate goes away (`estimate_booth_size` in `app.py`).
- Apollo: `enrich_company_apollo()` and `find_contact_apollo()` document the endpoints, headers, field mapping, rate limits and the bulk endpoint for 2,000-exhibitor shows.
- Show dates in `TARGET_SHOWS` came from the 2027 Las Vegas calendar; confirm each on the show's site before a campaign goes out.
