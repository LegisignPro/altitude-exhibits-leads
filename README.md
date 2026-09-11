# Altitude Exhibits -- Lead Intelligence & Outreach Dashboard (MVP)

Point it at any convention's exhibitor directory (any city), get back a scored list of
the exhibitors worth a custom build, and a timed 4-touch email sequence for each one
pitching a free 3D LED rendering from Altitude Exhibits.

Two ways a lead qualifies:

- **Goldilocks**: mid-market booth (200-600 sq ft), $15M-$100M revenue, 50-500 staff.
- **Big fish**: a company above the revenue floor ($100M+ by default) in a booth no bigger
  than the Goldilocks ceiling. They are on the floor because they have to be; the flagship
  build comes later, and the sequence pitches the shortlist for that build.

Single file: `app.py`. Stack: Streamlit, pandas, requests, BeautifulSoup.
Every exhibitor, booth number and booth footprint in the app is real, pulled live
from the show's directory. There is no sample or fallback data.

Live: https://altitude-exhibits-leads.streamlit.app (Streamlit Community Cloud, redeploys on push).

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

Opens at http://localhost:8501. Pick **NAB Show 2027** in the target-show dropdown and
click **Scrape & Analyze** -- its 2027 directory is already published.

## Keys (optional, all stored as Streamlit secrets)

| Secret | What it unlocks | Where to get it |
| --- | --- | --- |
| `APOLLO_API_KEY` | Live organisation enrichment + people search (decision-maker name, title, LinkedIn) | Apollo -> Settings -> Integrations -> API -> Create new key, tick **master API key** |
| `TAVILY_API_KEY` | "Find a show's exhibitor directory by name" for any convention, and a web-search fallback for company websites | https://app.tavily.com (free tier: 1,000 credits/month) |

Locally: `.streamlit/secrets.toml`. On Streamlit Cloud: App settings -> Secrets. Either key can
also be pasted into the sidebar for one session.

## Getting the most out of Apollo's Free plan (75 credits a month)

The Free plan blocks company search by name, people enrichment (email reveal) and the
newer `api_search` endpoints, and gives 75 credits a month, one per organisation
enrichment. The app is ordered so the credits go as far as possible:

1. **Domain, 0 credits.** Directory website if published, else Clearbit's public
   autocomplete (no key), else Tavily web search, else Apollo company search (paid plans only).
2. **People search, 0 credits.** `mixed_people/search` is not on Apollo's credit table, and
   it returns the decision-maker's name, title and LinkedIn URL plus the employer record
   (headcount, industry, sometimes revenue) for free.
3. **Organisation enrichment, 1 credit**, only where revenue is still unknown, in priority
   order: booths inside the Goldilocks band first (booth size is the one thing known for
   certain before spending anything), then small booths whose free payload already says the
   company is big (big-fish candidates). Islands and unplaced exhibitors never get a credit.
4. **Per-scrape budget** (sidebar, default 25) caps step 3, so 75 credits = three shows a
   month, or one show at 75. Results are cached on disk for 24h; re-running a scrape or
   clicking "Run Apollo on next N" never re-spends a credit. The sidebar shows credits spent
   since the app last started.
5. **Emails are never revealed through the API.** That is 1 credit per person and the
   `people/match` endpoint is paid-plan only. The sequence goes out with the name, title and
   LinkedIn; the email comes from your sequencer's finder (Apollo's own web app on Free
   includes some reveals) or a verifier.

When the pipeline proves out, Apollo Basic removes the endpoint restrictions and the
credit ceiling; nothing in the code changes.

## Deploy (public URL, free, ~2 minutes)

Streamlit apps need a persistent Python server, so Vercel is not an option.
The equivalent one-click host is **Streamlit Community Cloud**:

1. Push this folder to a GitHub repo (public or private).
2. Go to https://share.streamlit.io -> **New app** -> pick the repo, branch `main`, main file `app.py`.
3. Deploy. You get a `https://<app-name>.streamlit.app` URL to share at the meeting.
4. App settings -> Secrets -> add the keys above.

Backup: the included `Dockerfile` runs anywhere Docker runs (Render, Railway, Fly, a VPS).

## How the scrape works

