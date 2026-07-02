# Google Trends Playwright Scraper

A **free**, Playwright-powered Google Trends scraper for SEO research and topic monitoring.

It is designed for people who want richer Google Trends data than a simple “rising / not rising” signal, while avoiding paid Trends scrapers for low-volume, slow ideation workflows. It leverages Playwright to control a real Chrome/Edge browser profile and collect public Google Trends signals slowly and safely.

## Practical usage

Use it to:

- test and compare SEO/content ideas before investing in production
- estimate market interest directionally with Google Trends index data
- discover rising trends and related queries
- find promising keywords worldwide or regionally
- compare which countries or regions search for which keywords
- inspect short-term and long-term demand windows: daily, weekly/hebdo, monthly, yearly, or multi-year
- time seasonal campaigns and content refreshes
- monitor a slow keyword radar with cron jobs

## What it captures

For each query, market, and timeframe, the scraper normalizes:

| Signal | Included |
|---|---:|
| Interest over time | yes |
| Latest / mean / max / delta summary | yes |
| Interest by region | yes |
| Related queries | yes |
| Timeframe selection | yes |
| Country / language selection | yes |
| Explicit errors and warnings | yes |
| Search volume / CPC / keyword difficulty | no — validate elsewhere |

Google Trends values are normalized index values, **not search volume**. Use this for ideation, seasonality, and timing. Validate important opportunities with Search Console, DataForSEO, Google Ads, or SERP research before making business decisions.

## Why this uses a Windows browser profile

Google Trends often rate-limits clean headless browser sessions, WSL sessions, and unofficial clients. The most reliable setup we tested is:

```text
WSL / automation / cron
  → PowerShell
  → Windows Playwright
  → persistent visible Chrome or Edge profile
  → Google Trends JSON
```

That gives automation access to the same kind of browser environment that works manually. If Google asks you to log in, log in once in the dedicated browser profile; cookies stay local on your machine.

## Requirements

- Windows 10/11 with Chrome or Edge
- Node.js 20+
- npm
- Python 3.10+ for the rotating cron wrapper/tests
- Optional: WSL/Hermes if you want automated cron delivery

## Install

```bash
git clone https://github.com/judicael-s/Google-trends-scraper.git
cd Google-trends-scraper
npm install
```

If Playwright browsers are not installed yet:

```bash
npx playwright install chromium
```

## Quick offline test

This does not call Google. It validates the output contract using a fixture.

```bash
npm run test:fixture
npm test
```

Expected: JSON with `rows`, `interest_over_time`, `interest_by_region`, and `related_queries`.

## Manual profile warmup

Use this first if Google Trends works in your normal browser but automation gets 429.

From WSL/bash:

```bash
powershell.exe -NoProfile -ExecutionPolicy Bypass \
  -File "$(wslpath -w ./open-trends-profile.ps1)" \
  -Query "cahier de vacances maths" \
  -Geo FR \
  -Hl fr-FR \
  -Timeframe "today 12-m"
```

A Windows Chrome window opens. In that window:

1. Log in to Google if requested.
2. Confirm Google Trends loads.
3. Search/inspect the query manually once.
4. Close the window before running automated collection with the same profile.

## Run one live scrape

From WSL/bash:

```bash
powershell.exe -NoProfile -ExecutionPolicy Bypass \
  -File "$(wslpath -w ./run-trends.ps1)" \
  -Query "cahier de vacances maths" \
  -Geo FR \
  -Hl fr-FR \
  -Timeframe "today 12-m" \
  -RegionResolution REGION
```

PowerShell-native equivalent:

```powershell
.\run-trends.ps1 -Query "cahier de vacances maths" -Geo FR -Hl fr-FR -Timeframe "today 12-m" -RegionResolution REGION
```

## Output example

```json
{
  "connector": "google_trends_playwright_windows",
  "mode": "windows-live",
  "params": {
    "queries": ["cahier de vacances maths"],
    "geo": "FR",
    "hl": "fr-FR",
    "timeframe": "today 12-m",
    "region_resolution": "REGION"
  },
  "rows": [
    {
      "interest_over_time": [],
      "interest_by_region": [],
      "related_queries": [],
      "summary": {
        "points": 53,
        "latest_value": 97,
        "mean_value": 10.98,
        "max_value": 100,
        "trend_delta": -3,
        "region_count": 22,
        "related_query_count": 6
      },
      "validation_status": "trends_ideation_only"
    }
  ],
  "warnings": [],
  "errors": []
}
```

## Supported options

| Option | Example | Notes |
|---|---|---|
| `-Query` / `--query` | `cahier de vacances maths` | One query per run is safest |
| `-Geo` / `--geo` | `FR` | Google Trends geo code |
| `-Hl` / `--hl` | `fr-FR` | Interface language |
| `-Timeframe` / `--timeframe` | `today 12-m` | Also supports `now 1-d`, `now 7-d`, `today 1-m`, `today 3-m`, `today 5-y`, `all` |
| `-RegionResolution` | `REGION` | `COUNTRY`, `REGION`, or `CITY` |
| `-BrowserChannel` | `chrome` | `chrome` or `msedge` |
| `-UserDataDir` | temp profile path | Persistent browser profile; keep per user/client |

## Rotating cron mode

Use the rotating wrapper when you want a slow radar over multiple seed queries.

1. Copy the example config:

```bash
cp examples/client-trends-radar.config.json ./my-client.config.json
```

2. Edit queries, market, and cadence.

3. Run one tick:

```bash
python rotating_trends_cron.py \
  --config ./my-client.config.json \
  --state ./.google-trends-cache/my-client-state.json \
  --output-dir ./.google-trends-cache/raw
```

The wrapper chooses one query whose cooldown expired, stores raw JSON, and prints a concise summary. If all queries were checked recently, it stays silent.

### Recommended cadence

- One query per tick.
- Every 8h is a safe starting point: about 3 requests/day.
- Avoid repeated identical query/geo/timeframe requests.
- Use broad anchors before long-tail article titles.

## Hermes cron example

```bash
hermes cron create \
  --name "Client Google Trends radar" \
  --schedule "every 8h" \
  --script client_google_trends_radar.sh \
  --no-agent
```

Example script body:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd /path/to/Google-trends-scraper
python rotating_trends_cron.py \
  --config /path/to/client.config.json \
  --state /path/to/state.json \
  --output-dir /path/to/raw
```

## Error handling

| Code | Meaning | What to do |
|---|---|---|
| `GOOGLE_TRENDS_RATE_LIMITED` | Google returned 429 | Cool down, reduce cadence, warm/login profile manually |
| `NO_TIMELINE_DATA` | No time-series captured | Could be low volume, UI change, or partial capture; compare a broader anchor |
| `WINDOWS_BROWSER_LAUNCH_FAILED` | Profile in use or browser issue | Close Chrome using same profile, check Chrome/Edge path |
| `RUNNER_STDOUT_PARSE_FAILED` | Wrapper could not parse output | Inspect stdout/stderr and rerun fixture test |

Never interpret rate-limit or parser errors as “zero demand”.

## Good SEO workflow

1. Start with broad market/seasonal anchors.
2. Compare narrower cluster terms.
3. Save raw JSON for future learning.
4. Convert only strong signals into content hypotheses.
5. Validate with GSC / DataForSEO / Google Ads / SERP evidence.
6. Track actions as experiments.

## Privacy and safety

- Do not commit browser profiles, cookies, API keys, or secrets.
- Do not copy profiles between clients or machines.
- Keep collection slow and human-scale.
- This repository is for read-only public Google Trends collection.

## License

MIT
