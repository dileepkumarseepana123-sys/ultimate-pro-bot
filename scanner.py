import json, os, re, zipfile, statistics
from pathlib import Path
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ==========================================
# CONFIGURATION
# ==========================================
DATA_DIR = Path("./data")
HISTORY_DIR = DATA_DIR / "cricsheet_all"
CACHE_DIR = DATA_DIR / "cache"
CRICSHEET_URL = "https://cricsheet.org/downloads/all_json.zip"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Ultimate-Pro-Bot/T20-Scanner-V2"})
API_KEY = os.getenv("CRICKETDATA_API_KEY", "").strip()
LOOKAHEAD_DAYS = int(os.getenv("SCANNER_LOOKAHEAD_DAYS", "1"))
MIN_VENUE_MATCHES = int(os.getenv("MIN_VENUE_MATCHES", "5"))
MIN_SPIN_CLASSIFICATION = float(os.getenv("MIN_SPIN_CLASSIFICATION", "0.90"))
XI_LOOKAHEAD_MIN = int(os.getenv("XI_LOOKAHEAD_MIN", "180"))
ALLOW_FANTASY_SQUAD = os.getenv("ALLOW_FANTASY_SQUAD", "0").strip().lower() in {"1", "true", "yes"}
MAX_XI_LOOKUPS_PER_RUN = int(os.getenv("MAX_XI_LOOKUPS_PER_RUN", "0" if not ALLOW_FANTASY_SQUAD else "2"))
MAX_MATCH_PAGES_PER_RUN = int(os.getenv("MAX_MATCH_PAGES_PER_RUN", "8"))
TODAY_ONLY = os.getenv("TODAY_ONLY", "1").strip().lower() not in {"0", "false", "no"}

# PREMIUM LEAGUES & TEAMS
PREMIUM_KEYWORDS = ["indian premier league", "ipl", "big bash league", "bbl", "caribbean premier league", "cpl", "pakistan super league", "psl", "sa20", "t20 blast", "vitality blast", "major league cricket", "mlc", "international t20 league", "ilt20", "bangladesh premier league", "bpl", "super smash"]
MAJOR_TEAMS = ["england", "india", "australia", "sri lanka", "west indies", "south africa", "new zealand", "pakistan", "bangladesh", "afghanistan", "ireland", "zimbabwe"]
JUNK_WORDS = ["test", "tests", "odi", "one day", "t10", "hundred", "100 ball", "women's club", "under-19", "u19", "county championship"]


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm(value):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", clean_text(value).lower())).strip()


def parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except ValueError:
        for fmt in ("%Y-%m-%d", "%d %b %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(str(value), fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    return None


def is_standard_t20(match):
    return infer_match_type(match) in {"T20", "T20I", "TWENTY20"}




def parse_match_name(name):
    text = clean_text(name)
    parts = re.split(r"\s+vs\s+|\s+v\s+|\s+versus\s+", text, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return None, None
    team_a = clean_text(re.split(r",|\s+-\s+", parts[0], maxsplit=1)[0])
    team_b = clean_text(re.split(r",|\s+-\s+", parts[1], maxsplit=1)[0])
    return team_a, team_b


def infer_match_type(match, source_t20=False):
    explicit = norm(match.get("matchType") or match.get("match_type") or match.get("type"))
    if explicit in {"t20", "t20i", "twenty20"}:
        return explicit.upper() if explicit else "T20"
    if explicit in {"odi", "test", "t10"}:
        return explicit.upper()
    text_blob = norm(" ".join(str(match.get(k) or "") for k in ("name", "series_name", "series", "status")))
    if any(x in text_blob for x in ("test", "tests", "one day", "odi", "t10", "hundred", "100 ball")):
        return "NON_T20"
    if source_t20 or "t20" in text_blob or "twenty20" in text_blob:
        return "T20"
    if any(k in text_blob for k in PREMIUM_KEYWORDS):
        return "T20"
    return "UNKNOWN"


def local_today():
    return datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).date()


def in_india_today(dt):
    return dt is not None and dt.astimezone(ZoneInfo("Asia/Kolkata")).date() == local_today()


def date_field_is_india_today(value):
    if not value:
        return False
    txt = clean_text(value)
    variants = [
        txt,
        txt.replace("T", " ").replace("Z", " ").strip(),
    ]
    formats = (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d %b %Y",
        "%b %d, %Y",
        "%a, %b %d %Y",
        "%a, %b %d, %Y",
        "%d/%m/%Y",
        "%m/%d/%Y",
    )
    for item in variants:
        for fmt in formats:
            try:
                return datetime.strptime(item, fmt).date() == local_today()
            except ValueError:
                continue
        # Handle strings that contain an ISO date plus additional metadata.
        m = re.search(r"\\b(20\\d{2}-\\d{2}-\\d{2})\\b", item)
        if m:
            try:
                return datetime.strptime(m.group(1), "%Y-%m-%d").date() == local_today()
            except ValueError:
                pass
    return False


def match_is_today(match):
    # CricketData documents date as the local match date and dateTimeGMT as
    # the UTC start timestamp. Prefer the UTC timestamp, then fall back across
    # common date/datetime field variants without fabricating a date.
    datetime_keys = (
        "dateTimeGMT", "date_time_gmt", "dateTime", "datetime",
        "startDateTime", "start_datetime", "matchDateTime", "match_datetime"
    )
    for key in datetime_keys:
        raw_dt = match.get(key)
        if raw_dt:
            dt = parse_dt(raw_dt)
            if dt is not None:
                return in_india_today(dt), dt

    date_keys = (
        "date", "matchDate", "match_date", "startDate", "start_date",
        "localDate", "local_date"
    )
    for key in date_keys:
        raw_date = match.get(key)
        if date_field_is_india_today(raw_date):
            return True, None

    return False, None

def match_is_today(match):
    raw_dt = match.get("dateTimeGMT") or match.get("date_time_gmt")
    if raw_dt:
        dt = parse_dt(raw_dt)
        if dt is not None:
            return in_india_today(dt), dt
    raw_date = match.get("date")
    if date_field_is_india_today(raw_date):
        return True, None
    return False, None




def premium_market(match):
    name = norm(match.get("name", ""))
    series = norm(match.get("series_name", match.get("series", "")))
    combined = f"{series} {name}"
    if any(k in combined for k in PREMIUM_KEYWORDS):
        return True, "PREMIUM COMPETITION"
    teams = [norm(t) for t in match.get("teams", []) if t]
    major = sum(1 for t in teams if any(t == x or t.startswith(x + " ") or x in t for x in MAJOR_TEAMS))
    if major >= 2 and "women" not in combined and "under 19" not in combined:
        return True, "MAJOR INTERNATIONAL T20I"
    return False, "LOW/UNKNOWN MARKET TIER"


def api_get(endpoint, params=None):
    if not API_KEY:
        return None
    q = {"apikey": API_KEY, "offset": 0}
    q.update(params or {})
    try:
        r = SESSION.get(f"https://api.cricapi.com/v1/{endpoint}", params=q, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        print(f"[WARN] CricketData {endpoint} failed: {exc}")
        return None


def get_cricketdata_matches():
    if not API_KEY:
        return []
    rows_out = []
    offset = 0
    for _ in range(MAX_MATCH_PAGES_PER_RUN):
        data = api_get("matches", {"offset": offset})
        if not isinstance(data, dict) or data.get("status") != "success":
            break
        rows = data.get("data") or []
        if not rows:
            break
        rows_out.extend(rows)
        total = (data.get("info") or {}).get("totalRows")
        if total is None or len(rows_out) >= int(total):
            break
        offset += len(rows)
    return rows_out


def cache_key(prefix, value):
    key = re.sub(r"[^a-z0-9]+", "_", norm(value)).strip("_")[:100]
    return CACHE_DIR / f"{prefix}_{key}.json"


def get_match_squad(match_id):
    if not API_KEY or not match_id:
        return None
    cache = cache_key("squad", match_id)
    try:
        if cache.exists() and (datetime.now().timestamp() - cache.stat().st_mtime) < 6 * 3600:
            return json.loads(cache.read_text(encoding="utf-8"))
    except Exception:
        pass
    data = api_get("match_squad", {"id": match_id})
    if data and data.get("status") == "success":
        cache.write_text(json.dumps(data), encoding="utf-8")
        return data
    return None


def extract_squad(payload):
    groups, styles = [], {}
    if not isinstance(payload, dict):
        return groups, styles
    data = payload.get("data") or []
    if isinstance(data, dict):
        data = [data]
    for group in data:
        team = clean_text(group.get("teamName") or group.get("name") or group.get("shortname") or "Unknown")
        players = []
        for p in group.get("players") or []:
            name = clean_text(p.get("name"))
            style = clean_text(p.get("bowlingStyle"))
            if name:
                players.append({
                    "name": name,
                    "role": clean_text(p.get("role")),
                    "bowlingStyle": style or None,
                    "isPlaying": p.get("isPlaying") if "isPlaying" in p else p.get("playing")
                })
                if style:
                    styles[name] = style
        groups.append({"team": team, "players": players})
    return groups, styles


def confirmed_xi_status(payload):
    groups, _ = extract_squad(payload)
    confirmed, flag_seen = {}, False
    for group in groups:
        names = []
        for p in group["players"]:
            flag = p.get("isPlaying")
            if isinstance(flag, bool):
                flag_seen = True
                if flag:
                    names.append(p["name"])
        if names:
            confirmed[group["team"]] = names
    if flag_seen and confirmed and all(len(v) >= 11 for v in confirmed.values()):
        return {"status": "CONFIRMED", "teams": confirmed, "source": "CricketData match_squad"}
    return {"status": "NOT CONFIRMED", "teams": {}, "source": "CricketData match_squad" if groups else "No squad data"}


def is_spin_style(style):
    s = norm(style)
    return bool(s) and any(k in s for k in ("spin", "orthodox", "off break", "leg break", "googly", "chinaman", "carrom"))


def team_form(history, team, limit=10):
    target = norm(team)
    rows = []
    dated = []
    for m in history:
        teams = m.get("info", {}).get("teams", [])
        if not any(norm(t) == target for t in teams):
            continue
        meta = m.get("info", {})
        date_value = (meta.get("dates") or [None])[0]
        dt = parse_dt(date_value) or datetime.min.replace(tzinfo=timezone.utc)
        winner = norm(meta.get("outcome", {}).get("winner", ""))
        rows.append({
            "date": date_value,
            "winner": meta.get("outcome", {}).get("winner"),
            "won": winner == target if winner else None,
            "venue": meta.get("venue")
        })
        dated.append((dt, rows[-1]))
    dated.sort(key=lambda x: x[0], reverse=True)
    recent = [row for _, row in dated[:limit]]
    wins = sum(1 for r in recent if r["won"] is True)
    losses = sum(1 for r in recent if r["won"] is False)
    return {"matches": len(recent), "wins": wins, "losses": losses, "recent": recent}


def load_spin_bowler_db():
    path = Path("config/spin_bowlers.json")
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {clean_text(k): clean_text(v) for k, v in raw.items() if k and v} if isinstance(raw, dict) else {}
    except Exception as exc:
        print(f"[WARN] Spin database load failed: {exc}")
        return {}


def bowler_style(name, dynamic_styles, static_styles):
    target = norm(name)
    for source in (dynamic_styles, static_styles):
        for key, style in source.items():
            if norm(key) == target:
                return style
    return None


def check_cricbuzz_xi(url):
    if not url:
        return {"status": "NOT CONFIRMED", "source": "No match URL"}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36")
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1200)
            body = page.locator("body").inner_text(timeout=12000).lower()
            browser.close()
            if "playing xi" in body or "playing 11" in body:
                return {"status": "XI PUBLISHED", "source": "Cricbuzz match page"}
    except Exception as exc:
        print(f"[WARN] Cricbuzz XI check failed: {exc}")
    return {"status": "NOT CONFIRMED", "source": "Cricbuzz match page"}

# ==========================================
# OPEN-METEO WEATHER ENGINE
# ==========================================
def geocode_venue(venue):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = re.sub(r"[^a-z0-9]+", "_", str(venue).lower()).strip("_")
    cache = CACHE_DIR / f"geo_{key}.json"
    if cache.exists():
        try: return json.loads(cache.read_text(encoding="utf-8"))
        except: pass
    try:
        r = SESSION.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": venue, "count": 1, "format": "json"}, timeout=10)
        results = r.json().get("results", [])
        if results:
            data = {"lat": results[0]["latitude"], "lon": results[0]["longitude"], "tz": results[0].get("timezone", "auto")}
            cache.write_text(json.dumps(data), encoding="utf-8")
            return data
    except: pass
    return None

def calculate_dew_risk(temp, dp, rh, wind, precip):
    if None in (temp, dp, rh):
        return "UNKNOWN"
    spread = temp - dp
    score = 0
    if rh >= 80: score += 3
    elif rh >= 70: score += 2
    elif rh >= 60: score += 1
    if spread <= 2: score += 3
    elif spread <= 4: score += 2
    elif spread <= 6: score += 1
    if wind is not None and wind <= 8: score += 1
    if precip is not None and precip >= 1.0: score -= 1
    if score >= 6: return "HIGH"
    if score >= 4: return "MEDIUM"
    return "LOW"


def get_match_weather(venue, match_dt):
    if not match_dt:
        return {"status": "UNKNOWN", "reason": "Match time unavailable"}
    geo = geocode_venue(venue)
    if not geo:
        return {"status": "UNKNOWN", "reason": "Venue could not be geocoded"}
    tz_name = geo.get("tz", "UTC")
    try:
        local_dt = match_dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        local_dt = match_dt.astimezone(timezone.utc)
        tz_name = "UTC"
    params = {
        "latitude": geo["lat"], "longitude": geo["lon"], "timezone": tz_name,
        "start_date": local_dt.strftime("%Y-%m-%d"), "end_date": local_dt.strftime("%Y-%m-%d"),
        "forecast_days": 16,
        "hourly": "temperature_2m,relative_humidity_2m,dew_point_2m,precipitation_probability,precipitation,wind_speed_10m"
    }
    try:
        data = SESSION.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=20).json()
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        if not times:
            return {"status": "UNKNOWN", "reason": "No hourly forecast returned"}
        target = local_dt.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
        if target in times:
            idx = times.index(target)
        else:
            parsed = []
            naive_target = local_dt.replace(tzinfo=None)
            for i, t in enumerate(times):
                try:
                    parsed.append((i, datetime.fromisoformat(str(t))))
                except ValueError:
                    pass
            if not parsed:
                return {"status": "UNKNOWN", "reason": "Forecast timestamps unreadable"}
            idx = min(parsed, key=lambda x: abs(x[1] - naive_target))[0]
        def val(k):
            arr = hourly.get(k) or []
            return arr[idx] if idx < len(arr) else None
        temp, rh, dp = val("temperature_2m"), val("relative_humidity_2m"), val("dew_point_2m")
        rainp, rain, wind = val("precipitation_probability"), val("precipitation"), val("wind_speed_10m")
        return {
            "status": "OK", "local_time": local_dt.isoformat(), "temperature_c": temp,
            "humidity_pct": rh, "dew_point_c": dp, "rain_probability_pct": rainp,
            "precipitation_mm": rain, "wind_kmh": wind,
            "dew_risk": calculate_dew_risk(temp, dp, rh, wind, rain),
            "rain_risk": "UNKNOWN" if rainp is None else ("HIGH" if rainp >= 60 else "MEDIUM" if rainp >= 30 else "LOW")
        }
    except Exception as exc:
        return {"status": "UNKNOWN", "reason": f"Weather API error: {exc}"}