**MapYourShow** (CES, NAB Show, IMTS, World of Concrete, PACK EXPO, AHR, TISE and most
large shows nationwide) is fully supported. The public directory is a JavaScript
app, so the HTML carries no exhibitor rows; the app talks to the same JSON
endpoints the page uses, which answer to a plain GET with an
`X-Requested-With: XMLHttpRequest` header:

| Call | What it returns |
| --- | --- |
| `/8_0/ajax/remote-proxy.cfm?action=getsearchoptions&function=getBoothHalls` | every hall in the show |
| `/8_0/ajax/remote-proxy.cfm?action=search&searchtype=exhibitorgallery&searchsize=20000` | the complete exhibitor list in one call: name, id, booth numbers, halls, description |
| `/8_0/floorplan/02/_remote-proxy.cfm?showid=NAB27&hallid=C&action=GetBoothByHall` | every booth polygon in the hall with `boothWidth` / `boothHeight` (inches) and `area` (sq ft), plus the exhibitor id holding it |

Joining the gallery and the floor plan on the exhibitor id gives exact booth footprints
for the whole show (halls are pulled in parallel; CES's 37 halls take a few seconds).
Exhibitors with several booths are aggregated (total sq ft, largest booth shown as the
size, `(+n)` for the extras). A pull that comes back without booth geometry is never
cached, so a transient floor-plan hiccup clears on the next click.

**Any other convention**: type the show name into "Find a show's exhibitor directory by
name" (Tavily) and pick the directory it finds; MapYourShow hits come first. Other platforms
(A2Z / Personify, ExpoCAD, custom sites) go through the HTML table and card parsers. If a
page is JavaScript-rendered or blocked, the app says so and stops -- it never substitutes data.

## Modules

| Module | What happens |
| --- | --- |
| Enrichment | Revenue, headcount and industry are modelled for every exhibitor (deterministic per company, booth footprint as the prior, industry keyed off the name and directory description). With an Apollo key the top leads then go through the free-tier-aware Apollo pass above. Contacts are never invented. |
| Scoring | 0-100: booth 40 pts, revenue 30, headcount 30. Full marks inside the band, decaying outside. All three in range = Goldilocks (green row). Big fish (amber row) rank just under perfect matches and are valued at the mid-market floor. Near-miss catches leads that miss one criterion; "No booth data" marks exhibitors not yet placed on the floor plan. |
| Outreach | Target-show calendar (Las Vegas, March-June 2027, the current focus) with the RFP window computed from the show date (prime = 5-9 months out); any other show gets its date and venue typed in. 4-touch sequence per lead: timeline hook + render offer, bump, the math, close the loop. A Las Vegas venue gets the home-turf pitch (local build, no freight, local crew); any other city gets the build-that-travels pitch. Big fish get their own Touch 1. Persona angle changes with the contact's title. |
| Export | Leads CSV, per-email .txt, and a campaign CSV (one row per qualified lead with all four subjects, bodies and send dates) for Apollo Sequences, Instantly, Lemlist or Clay. |

## 60-second demo script

1. Pick **NAB Show 2027** in the target-show dropdown, click **Scrape & Analyze** (about 10 seconds:
   651 real exhibitors, 639 exact booth footprints from the LVCC floor plan).
2. Point at the KPIs: total scraped, qualified (Goldilocks + big fish), pipeline value, outreach window "Open now".
3. Click **Islands** in the sidebar and watch the grid and pipeline re-score live; click **Mid-market** to go back.
4. Click a green row. The expander shows why it qualifies, the hall, the directory description, who to reach
   (name, title, LinkedIn when Apollo is on), the timing, and the four emails. Click an amber row for the big-fish pitch.
5. Click **Export campaign CSV**: that file is what gets loaded into the sequencer.
6. Type any other show into the directory finder ("IMTS 2026") to show it works nationwide.

## Production notes

- Apollo: `enrich_company_apollo()`, `find_contact_apollo()` and `apply_apollo()` document the endpoints, headers, field mapping, credit costs and the free-tier ordering.
- Show dates in `TARGET_SHOWS` came from the 2027 Las Vegas calendar; confirm each on the show's site before a campaign goes out.
- Next platform to add: A2Z / Personify (used by several of the March-June target shows); its exhibitor list is server-rendered ASP.NET, so the generic table parser is the starting point.
