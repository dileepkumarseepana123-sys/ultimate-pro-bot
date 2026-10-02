# T20 Equal-Profit Scanner

Strict standard T20/T20I only. The Hundred, T10, ODI and Test formats are rejected.

## Primary match discovery

- **CricketData.org `cricScore`** is the primary fixture/live feed. It covers recent, live and upcoming matches around the current date.
- **CricketData `match_info`** enriches each nearby match ID with match type, clean teams, venue and GMT start time. Those stable fields are persisted in `cricketdata_match_cache.json`, so later scans reuse them instead of spending another API hit.
- **CricketData Match List** is a bounded fallback enrichment pass when a nearby ID is still unresolved.
- **Cricbuzz text relay** is supplemental/fallback only. It may fill missing venue/time/status but must never overwrite clean CricketData team identity.
- Missing fields remain **UNKNOWN**. No dummy/default cricket data is permitted.

## Metric sources

- **🌤️ Weather & Dew:** Open-Meteo forecast at the local match hour. Dew risk is derived from temperature, dew point, humidity, wind and precipitation.
- **🪙 Toss Bias:** Cricsheet historical standard-T20 venue sample, expressed as chasing-win percentage.
- **⚡ Powerplay Baseline:** Cricsheet first-innings runs in overs 1-6 at the matched venue.
- **🌀 Spin Choke Index:** Cricsheet wickets in overs 7-14. Spinner wicket share is shown only when bowler-style classification meets the configured minimum (default 90%); otherwise it stays UNKNOWN.
- **Competitive Balance / Skill Gap:** Cricsheet-derived Elo + recent form + head-to-head.
- **Swing Score:** Pre-match two-way movement suitability only; it is not a probability of profit.

## Odds / equal-profit execution

CricketData.org does **not** provide betting odds. Actual BACK/LAY prices must come from the user's exchange/app.

For an initial BACK stake **B** at odds **O** and later LAY price **L**:

- Hedge LAY stake = `B × O / L`
- Equalized gross profit = `B × (O / L − 1)`
- 100% gross profit relative to the initial BACK stake requires `L = O / 2`

The dashboard calculator separately handles cent rounding, liability and optional commission.

## API budget

The scanner records endpoint calls in `intel.json`. Normal discovery uses one `cricScore` call, cached `match_info` metadata, and only then a bounded Match List fallback. New `match_info` lookups are capped per run. Fantasy squad lookups remain disabled by default to protect quota.

## Pre-match archive

Scheduled reports are archived so the original pre-match analysis remains visible after a match starts. Old noisy aliases are similarity-deduplicated when a clean CricketData fixture is available.

## Secret

Create GitHub Actions repository secret:

`CRICKETDATA_API_KEY`

Never put the API key in source code, JSON, HTML, README or commits.

## Local

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
python scanner.py
```