# ==========================================
# CRICSHEET HISTORICAL ENGINE
# ==========================================
def ensure_cricsheet_history():
    DATA_DIR.mkdir(exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    marker = HISTORY_DIR / ".ready"
    if marker.exists(): return True
    print("[INFO] Initializing Cricsheet Database...")
    zip_path = DATA_DIR / "all_json.zip"
    r = SESSION.get(CRICSHEET_URL, stream=True, timeout=30)
    with zip_path.open("wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk: f.write(chunk)
    with zipfile.ZipFile(zip_path) as z: z.extractall(HISTORY_DIR)
    marker.write_text(datetime.now(timezone.utc).isoformat())
    zip_path.unlink()
    return True

def load_t20_history():
    history = []
    for p in HISTORY_DIR.glob("*.json"):
        try:
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
                if str(data.get("info", {}).get("match_type", "")).lower() in ["t20", "t20i", "twenty20"]:
                    history.append(data)
        except: continue
    return history

def analyze_venue(history, target_venue, dynamic_styles=None):
    dynamic_styles = dynamic_styles or {}
    static_styles = load_spin_bowler_db()
    target = norm(target_venue)
    matched, best = [], 0.0
    for m in history:
        venue = str(m.get("info", {}).get("venue", ""))
        a, b = target, norm(venue)
        if not a or not b:
            score = 0.0
        elif a == b:
            score = 1.0
        else:
            token_overlap = len(set(a.split()) & set(b.split())) / max(1, len(set(a.split()) | set(b.split())))
            score = max(SequenceMatcher(None, a, b).ratio(), token_overlap * 0.95)
        best = max(best, score)
        if score >= 0.90:
            matched.append(m)
    if len(matched) < MIN_VENUE_MATCHES:
        return {"status": "NO DATA / UNKNOWN VENUE", "matches": len(matched), "venue_confidence": round(best, 3)}

    first_scores, pp_scores = [], []
    chase_wins = completed = 0
    mid_wickets = spinner_wickets = mid_balls = classified_balls = 0

    for m in matched:
        innings = m.get("innings", [])
        if not innings:
            continue
        first_total = pp = 0
        for over in innings[0].get("overs", []):
            ono = int(over.get("over", -1))
            for d in over.get("deliveries", []):
                runs = int(d.get("runs", {}).get("total", 0))
                first_total += runs
                if 0 <= ono <= 5:
                    pp += runs
        first_scores.append(first_total)
        pp_scores.append(pp)

        winner = norm(m.get("info", {}).get("outcome", {}).get("winner", ""))
        if len(innings) >= 2 and winner:
            completed += 1
            if winner == norm(innings[1].get("team", "")):
                chase_wins += 1

        for inn in innings:
            for over in inn.get("overs", []):
                ono = int(over.get("over", -1))
                if not 6 <= ono <= 13:
                    continue
                for d in over.get("deliveries", []):
                    mid_balls += 1
                    style = bowler_style(d.get("bowler", ""), dynamic_styles, static_styles)
                    if style:
                        classified_balls += 1
                    wickets = len(d.get("wickets") or [])
                    mid_wickets += wickets
                    if wickets and style and is_spin_style(style):
                        spinner_wickets += wickets

    classification_pct = round((classified_balls / mid_balls) * 100, 2) if mid_balls else None
    spin_ok = classification_pct is not None and classification_pct >= MIN_SPIN_CLASSIFICATION * 100
    return {
        "status": "OK", "matches": len(matched), "venue_confidence": round(best, 3),
        "avg_first_innings": round(statistics.mean(first_scores), 2) if first_scores else None,
        "powerplay_avg": round(statistics.mean(pp_scores), 2) if pp_scores else None,
        "chasing_win_pct": round((chase_wins / completed) * 100, 2) if completed else None,
        "mid_overs_7_14_wickets": mid_wickets,
        "spin_classification_pct": classification_pct,
        "spinner_7_14_wickets": spinner_wickets if spin_ok else None,
        "spin_wicket_share_pct": round((spinner_wickets / mid_wickets) * 100, 2) if mid_wickets and spin_ok else None,
        "spin_index_status": "OK" if spin_ok else "NO DATA / INCOMPLETE BOWLER CLASSIFICATION"
    }


# ==========================================
# BROWSER AUTOMATION ENGINE (PLAYWRIGHT PURE)
# ==========================================
def _parse_schedule_date(text):
    txt = clean_text(text).upper().replace(",", "")
    patterns = (
        "%a %b %d %Y",
        "%A %B %d %Y",
        "%b %d %Y",
        "%B %d %Y",
        "%d %b %Y",
        "%Y-%m-%d",
    )
    for fmt in patterns:
        try:
            return datetime.strptime(txt.title(), fmt).date()
        except ValueError:
            continue
    return None


def _extract_date_token(text):
    txt = clean_text(text)
    patterns = (
        r"\\b(?:MON|TUE|WED|THU|FRI|SAT|SUN),?\\s+[A-Z]{3,9}\\s+\\d{1,2},?\\s+20\\d{2}\\b",
        r"\\b(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\\s+\\d{1,2},?\\s+20\\d{2}\\b",
        r"\\b20\\d{2}-\\d{2}-\\d{2}\\b",
    )
    for pat in patterns:
        m = re.search(pat, txt, re.I)
        if m:
            dt = _parse_schedule_date(m.group(0))
            if dt:
                return dt, m.group(0)
    return None, None


def _is_t20_series_name(series_name):
    s = norm(series_name)
    if not s:
        return False
    return (
        "t20" in s
        or "twenty20" in s
        or "t20i" in s
        or "asian games" in s
        or any(k in s for k in ("premier league", "premier league", "super league", "big bash", "cpl"))
    )


def _parse_cricbuzz_match_line(line, series_name):
    lower = line.lower()
    if " vs " not in lower or "test" in lower or "odi" in lower or "t10" in lower or "hundred" in lower:
        return None

    # A detailed Cricbuzz line normally puts the match stage after the team
    # names. Split before that stage so commas inside a team name are preserved.
    stage_re = re.compile(
        r",\\s*(?:\\d+(?:st|nd|rd|th)\\s+)?"
        r"(?:t20i|t20|twenty20|final|semi\\s*final|qualifier|eliminator|"
        r"1st\\s+semi\\s+final|2nd\\s+semi\\s+final|bronze\\s+medal\\s+match|"
        r"\\d+st\\s+match|\\d+nd\\s+match|\\d+rd\\s+match|\\d+th\\s+match)",
        re.I,
    )
    stage_match = stage_re.search(line)
    match_part = line[:stage_match.start()] if stage_match else line

    parts = re.split(r"\\s+vs\\s+|\\s+versus\\s+", match_part, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return None

    team_a = clean_text(parts[0].split("•")[-1])
    team_b = clean_text(parts[1])
    if not team_a or not team_b:
        return None

    t20_line = bool(re.search(r"\\b(?:t20i|t20|twenty20)\\b", line, re.I))
    t20_series = _is_t20_series_name(series_name)
    if not (t20_line or t20_series):
        return None

    # Try to isolate venue from the visible text after the stage.
    venue = "UNKNOWN VENUE"
    if stage_match:
        after = clean_text(line[stage_match.end():])
        if after:
            venue = after
    elif "," in line:
        after = clean_text(line.split(",", 1)[1])
        if after:
            venue = after

    return {
        "id": None,
        "teamA": team_a,
        "teamB": team_b,
        "name": line,
        "matchType": "T20",
        "teams": [team_a, team_b],
        "venue": venue,
        "status": "Scheduled",
        "source_date": local_today().isoformat(),
        "series_name": clean_text(series_name) or "UNKNOWN SERIES",
    }


def scrape_cricbuzz():
    matches_found = []
    target_date = local_today()
    urls = [
        "https://m.cricbuzz.com/cricket-schedule/upcoming-series/all",
        "https://www.cricbuzz.com/cricket-schedule/upcoming-series/all",
    ]

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent="Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/128 Mobile Safari/537.36"
            )
            loaded = False
            for url in urls:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(2500)
                    body_text = page.locator("body").inner_text(timeout=20000)
                    if body_text and ("cricket schedule" in body_text.lower() or "schedule" in body_text.lower()):
                        loaded = True
                        break
                except Exception as exc:
                    print(f"[WARN] Cricbuzz page load failed for {url}: {exc}")
            if not loaded:
                browser.close()
                return matches_found

            lines = [clean_text(x) for x in body_text.splitlines() if clean_text(x)]

            # Find the first explicit occurrence of today's calendar date, then
            # scan only that date section. This avoids relying on a single exact
            # heading format such as 'SUN, SEP 20 2026'.
            target_tokens = (
                target_date.strftime("%b %d %Y").upper(),
                target_date.strftime("%b %d, %Y").upper(),
                target_date.strftime("%B %d %Y").upper(),
                target_date.strftime("%B %d, %Y").upper(),
                target_date.isoformat(),
            )

            start_idx = None
            for idx, line in enumerate(lines):
                u = line.upper().replace(",", "")
                if any(token.replace(",", "") in u for token in target_tokens):
                    start_idx = idx
                    break

            if start_idx is None:
                # As a final layout fallback, detect a standalone schedule-date heading.
                for idx, line in enumerate(lines):
                    dt, _ = _extract_date_token(line)
                    if dt == target_date:
                        start_idx = idx
                        break

            if start_idx is None:
                print("[WARN] Cricbuzz today section not found.")
                browser.close()
                return matches_found

            current_series = "UNKNOWN SERIES"
            current_date = target_date

            for line in lines[start_idx:]:
                maybe_date, _ = _extract_date_token(line)
                if maybe_date and maybe_date != target_date:
                    if current_date == target_date:
                        break
                    continue
                if maybe_date == target_date:
                    current_date = target_date
                    continue

                # Series headings appear as standalone text before one or more matches.
                if " vs " not in line.lower():
                    if len(line) < 120 and ("t20" in line.lower() or "twenty20" in line.lower() or "premier league" in line.lower() or "asian games" in line.lower() or "super league" in line.lower()):
                        current_series = line
                    continue

                parsed = _parse_cricbuzz_match_line(line, current_series)
                if parsed:
                    matches_found.append(parsed)

            browser.close()
    except Exception as exc:
        print(f"[WARN] Cricbuzz schedule discovery failed: {exc}")
    return matches_found


# ==========================================
# CORE TERMINAL LOGIC
# ==========================================
def run_scanner():
    DATA_DIR.mkdir(exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not ensure_cricsheet_history():
        print("[ERROR] Cricsheet database unavailable. No fake values will be generated.")
        return

    history = load_t20_history()
    api_rows = get_cricketdata_matches() if API_KEY else []
    cb_rows = scrape_cricbuzz()

    # Discovery diagnostics must be initialized before report generation.
    api_t20_count = sum(
        1 for raw in api_rows
        if infer_match_type(raw) in {"T20", "T20I", "TWENTY20"}
    )
    api_today_count = sum(
        1 for raw in api_rows
        if infer_match_type(raw) in {"T20", "T20I", "TWENTY20"} and match_is_today(raw)[0]
    )
    cb_today_count = sum(
        1 for raw in cb_rows
        if infer_match_type(raw, source_t20=True) in {"T20", "T20I", "TWENTY20"}
        and date_field_is_india_today(raw.get("source_date"))
    )

    candidates = []

    # CricketData provides a broad match list; Cricbuzz T20 schedule is a second,
    # T20-specific discovery source. We merge both rather than using either as a gate.
    for raw in api_rows:
        m = dict(raw)
        t = infer_match_type(m)
        if t in {"T20", "T20I", "TWENTY20"}:
            if not m.get("teams"):
                a, b = parse_match_name(m.get("name"))
                m["teams"] = [a, b] if a and b else []
            candidates.append(m)

    for raw in cb_rows:
        m = dict(raw)
        m["matchType"] = "T20"
        candidates.append(m)

    output, seen = [], set()
    xi_lookups = 0
    today = local_today()

    for raw in candidates:
        m = dict(raw)
        mt = infer_match_type(m, source_t20=(m.get("matchType") == "t20"))
        if mt not in {"T20", "T20I", "TWENTY20"}:
            continue

        teams = m.get("teams") or []
        if len(teams) < 2 or not teams[0] or not teams[1]:
            a, b = parse_match_name(m.get("name"))
            teams = [a, b] if a and b else []
        if len(teams) < 2:
            continue

        is_today, dt = match_is_today(m)
        if TODAY_ONLY and not is_today:
            continue

        team_a, team_b = clean_text(teams[0]), clean_text(teams[1])
        venue = clean_text(m.get("venue")) or "UNKNOWN VENUE"
        date_key = dt.astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat() if dt else str(today)
        key = f"{norm(team_a)}|{norm(team_b)}|{norm(venue)}|{date_key}"
        reverse_key = f"{norm(team_b)}|{norm(team_a)}|{norm(venue)}|{date_key}"
        if key in seen or reverse_key in seen:
            continue
        seen.add(key)

        premium, tier = premium_market({
            "name": m.get("name"),
            "series_name": m.get("series_name") or m.get("series"),
            "teams": [team_a, team_b]
        })

        xi = {"status": "NOT CONFIRMED", "teams": {}, "source": "Not queried"}
        dynamic_styles = {}
        if standard := True:
            if ALLOW_FANTASY_SQUAD and premium and API_KEY and m.get("id") and dt and dt <= datetime.now(timezone.utc) + timedelta(minutes=XI_LOOKAHEAD_MIN) and xi_lookups < MAX_XI_LOOKUPS_PER_RUN:
                squad = get_match_squad(m.get("id"))
                xi_lookups += 1
                _, dynamic_styles = extract_squad(squad)
                xi = confirmed_xi_status(squad)
        # We only mark XI as CONFIRMED when match_squad returns explicit playing flags.
        # A page mention is not enough evidence for a confirmed XI.

        venue_stats = analyze_venue(history, venue, dynamic_styles) if venue != "UNKNOWN VENUE" else {"status": "NO DATA / UNKNOWN VENUE"}
        weather = get_match_weather(venue, dt) if venue != "UNKNOWN VENUE" else {"status": "UNKNOWN", "reason": "No usable venue"}

        form_a, form_b = team_form(history, team_a), team_form(history, team_b)
        reasons, warnings = [], []

        # Liquidity is not directly supplied by CricketData. Premium competition is only a proxy.
        if not premium:
            reasons.append("LIQUIDITY NOT VERIFIED: LOW/UNKNOWN MARKET TIER")
        if venue_stats.get("status") != "OK":
            reasons.append("NO DATA / UNKNOWN VENUE")
        if xi.get("status") != "CONFIRMED":
            reasons.append("PLAYING XI NOT CONFIRMED")
        if weather.get("status") != "OK":
            reasons.append("WEATHER DATA UNKNOWN")
        elif weather.get("dew_risk") == "HIGH":
            reasons.append("HIGH DEW RISK")
        elif weather.get("dew_risk") == "MEDIUM":
            warnings.append("MEDIUM DEW RISK")
        if weather.get("status") == "OK" and weather.get("rain_risk") == "HIGH":
            reasons.append("HIGH RAIN INTERRUPTION RISK")
        if form_a.get("matches", 0) < 5:
            reasons.append(f"LIMITED RECENT T20 DATA FOR {team_a}")
        if form_b.get("matches", 0) < 5:
            reasons.append(f"LIMITED RECENT T20 DATA FOR {team_b}")
        if venue_stats.get("spin_index_status") != "OK":
            warnings.append("SPIN CHOKE INDEX NOT FULLY CLASSIFIED")

        if not reasons:
            verdict = "🟢 DATA CLEAR FOR FURTHER PRICE CHECK"
        elif "PLAYING XI NOT CONFIRMED" in reasons:
            verdict = "🟡 WAIT — KEY DATA PENDING"
        else:
            verdict = "🔴 HIGH-CAUTION / NO-BET"

        weather_text = "NO DATA"
        if weather.get("status") == "OK":
            weather_text = (
                f"{weather.get('temperature_c')}°C | RH {weather.get('humidity_pct')}% | "
                f"Rain {weather.get('rain_probability_pct')}% | Wind {weather.get('wind_kmh')} km/h"
            )

        pp = venue_stats.get("powerplay_avg")
        spin_share = venue_stats.get("spin_wicket_share_pct")
        local_time = dt.astimezone(ZoneInfo("Asia/Kolkata")).isoformat() if dt else "UNKNOWN"

        output.append({
            "teamA": team_a,
            "teamB": team_b,
            "name": m.get("name") or f"{team_a} vs {team_b}",
            "matchType": mt,
            "venue": venue,
            "dateTimeGMT": m.get("dateTimeGMT"),
            "sourceDate": m.get("source_date") or m.get("date"),
            "matchTimeIST": local_time,
            "series": m.get("series_name") or m.get("series") or "UNKNOWN SERIES",
            "marketTier": tier,
            "liquidityProxyOnly": True,
            "premiumMarketProxy": premium,
            "standardT20": True,
            "playingXI": xi,
            "formA": form_a,
            "formB": form_b,
            "weather": weather,
            "weatherText": weather_text,
            "dewRisk": weather.get("dew_risk", "UNKNOWN"),
            "tossBias": f"{venue_stats.get('chasing_win_pct')}% Chasing Wins" if venue_stats.get("chasing_win_pct") is not None else "UNKNOWN",
            "tossTrend": "Historical venue trend",
            "ppScoreAvg": f"{pp:.2f}" if isinstance(pp, (int, float)) else "UNKNOWN",
            "ppRunRate": "Historical first-innings PP",
            "spinIndex": f"{spin_share:.2f}% Spinner Wicket Share (7-14)" if isinstance(spin_share, (int, float)) else "UNKNOWN",
            "spinNote": venue_stats.get("spin_index_status", "UNKNOWN"),
            "venueIntel": venue_stats,
            "midOversWickets": venue_stats.get("mid_overs_7_14_wickets"),
            "spinnerWickets7to14": venue_stats.get("spinner_7_14_wickets"),
            "spinClassificationPct": venue_stats.get("spin_classification_pct"),
            "verdict": verdict,
            "noBetReasons": reasons,
            "warnings": warnings,
            "badgeClass": "badge-green" if verdict.startswith("🟢") else "badge-red" if verdict.startswith("🔴") else "badge-yellow",
            "strategyText": "; ".join(reasons) if reasons else "No critical pre-match data issues from available sources."
        })

    output.sort(key=lambda x: x.get("matchTimeIST", "9999"))
    Path("intel.json").write_text(json.dumps({
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "source": {"primary": "CricketData API" if api_rows else "Cricbuzz fallback", "secondary": "Cricbuzz T20 schedule"},
        "date_filter": {"timezone": "Asia/Kolkata", "today": today.isoformat(), "today_only": TODAY_ONLY, "method": "GMT timestamp when available; local date field or Cricbuzz today section otherwise"},
        "api_budget": {
            "daily_limit_user_reported": 100,
            "max_match_list_calls_per_run": MAX_MATCH_PAGES_PER_RUN,
            "max_squad_calls_per_run": MAX_XI_LOOKUPS_PER_RUN
        },
        "rules": {
            "standard_t20_only": True,
            "show_all_t20_today": True,
            "premium_market_proxy_is_not_liquidity_proof": True,
            "fantasy_squad_default_disabled_on_free_plan": True,
            "no_fake_data": True,
            "min_venue_matches": MIN_VENUE_MATCHES,
            "min_spin_classification": MIN_SPIN_CLASSIFICATION
        },
        "discovery": {
            "cricketdata_rows": len(api_rows),
            "cricketdata_t20_rows": api_t20_count,
            "cricketdata_today_t20_rows": api_today_count,
            "cricbuzz_today_t20_rows": cb_today_count,
            "merged_candidates": len(candidates),
            "final_reports": len(output)
        },
        "summary": {
            "today_t20_count": len(output),
            "no_bet_or_high_caution": sum(1 for x in output if x["verdict"].startswith("🔴")),
            "wait": sum(1 for x in output if x["verdict"].startswith("🟡")),
            "data_clear": sum(1 for x in output if x["verdict"].startswith("🟢"))
        },
        "matches": output
    }, indent=4, ensure_ascii=False), encoding="utf-8")
    print(f"[INFO] Saved {len(output)} T20 fixtures for India date {today.isoformat()}.")


if __name__ == "__main__":
    run_scanner()
