# T20 Pre-Match Scanner

Strict standard T20/T20I only. The Hundred, T10, ODI and Test formats are rejected.

## Data rules

- Cricsheet: historical ball-by-ball venue metrics.
- CricketData/CricAPI: optional upcoming matches and match_squad.
- Open-Meteo: match-time weather/dew.
- Cricbuzz: fallback fixture/XI-published check.
- Missing data stays UNKNOWN/NO DATA.

## Spin Choke

Overs 7-14 are treated as overs 7 through 14. Spinner wickets are counted only when the bowler's style is known. If at least 90% of those balls have a classified bowler style, the spinner wicket share is shown; otherwise it stays UNKNOWN.

## Secret

Create GitHub Actions repository secret:

CRICKETDATA_API_KEY

Never put the API key in source code, JSON, HTML, README or commits.

## Local

python -m pip install -r requirements.txt
python -m playwright install chromium
python scanner.py
