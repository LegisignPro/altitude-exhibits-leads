# Altitude Exhibits -- Lead Intelligence & Outreach Dashboard (MVP)

Paste a trade-show exhibitor directory URL, get back a scored list of mid-market
exhibitors ("Goldilocks" booths: 200-600 sq ft, $15M-$100M revenue, 50-500 staff),
and a timed 4-touch email sequence for each one pitching a free 3D LED rendering
from Altitude Exhibits.

Single file: `app.py`. Stack: Streamlit, pandas, requests, BeautifulSoup.
Every exhibitor, booth number and booth footprint in the app is real, pulled live
from the show's directory. There is no sample or fallback data.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

Opens at http://localhost:8501. Pick **NAB Show 2027** in the target-show dropdown and
click **Scrape & Analyze** -- its 2027 directory is already published.

## Deploy (public URL, free, ~2 minutes)

Streamlit apps need a persistent Python server, so Vercel is not an option.
The equivalent one-click host is **Streamlit Community Cloud**:

1. Push this folder to a GitHub repo (public or private).
2. Go to https://share.streamlit.io -> **New app** -> pick the repo, branch `main`, main file `app.py`.
3. Deploy. You get a `https://<app-name>.streamlit.app` URL to share at the meeting.
4. Optional: App settings -> Secrets -> add `APOLLO_API_KEY = "..."` to switch enrichment from simulated to live.

Backup: the included `Dockerfile` runs anywhere Docker runs (Render, Railway, Fly, a VPS).

## How the scrape works

**MapYourShow** (CES, NAB Show, World of Concrete, PACK EXPO, AHR, TISE and most
large Las Vegas shows) is fully supported. The public directory is a JavaScript
app, so the HTML carries no exhibitor rows; the app talks to the same JSON
endpoints the page uses, which answer to a plain GET with an
`X-Requested-With: XMLHttpRequest` header and no cookies:

| Call | What it returns |
| --- | --- |
| `/8_0/ajax/remote-proxy.cfm?action=getsearchoptions&function=getBoothHalls` | every hall in the show |
| `/8_0/ajax/remote-proxy.cfm?action=search&searchtype=exhibitorgallery&searchsize=20000` | the complete exhibitor list in one call: name, id, booth numbers, halls, description |
| `/8_0/floorplan/02/_remote-proxy.cfm?showid=NAB27&hallid=C&action=GetBoothByHall` | every booth polygon in the hall with `boothWidth` / `boothHeight` (inches) and `area` (sq ft), plus the exhibitor id holding it |

Joining the gallery and the floor plan on the exhibitor id gives exact booth footprints
for the whole show (halls are pulled in parallel; CES's 37 halls take a few seconds).
Exhibitors with several booths are aggregated (total sq ft, largest booth shown as the
size, `(+n)` for the extras). Websites come from each exhibitor's detail page
(`websiteValue` is embedded in the server-rendered HTML) and are fetched for the
top-scoring leads after a scrape, with a button to fetch more.

**Other platforms** (A2Z / Personify, ExpoCAD, custom sites): the app parses plain
HTML tables and card grids for company, booth number, dimensions and website. If a page
is JavaScript-rendered or blocked, the app says so and stops -- it never substitutes data.

## Modules

| Module | What happens |
| --- | --- |
| Enrichment | Revenue, headcount and industry are modelled (deterministic per company, booth footprint as the prior, industry keyed off the name and directory description) until an Apollo key is added; then `enrich_company_apollo()` and `find_contact_apollo()` do live organisation enrich + people search for the target titles. Contacts are never invented. |
| Scoring | 0-100: booth 40 pts, revenue 30, headcount 30. Full marks inside the band, decaying outside. All three in range = Goldilocks (green row). Near-miss tier catches leads that miss one criterion; "No booth data" marks exhibitors not yet placed on the floor plan. |
| Outreach | Target-show calendar (Las Vegas, March-June 2027) with the RFP window computed from the show date (prime = 5-9 months out). 4-touch sequence per lead: timeline hook + render offer, bump, the Las Vegas math (freight, drayage, local crew), close the loop. Persona angle changes with the contact's title. |
| Export | Leads CSV, per-email .txt, and a campaign CSV (one row per Goldilocks lead with all four subjects, bodies and send dates) for Apollo Sequences, Instantly, Lemlist or Clay. |

## 60-second demo script

1. Pick **NAB Show 2027** in the target-show dropdown, click **Scrape & Analyze** (about 10 seconds:
   651 real exhibitors, exact booth footprints from the LVCC floor plan).
2. Point at the KPIs: total scraped, Goldilocks qualified, pipeline value, outreach window "Open now".
3. Click **Islands** in the sidebar and watch the grid and pipeline re-score live; click **Mid-market** to go back.
4. Click a green row. The expander shows why it qualifies, the hall, the directory description, who to reach,
   the timing, and the four emails.
5. Click **Export campaign CSV**: that file is what gets loaded into the sequencer.
6. Paste the CES 2026 directory URL for the big-show version (4,190 exhibitors across 37 halls).

Other Las Vegas 2027 directories already live on MapYourShow are listed on the app's start screen
(World of Concrete, TISE, AHR Expo, PACK EXPO, International Roofing Expo).

## Production notes

- Apollo: `enrich_company_apollo()` and `find_contact_apollo()` document the endpoints, headers, field mapping, rate limits and the bulk endpoint for 4,000-exhibitor shows.
- Show dates in `TARGET_SHOWS` came from the 2027 Las Vegas calendar; confirm each on the show's site before a campaign goes out.
- Next platform to add: A2Z / Personify (used by several of the March-June target shows); its exhibitor list is server-rendered ASP.NET, so the generic table parser is the starting point.
