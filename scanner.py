import json, os, re, zipfile, statistics
from pathlib import Path
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo
import requests
from curl_cffi import requests as curl_requests
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
PAUSE_UNTIL_IST = os.getenv("PAUSE_UNTIL_IST", "").strip()

# PREMIUM LEAGUES & TEAMS
PREMIUM_KEYWORDS = ["indian premier league", "ipl", "big bash league", "bbl", "caribbean premier league", "cpl", "pakistan super league", "psl", "sa20", "t20 blast", "vitality blast", "major league cricket", "mlc", "international t20 league", "ilt20", "bangladesh premier league", "bpl", "super smash"]
MAJOR_TEAMS = ["england", "india", "australia", "sri lanka", "west indies", "south africa", "new zealand", "pakistan", "bangladesh", "afghanistan", "ireland", "zimbabwe"]
JUNK_WORDS = ["test", "tests", "odi", "one day", "t10", "hundred", "100 ball", "women's club", "under-19", "u19", "county championship"]

API_USAGE = {"hitsToday": None, "hitsLimit": None, "quota_exhausted": False, "last_error": None}
PREMATCH_ARCHIVE_PATH = Path("pre_match_archive.json")


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm(value):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", clean_text(value).lower())).strip()


def has_non_t20_format_marker(value):
    text = norm(value)
    return bool(
        re.search(r"\btests?\b", text)
        or re.search(r"\bodi\b", text)
        or re.search(r"\bone day\b", text)
        or re.search(r"\bt10\b", text)
        or re.search(r"\bhundred\b", text)
        or re.search(r"\b100 ball\b", text)
    )


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
    if has_non_t20_format_marker(text_blob):
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
        m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", item)
        if m:
            try:
                return datetime.strptime(m.group(1), "%Y-%m-%d").date() == local_today()
            except ValueError:
                pass
    return False


def _parse_embedded_gmt_date(value):
    text = clean_text(value)
    # CricketData status commonly contains:
    # "Match starts at Sep 20, 01:00 GMT"
    patterns = (
        r"(?:match\s+starts\s+at\s+)?([A-Z]{3,9})\s+(\d{1,2}),?\s+(\d{1,2}):(\d{2})\s*GMT",
        r"(?:match\s+starts\s+at\s+)?([A-Z]{3,9})\s+(\d{1,2}),?\s+(20\d{2})\s+(\d{1,2}):(\d{2})\s*GMT",
    )
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if not m:
            continue
        try:
            groups = m.groups()
            month = groups[0].title()
            day = int(groups[1])
            if len(groups) == 4:
                year = local_today().year
                hour = int(groups[2])
                minute = int(groups[3])
            else:
                year = int(groups[2])
                hour = int(groups[3])
                minute = int(groups[4])
            return datetime.strptime(
                f"{month} {day} {year} {hour:02d}:{minute:02d}",
                "%b %d %Y %H:%M"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def match_is_today(match):
    # Prefer explicit UTC start fields. If unavailable, CricketData's status
    # can contain a GMT start time; parse that before falling back to local date.
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

    for key in ("status", "match_status", "message"):
        embedded = _parse_embedded_gmt_date(match.get(key))
        if embedded is not None:
            return in_india_today(embedded), embedded

    date_keys = (
        "date", "source_date", "matchDate", "match_date", "startDate", "start_date",
        "localDate", "local_date"
    )
    for key in date_keys:
        raw_date = match.get(key)
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




def _sofascore_t20_type(event):
    tournament = event.get("tournament") if isinstance(event.get("tournament"), dict) else {}
    unique = tournament.get("uniqueTournament") if isinstance(tournament.get("uniqueTournament"), dict) else {}
    season = event.get("season") if isinstance(event.get("season"), dict) else {}
    round_info = event.get("roundInfo") if isinstance(event.get("roundInfo"), dict) else {}
    text_blob = norm(" ".join([
        clean_text(tournament.get("name")),
        clean_text(unique.get("name")),
        clean_text(season.get("name")),
        clean_text(round_info.get("name")),
        clean_text(event.get("slug")),
    ]))
    if has_non_t20_format_marker(text_blob) or "list a" in text_blob:
        return "NON_T20"
    if "t20i" in text_blob:
        return "T20I"
    if "t20" in text_blob or "twenty20" in text_blob:
        return "T20"
    # Asian Games cricket is played in the T20 format.
    if "asian games" in text_blob:
        return "T20"
    return "UNKNOWN"


def get_sofascore_schedule(target_date):
    """No-key SofaScore cricket date board."""
    url = f"https://www.sofascore.com/api/v1/sport/cricket/scheduled-events/{target_date.isoformat()}"
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://www.sofascore.com/cricket",
        "Origin": "https://www.sofascore.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
    }
    try:
        resp = curl_requests.get(
            url,
            headers=headers,
            impersonate="chrome120",
            timeout=25,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        print(f"[WARN] SofaScore schedule request failed: {exc}")
        return []

    rows_out = []
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        home = event.get("homeTeam") if isinstance(event.get("homeTeam"), dict) else {}
        away = event.get("awayTeam") if isinstance(event.get("awayTeam"), dict) else {}
        team_a = clean_text(home.get("name"))
        team_b = clean_text(away.get("name"))
        if not team_a or not team_b:
            continue

        mt = _sofascore_t20_type(event)
        if mt not in {"T20", "T20I"}:
            continue

        tournament = event.get("tournament") if isinstance(event.get("tournament"), dict) else {}
        unique = tournament.get("uniqueTournament") if isinstance(tournament.get("uniqueTournament"), dict) else {}
        series_name = clean_text(unique.get("name") or tournament.get("name")) or "UNKNOWN SERIES"

        venue_obj = event.get("venue") if isinstance(event.get("venue"), dict) else {}
        city_obj = venue_obj.get("city") if isinstance(venue_obj.get("city"), dict) else {}
        venue_parts = [clean_text(venue_obj.get("name")), clean_text(city_obj.get("name"))]
        venue = ", ".join(x for x in venue_parts if x) or "UNKNOWN VENUE"

        ts = event.get("startTimestamp")
        dt = None
        try:
            if ts is not None:
                dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            dt = None

        status_obj = event.get("status") if isinstance(event.get("status"), dict) else {}
        rows_out.append({
            "id": event.get("id"),
            "name": f"{team_a} vs {team_b}",
            "matchType": mt,
            "status": clean_text(status_obj.get("description") or status_obj.get("type")),
            "dateTimeGMT": dt.isoformat() if dt else None,
            "date": target_date.isoformat(),
            "teams": [team_a, team_b],
            "venue": venue,
            "series_name": series_name,
            "source_name": "SofaScore scheduled",
            "source_date": target_date.isoformat(),
        })
    return rows_out


def _espn_matches_from_payload(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    containers = [
        payload.get("matches"),
        (payload.get("content") or {}).get("matches"),
        ((payload.get("content") or {}).get("matchList") or {}).get("matches"),
        (payload.get("matchList") or {}).get("matches"),
        (payload.get("data") or {}).get("matches") if isinstance(payload.get("data"), dict) else None,
    ]
    for value in containers:
        if isinstance(value, list):
            return value
    groups = (
        (payload.get("content") or {}).get("matchesByDate")
        or payload.get("matchesByDate")
        or (payload.get("content") or {}).get("groups")
        or payload.get("groups")
    )
    if isinstance(groups, list):
        out = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            items = group.get("matches") or group.get("items") or []
            if isinstance(items, list):
                out.extend(x for x in items if isinstance(x, dict))
        return out
    return []


def _espn_team_name(entry):
    if not isinstance(entry, dict):
        return ""
    team = entry.get("team") if isinstance(entry.get("team"), dict) else entry
    return clean_text(
        team.get("longName")
        or team.get("name")
        or team.get("displayName")
        or team.get("shortName")
    )


def get_espn_schedule(target_date):
    """Date-scoped ESPNcricinfo schedule JSON. No API key and no quota."""
    date_value = target_date.strftime("%d-%m-%Y")
    url = "https://hs-consumer-api.espncricinfo.com/v1/pages/matches/scheduled"
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "Origin": "https://www.espncricinfo.com",
        "Referer": "https://www.espncricinfo.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
    }
    try:
        resp = curl_requests.get(
            url,
            params={"lang": "en", "filterType": "DATE", "filterValue": date_value},
            headers=headers,
            impersonate="chrome120",
            timeout=25,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        print(f"[WARN] ESPNcricinfo impersonated request failed: {exc}")
        try:
            resp = requests.get(
                url,
                params={"lang": "en", "filterType": "DATE", "filterValue": date_value},
                headers=headers,
                timeout=25,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc2:
            print(f"[WARN] ESPNcricinfo schedule JSON failed: {exc2}")
            return []

    rows_out = []
    for item in _espn_matches_from_payload(payload):
        teams_raw = item.get("teams") or []
        teams = [_espn_team_name(x) for x in teams_raw[:2]] if isinstance(teams_raw, list) else []
        teams = [x for x in teams if x]
        if len(teams) < 2:
            continue

        fmt = clean_text(item.get("format") or item.get("matchFormat") or item.get("matchType"))
        series = item.get("series") if isinstance(item.get("series"), dict) else {}
        ground = item.get("ground") if isinstance(item.get("ground"), dict) else {}
        town = ground.get("town") if isinstance(ground.get("town"), dict) else {}
        venue_parts = [clean_text(ground.get("name")), clean_text(town.get("name"))]
        venue = ", ".join(x for x in venue_parts if x) or "UNKNOWN VENUE"

        start = (
            item.get("startTime")
            or item.get("startDate")
            or item.get("date")
            or item.get("dateTimeGMT")
        )
        rows_out.append({
            "id": item.get("id") or item.get("objectId") or item.get("matchId"),
            "name": clean_text(item.get("title")) or f"{teams[0]} vs {teams[1]}",
            "matchType": fmt,
            "status": clean_text(item.get("statusText") or item.get("status") or item.get("state")),
            "dateTimeGMT": start,
            "date": target_date.isoformat(),
            "teams": teams,
            "venue": venue,
            "series_name": clean_text(series.get("longName") or series.get("name")) or "UNKNOWN SERIES",
            "source_name": "ESPNcricinfo scheduled",
            "source_date": target_date.isoformat(),
        })
    return rows_out


def api_get(endpoint, params=None):
    if not API_KEY or API_USAGE.get("quota_exhausted"):
        return None
    q = {"apikey": API_KEY, "offset": 0}
    q.update(params or {})
    try:
        r = SESSION.get(f"https://api.cricapi.com/v1/{endpoint}", params=q, timeout=20)
        r.raise_for_status()
        data = r.json()
        info = data.get("info") if isinstance(data, dict) else None
        if isinstance(info, dict):
            API_USAGE["hitsToday"] = info.get("hitsToday")
            API_USAGE["hitsLimit"] = info.get("hitsLimit")
            try:
                if int(info.get("hitsToday")) >= int(info.get("hitsLimit")):
                    API_USAGE["quota_exhausted"] = True
            except (TypeError, ValueError):
                pass
        if isinstance(data, dict) and data.get("status") != "success":
            API_USAGE["last_error"] = clean_text(data.get("reason") or data.get("message") or data.get("status"))
            if "limit" in norm(API_USAGE["last_error"]) or "quota" in norm(API_USAGE["last_error"]):
                API_USAGE["quota_exhausted"] = True
        return data
    except Exception as exc:
        API_USAGE["last_error"] = str(exc)
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



def strip_team_code(value):
    return clean_text(re.sub(r"\\s*\\[[^\\]]+\\]\\s*$", "", clean_text(value)))


def get_cricscore_matches():
    """Use CricketData's +/-7 day fixture/live/results feed for reliable date discovery."""
    if not API_KEY:
        return []
    data = api_get("cricScore")
    if not isinstance(data, dict) or data.get("status") != "success":
        return []
    rows_out = []
    for raw in data.get("data") or []:
        if not isinstance(raw, dict):
            continue
        team_a = strip_team_code(raw.get("t1"))
        team_b = strip_team_code(raw.get("t2"))
        row = {
            "id": raw.get("id"),
            "name": f"{team_a} vs {team_b}" if team_a and team_b else clean_text(raw.get("name")),
            "matchType": raw.get("matchType"),
            "status": raw.get("status"),
            "dateTimeGMT": raw.get("dateTimeGMT"),
            "teams": [team_a, team_b] if team_a and team_b else [],
            "venue": clean_text(raw.get("venue")) or "UNKNOWN VENUE",
            "source_name": "CricketData cricScore",
            "cricscore_status": raw.get("ms"),
        }
        rows_out.append(row)
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
        r"\b(?:MON|TUE|WED|THU|FRI|SAT|SUN),?\s+[A-Z]{3,9}\s+\d{1,2},?\s+20\d{2}\b",
        r"\b(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\s+\d{1,2},?\s+20\d{2}\b",
        r"\b20\d{2}-\d{2}-\d{2}\b",
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
        r",\s*(?:\d+(?:st|nd|rd|th)\s+)?"
        r"(?:t20i|t20|twenty20|final|semi\s*final|qualifier|eliminator|"
        r"1st\s+semi\s+final|2nd\s+semi\s+final|bronze\s+medal\s+match|"
        r"\d+st\s+match|\d+nd\s+match|\d+rd\s+match|\d+th\s+match)",
        re.I,
    )
    stage_match = stage_re.search(line)
    match_part = line[:stage_match.start()] if stage_match else line

    parts = re.split(r"\s+vs\s+|\s+versus\s+", match_part, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return None

    team_a = clean_text(parts[0].split("•")[-1])
    team_b = clean_text(parts[1])
    if not team_a or not team_b:
        return None

    t20_line = bool(re.search(r"\b(?:t20i|t20|twenty20)\b", line, re.I))
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


def _strip_markdown_text(value):
    text = str(value or "")
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^[#>*\-\s]+", "", text)
    return clean_text(text)


def _parse_cricbuzz_live_title(line, current_series, context=""):
    text = clean_text(line)
    live_marker = re.search(r"\bLIVE:\s*", text, re.I)
    if live_marker:
        text = text[live_marker.end():].strip()
    m = re.search(
        r"(?:LIVE:\s*)?(.+?)\s+vs\s+(.+?)\s*\|\s*"
        r"(Final|Semi[- ]?Final|Qualifier|Eliminator|[^|]+?)\s*\|\s*(.+)$",
        text,
        re.I,
    )
    if not m:
        return None

    team_a = clean_text(m.group(1))
    team_b = clean_text(m.group(2))
    stage = clean_text(m.group(3))
    tail_series = clean_text(m.group(4))
    series_name = (
        clean_text(current_series)
        if current_series and current_series != "UNKNOWN SERIES"
        else tail_series
    )
    if not _is_t20_series_name(series_name) and not _is_t20_series_name(tail_series):
        return None

    venue = "UNKNOWN VENUE"
    ctx = clean_text(context)
    vm = re.search(
        r"(?:Final|Semi[- ]?Final|Qualifier|Eliminator)\s*•\s*"
        r"(.+?)(?:\s+Image\s+\d+:|\s+[A-Z]{2,5}\s+\d)",
        ctx,
        re.I,
    )
    if vm:
        venue = clean_text(vm.group(1))

    return {
        "id": None,
        "teamA": team_a,
        "teamB": team_b,
        "name": f"{team_a} vs {team_b}, {stage}, {series_name}",
        "matchType": "T20",
        "teams": [team_a, team_b],
        "venue": venue,
        "status": "LIVE / CURRENT",
        "source_date": local_today().isoformat(),
        "series_name": series_name or tail_series or "UNKNOWN SERIES",
        "source_name": "Cricbuzz live via text relay",
    }


def scrape_cricbuzz_live_via_text_relay():
    """Discover currently live/recent Cricbuzz matches through the text relay."""
    target_date = local_today()
    relay_urls = [
        "https://r.jina.ai/http://www.cricbuzz.com/cricket-match/live-scores",
        "https://r.jina.ai/https://www.cricbuzz.com/cricket-match/live-scores",
    ]
    rows = []
    for relay_url in relay_urls:
        try:
            r = SESSION.get(
                relay_url,
                headers={
                    "Accept": "text/plain",
                    "User-Agent": "Mozilla/5.0",
                    "x-engine": "browser",
                    "x-no-cache": "true",
                    "x-timeout": "20",
                },
                timeout=40,
            )
            r.raise_for_status()
            lines = [_strip_markdown_text(x) for x in r.text.splitlines()]
            lines = [x for x in lines if x]
            current_series = "UNKNOWN SERIES"
            for i, line in enumerate(lines):
                low = line.lower()
                if " vs " not in low:
                    if (
                        len(line) < 180
                        and (
                            "t20" in low
                            or "twenty20" in low
                            or "premier league" in low
                            or "big bash" in low
                            or "psl" in low
                            or "sa20" in low
                            or "cpl" in low
                            or "blast" in low
                        )
                    ):
                        current_series = line
                    continue

                context = " ".join(lines[max(0, i-3):min(len(lines), i+4)])
                if has_non_t20_format_marker(context):
                    continue

                # The live page also contains highlight/video titles with "vs".
                # Only the actual current-match card carries a LIVE: marker.
                if "live:" not in low:
                    continue

                context = " ".join(lines[max(0, i-4):min(len(lines), i+5)])
                parsed = _parse_cricbuzz_live_title(line, current_series, context)
                if parsed:
                    parsed["source_name"] = "Cricbuzz live via text relay"
                    parsed["source_date"] = target_date.isoformat()
                    parsed["status"] = "LIVE / CURRENT"
                    if parsed.get("series_name") == "UNKNOWN SERIES":
                        parsed["series_name"] = current_series
                    rows.append(parsed)

            if rows:
                print(f"[INFO] Live text relay discovered {len(rows)} current cricket rows.")
                return rows
        except Exception as exc:
            print(f"[WARN] Cricbuzz live text relay failed for {relay_url}: {exc}")
    return rows


def scrape_cricbuzz_via_text_relay():
    """Fetch Cricbuzz through a public text relay to avoid GitHub-runner 403s."""
    target_date = local_today()
    relay_urls = [
        "https://r.jina.ai/http://www.cricbuzz.com/cricket-schedule/upcoming-series/all",
        "https://r.jina.ai/https://www.cricbuzz.com/cricket-schedule/upcoming-series/all",
    ]
    for relay_url in relay_urls:
        try:
            r = SESSION.get(
                relay_url,
                headers={
                    "Accept": "text/plain",
                    "User-Agent": "Mozilla/5.0",
                    "x-engine": "browser",
                    "x-no-cache": "true",
                    "x-timeout": "20",
                },
                timeout=40,
            )
            r.raise_for_status()
            lines = [_strip_markdown_text(x) for x in r.text.splitlines()]
            lines = [x for x in lines if x]

            start_idx = None
            for idx, line in enumerate(lines):
                dt, _ = _extract_date_token(line)
                if dt == target_date:
                    start_idx = idx
                    break
            if start_idx is None:
                continue

            rows = []
            current_series = "UNKNOWN SERIES"
            for line in lines[start_idx:]:
                maybe_date, _ = _extract_date_token(line)
                if maybe_date and maybe_date != target_date:
                    break
                if maybe_date == target_date:
                    continue

                low = line.lower()
                if " vs " not in low:
                    if (
                        len(line) < 180
                        and (
                            "t20" in low
                            or "twenty20" in low
                            or "asian games" in low
                            or "premier league" in low
                            or "super league" in low
                            or "big bash" in low
                            or "cpl" in low
                        )
                    ):
                        current_series = line
                    continue

                parsed = _parse_cricbuzz_match_line(line, current_series)
                if parsed:
                    parsed["source_name"] = "Cricbuzz via text relay"
                    rows.append(parsed)

            if rows:
                print(f"[INFO] Text relay discovered {len(rows)} Cricbuzz T20 rows.")
                return rows
        except Exception as exc:
            print(f"[WARN] Cricbuzz text relay failed for {relay_url}: {exc}")
    return []


def scrape_cricbuzz():
    matches_found = []
    target_date = local_today()
    urls = [
        "https://m.cricbuzz.com/cricket-schedule/upcoming-series/all",
        "https://www.cricbuzz.com/cricket-schedule/upcoming-series/all",
    ]

    def parse_lines(lines):
        clean_lines = [clean_text(x) for x in lines if clean_text(x)]
        # Prefer an exact standalone date heading. Cricbuzz repeats date headers,
        # so use the last matching heading rather than an arbitrary occurrence.
        date_heading_re = re.compile(
            r"^(?:MON|TUE|WED|THU|FRI|SAT|SUN),?\\s+"
            r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\\s+"
            r"\\d{1,2},?\\s+20\\d{2}$", re.I
        )
        start_indices = []
        for idx, line in enumerate(clean_lines):
            if date_heading_re.match(line):
                dt = _parse_schedule_date(line)
                if dt == target_date:
                    start_indices.append(idx)

        if not start_indices:
            # Fallback for layouts that include extra text around the date.
            for idx, line in enumerate(clean_lines):
                dt, _ = _extract_date_token(line)
                if dt == target_date:
                    start_indices.append(idx)

        if not start_indices:
            return []

        start_idx = start_indices[-1]
        current_series = "UNKNOWN SERIES"
        current_date = target_date

        for line in clean_lines[start_idx:]:
            maybe_date, _ = _extract_date_token(line)
            if maybe_date and maybe_date != target_date:
                break
            if maybe_date == target_date:
                current_date = target_date
                continue

            low = line.lower()
            if " vs " not in low:
                if (
                    len(line) < 160
                    and (
                        "t20" in low
                        or "twenty20" in low
                        or "asian games" in low
                        or "premier league" in low
                        or "super league" in low
                        or "big bash" in low
                        or "cpl" in low
                    )
                ):
                    current_series = line
                continue

            parsed = _parse_cricbuzz_match_line(line, current_series)
            if parsed:
                matches_found.append(parsed)

        return matches_found

    # 1) Server-rendered HTML: more stable than browser body extraction on CI.
    for url in urls:
        try:
            response = SESSION.get(url, timeout=30)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            text_lines = soup.get_text("\n").splitlines()
            parsed = parse_lines(text_lines)
            if parsed:
                return parsed
        except Exception as exc:
            print(f"[WARN] Cricbuzz HTML discovery failed for {url}: {exc}")

    # 2) Browser-rendered fallback for JS-only changes.
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"
            )
            for url in urls:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(2500)
                    body_text = page.locator("body").inner_text(timeout=20000)
                    parsed = parse_lines(body_text.splitlines())
                    if parsed:
                        browser.close()
                        return parsed
                except Exception as exc:
                    print(f"[WARN] Cricbuzz browser discovery failed for {url}: {exc}")
            browser.close()
    except Exception as exc:
        print(f"[WARN] Cricbuzz browser discovery unavailable: {exc}")

    return matches_found



def match_archive_key(team_a, team_b, match_date):
    pair = sorted([norm(team_a), norm(team_b)])
    return f"{pair[0]}|{pair[1]}|{match_date}"


def load_prematch_archive():
    try:
        if PREMATCH_ARCHIVE_PATH.exists():
            data = json.loads(PREMATCH_ARCHIVE_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"[WARN] Could not read pre-match archive: {exc}")
    return {}


def save_prematch_archive(archive):
    try:
        PREMATCH_ARCHIVE_PATH.write_text(
            json.dumps(archive, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"[WARN] Could not save pre-match archive: {exc}")


def build_trade_profile(premium, venue_stats, weather, form_a, form_b, xi):
    known = 0
    total = 5
    if premium:
        known += 1
    if venue_stats.get("status") == "OK":
        known += 1
    if weather.get("status") == "OK":
        known += 1
    if form_a.get("matches", 0) >= 5 and form_b.get("matches", 0) >= 5:
        known += 1
    if xi.get("status") == "CONFIRMED":
        known += 1

    catalysts = []
    chasing = venue_stats.get("chasing_win_pct")
    if isinstance(chasing, (int, float)) and (chasing >= 60 or chasing <= 40):
        catalysts.append(f"Strong historical chase/bat-first skew: {chasing:.1f}% chasing wins")
    if weather.get("status") == "OK" and weather.get("dew_risk") in {"HIGH", "MEDIUM"}:
        catalysts.append(f"{weather.get('dew_risk')} dew-risk regime")
    spin_share = venue_stats.get("spin_wicket_share_pct")
    if isinstance(spin_share, (int, float)) and spin_share >= 35:
        catalysts.append(f"Meaningful 7-14 over spin wicket share: {spin_share:.1f}%")

    completeness = round((known / total) * 100)
    if not premium:
        swing_profile = "LOW-CONFIDENCE / LOW-MARKET"
    elif completeness >= 80 and len(catalysts) >= 2:
        swing_profile = "HIGH-INFORMATION SWING PROFILE"
    elif completeness >= 60:
        swing_profile = "MODERATE-INFORMATION SWING PROFILE"
    else:
        swing_profile = "INSUFFICIENT PRE-MATCH DATA"

    return {
        "purpose": "Pre-match market-movement profile only; not a profit guarantee.",
        "dataCompletenessPct": completeness,
        "swingProfile": swing_profile,
        "swingCatalysts": catalysts,
        "equalProfitTargetRequiresEntryOdds": True,
        "equalProfitTargetBasis": "Use actual risk capital: BACK stake, or LAY liability.",
    }


# ==========================================
# CORE TERMINAL LOGIC
# Relay source_date is accepted by final India-today filtering.
# ==========================================
def run_scanner():
    DATA_DIR.mkdir(exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not ensure_cricsheet_history():
        print("[ERROR] Cricsheet database unavailable. No fake values will be generated.")
        return

    history = load_t20_history()
    today = local_today()
    prematch_archive = load_prematch_archive()

    if PAUSE_UNTIL_IST:
        try:
            pause_until = datetime.strptime(PAUSE_UNTIL_IST, "%Y-%m-%d").date()
            if today < pause_until:
                print(f"[INFO] Scanner paused until India date {pause_until.isoformat()}; no external APIs called.")
                return
        except ValueError:
            print(f"[WARN] Invalid PAUSE_UNTIL_IST={PAUSE_UNTIL_IST!r}; ignoring.")

    # Production discovery path:
    # 1) no-key Cricbuzz text relay; 2) CricketData cricScore; 3) small Match List fallback.
    live_relay_rows = scrape_cricbuzz_live_via_text_relay()
    schedule_relay_rows = scrape_cricbuzz_via_text_relay()
    relay_rows = live_relay_rows + schedule_relay_rows
    score_rows = get_cricscore_matches() if API_KEY and not relay_rows else []

    score_today_probe = [
        raw for raw in score_rows
        if infer_match_type(raw) in {"T20", "T20I", "TWENTY20"} and match_is_today(raw)[0]
    ]
    api_rows = (
        get_cricketdata_matches()
        if API_KEY and not relay_rows and not score_today_probe and not API_USAGE.get("quota_exhausted")
        else []
    )

    # Legacy direct-site sources stay disabled in production because GitHub shared
    # runners receive HTTP 403 from them.
    sofa_rows, espn_rows, cb_rows = [], [], []

    # Discovery diagnostics must be initialized before report generation.
    relay_t20_count = sum(
        1 for raw in relay_rows
        if infer_match_type(raw, source_t20=True) in {"T20", "T20I", "TWENTY20"}
    )
    relay_today_count = sum(
        1 for raw in relay_rows
        if infer_match_type(raw, source_t20=True) in {"T20", "T20I", "TWENTY20"}
        and date_field_is_india_today(raw.get("source_date"))
    )
    sofa_t20_count = sum(
        1 for raw in sofa_rows
        if infer_match_type(raw) in {"T20", "T20I", "TWENTY20"}
    )
    sofa_today_count = sum(
        1 for raw in sofa_rows
        if infer_match_type(raw) in {"T20", "T20I", "TWENTY20"} and match_is_today(raw)[0]
    )
    espn_t20_count = sum(
        1 for raw in espn_rows
        if infer_match_type(raw) in {"T20", "T20I", "TWENTY20"}
    )
    espn_today_count = sum(
        1 for raw in espn_rows
        if infer_match_type(raw) in {"T20", "T20I", "TWENTY20"} and match_is_today(raw)[0]
    )
    score_t20_count = sum(
        1 for raw in score_rows
        if infer_match_type(raw) in {"T20", "T20I", "TWENTY20"}
    )
    score_today_count = sum(
        1 for raw in score_rows
        if infer_match_type(raw) in {"T20", "T20I", "TWENTY20"} and match_is_today(raw)[0]
    )
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

    # No-key Cricbuzz text-relay rows are preferred on GitHub Actions.
    for raw in relay_rows:
        m = dict(raw)
        m["matchType"] = "T20"
        candidates.append(m)

    # SofaScore is the first no-key, date-scoped discovery source.
    for raw in sofa_rows:
        m = dict(raw)
        if infer_match_type(m) in {"T20", "T20I", "TWENTY20"}:
            candidates.append(m)

    # ESPNcricinfo is the second no-key date-scoped source.
    for raw in espn_rows:
        m = dict(raw)
        if infer_match_type(m) in {"T20", "T20I", "TWENTY20"}:
            candidates.append(m)

    # cricScore is the date-aware secondary feed (+/-7 days). Match List is
    # fallback-only; Cricbuzz remains a last-resort fallback.
    api_by_id = {str(x.get("id")): x for x in api_rows if x.get("id")}
    for raw in score_rows:
        m = dict(raw)
        richer = api_by_id.get(str(m.get("id"))) if m.get("id") else None
        if richer:
            for key in ("venue", "series", "series_name", "date", "name"):
                if richer.get(key):
                    m[key] = richer.get(key)
            if richer.get("teams"):
                m["teams"] = richer.get("teams")
        t = infer_match_type(m)
        if t in {"T20", "T20I", "TWENTY20"}:
            candidates.append(m)

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
        pair = sorted([norm(team_a), norm(team_b)])
        key = f"{pair[0]}|{pair[1]}|{date_key}"
        if key in seen:
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

        # This scanner is pre-match only. Keep live matches visible, but do not
        # treat them as fresh pre-match candidates.
        if "LIVE" in str(m.get("status") or "").upper():
            reasons.append("MATCH ALREADY LIVE — PRE-MATCH WINDOW CLOSED")

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

        if "MATCH ALREADY LIVE — PRE-MATCH WINDOW CLOSED" in reasons:
            verdict = "🔴 LIVE — PRE-MATCH WINDOW CLOSED"
        elif "LIQUIDITY NOT VERIFIED: LOW/UNKNOWN MARKET TIER" in reasons:
            verdict = "🔴 HIGH-CAUTION / NO-BET"
        elif not reasons:
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
        trade_profile = build_trade_profile(premium, venue_stats, weather, form_a, form_b, xi)

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
            "sourceName": m.get("source_name") or "UNKNOWN SOURCE",
            "sourceStatus": m.get("status") or "UNKNOWN",
            "marketTier": tier,
            "marketVisibility": "PREMIUM / LIKELY LISTED" if premium else "SCHEDULE-ONLY / LOW-VISIBILITY",
            "liquidityProxyOnly": True,
            "premiumMarketProxy": premium,
            "tradeProfile": trade_profile,
            "preMatchSnapshot": None,
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

    # Freeze scheduled pre-match analysis, then attach it to the same fixture
    # after the match becomes live. This preserves the decision context.
    for report in output:
        key = match_archive_key(
            report.get("teamA"),
            report.get("teamB"),
            (report.get("sourceDate") or today.isoformat())[:10],
        )
        is_live = "LIVE" in str(report.get("sourceStatus") or "").upper()
        if not is_live:
            snapshot = dict(report)
            snapshot["preMatchSnapshot"] = None
            prematch_archive[key] = {
                "capturedAt": datetime.now(timezone.utc).isoformat(),
                "matchDate": (report.get("sourceDate") or today.isoformat())[:10],
                "report": snapshot,
            }
        elif key in prematch_archive:
            report["preMatchSnapshot"] = prematch_archive[key]

    save_prematch_archive(prematch_archive)

    output.sort(key=lambda x: (
        0 if x.get("premiumMarketProxy") else 1,
        0 if "LIVE" in str(x.get("sourceStatus", "")).upper() else 1,
        x.get("matchTimeIST", "9999")
    ))
    source_rows_total = len(relay_rows) + len(score_rows) + len(api_rows)
    now_iso = datetime.now(timezone.utc).isoformat()

    payload = {
        "last_updated": now_iso,
        "scanner_status": {
            "state": (
                "OK" if len(output) > 0
                else "DISCOVERY_FILTERED" if source_rows_total > 0
                else "QUOTA_EXHAUSTED" if API_USAGE.get("quota_exhausted")
                else "SOURCE_UNAVAILABLE"
            ),
            "stale": False,
            "message": (
                "Fresh scan completed."
                if len(output) > 0
                else "Fixtures were discovered but none survived final report filters."
                if source_rows_total > 0
                else "CricketData quota exhausted and no no-key source was available."
                if API_USAGE.get("quota_exhausted")
                else "No discovery source returned data."
            ),
        },
        "source": {"primary": "Cricbuzz via text relay", "secondary": "CricketData cricScore", "fallback": "CricketData Match List"},
        "date_filter": {"timezone": "Asia/Kolkata", "today": today.isoformat(), "today_only": TODAY_ONLY, "method": "GMT timestamp when available; India date otherwise"},
        "api_budget": {
            "daily_limit_user_reported": 100,
            "max_match_list_calls_per_run": MAX_MATCH_PAGES_PER_RUN,
            "cricscore_calls_this_run": 1 if (API_KEY and not relay_rows) else 0,
            "max_squad_calls_per_run": MAX_XI_LOOKUPS_PER_RUN,
            "hits_today_reported": API_USAGE.get("hitsToday"),
            "hits_limit_reported": API_USAGE.get("hitsLimit"),
            "quota_exhausted": API_USAGE.get("quota_exhausted"),
            "api_last_error": API_USAGE.get("last_error")
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
            "live_text_relay_rows": len(live_relay_rows),
            "schedule_text_relay_rows": len(schedule_relay_rows),
            "text_relay_rows": len(relay_rows),
            "text_relay_t20_rows": relay_t20_count,
            "text_relay_today_t20_rows": relay_today_count,
            "cricscore_rows": len(score_rows),
            "cricscore_t20_rows": score_t20_count,
            "cricscore_today_t20_rows": score_today_count,
            "cricketdata_rows": len(api_rows),
            "cricketdata_t20_rows": api_t20_count,
            "cricketdata_today_t20_rows": api_today_count,
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
    }

    intel_path = Path("intel.json")
    if source_rows_total == 0 and intel_path.exists():
        try:
            previous = json.loads(intel_path.read_text(encoding="utf-8"))
            previous_date = (previous.get("date_filter") or {}).get("today")
            previous_matches = previous.get("matches") or []
            if previous_date == today.isoformat() and previous_matches:
                previous["last_checked"] = now_iso
                previous["scanner_status"] = {
                    "state": payload["scanner_status"]["state"],
                    "stale": True,
                    "message": payload["scanner_status"]["message"] + " Preserving earlier same-day report."
                }
                previous["api_budget"] = payload["api_budget"]
                previous["discovery_last_attempt"] = payload["discovery"]
                payload = previous
                print("[WARN] Discovery failed; preserving earlier same-day non-empty intel.json.")
        except Exception as exc:
            print(f"[WARN] Could not preserve previous intel.json: {exc}")

    intel_path.write_text(json.dumps(payload, indent=4, ensure_ascii=False), encoding="utf-8")
    if output:
        Path("intel_last_good.json").write_text(
            json.dumps(payload, indent=4, ensure_ascii=False), encoding="utf-8"
        )
    print(f"[INFO] Saved {len(payload.get('matches') or [])} T20 fixtures for India date {today.isoformat()}.")


if __name__ == "__main__":
    run_scanner()
