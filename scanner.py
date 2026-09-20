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
LOOKAHEAD_DAYS = int(os.getenv("SCANNER_LOOKAHEAD_DAYS", "7"))
MIN_VENUE_MATCHES = int(os.getenv("MIN_VENUE_MATCHES", "5"))
MIN_SPIN_CLASSIFICATION = float(os.getenv("MIN_SPIN_CLASSIFICATION", "0.90"))
XI_LOOKAHEAD_MIN = int(os.getenv("XI_LOOKAHEAD_MIN", "180"))

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
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d", "%d %b %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(str(value), fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    return None


def is_standard_t20(match):
    mt = norm(match.get("matchType", match.get("match_type", "")))
    name = norm(match.get("name", ""))
    if any(token in name for token in ("test", "odi", "one day", "t10", "hundred", "100 ball")):
        return False
    return mt in {"t20", "t20i", "twenty20"}


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
    for _ in range(10):
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
    if marker.exists(): return
    print("[INFO] Initializing Cricsheet Database...")
    zip_path = DATA_DIR / "all_json.zip"
    r = SESSION.get(CRICSHEET_URL, stream=True, timeout=30)
    with zip_path.open("wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk: f.write(chunk)
    with zipfile.ZipFile(zip_path) as z: z.extractall(HISTORY_DIR)
    marker.write_text(datetime.now(timezone.utc).isoformat())
    zip_path.unlink()

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
def scrape_cricbuzz():
    matches_found = []
    if sync_playwright is None:
        return matches_found
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36")
            page = context.new_page()
            page.goto("https://www.cricbuzz.com/cricket-schedule/upcoming-series/t20", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            soup = BeautifulSoup(page.content(), "html.parser")
            for a_tag in soup.find_all("a", href=re.compile(r"live-cricket-scores")):
                title = clean_text(a_tag.get("title", ""))
                if " vs " not in title.lower() and " v " not in title.lower():
                    continue
                clean_title = title.replace(" - Live Cricket Score", "")
                parts = re.split(r"\s+vs\s+|\s+v\s+", clean_title, maxsplit=1, flags=re.I)
                if len(parts) != 2:
                    continue
                team_a = parts[0].split(",")[0].strip()
                team_b = parts[1].split(",")[0].strip()
                venue = "UNKNOWN VENUE"
                parent = a_tag.find_parent("div", class_="cb-col") or a_tag.parent
                if parent:
                    venue_div = parent.find("div", class_="text-gray")
                    if venue_div:
                        venue = clean_text(venue_div.get_text(" ", strip=True)).split("•")[0].strip()
                href = a_tag.get("href", "")
                matches_found.append({
                    "id": None, "teamA": team_a, "teamB": team_b,
                    "name": f"{team_a} vs {team_b}", "matchType": "t20",
                    "teams": [team_a, team_b], "venue": venue, "status": "Scheduled",
                    "cricbuzz_url": ("https://www.cricbuzz.com" + href) if href.startswith("/") else href
                })
            browser.close()
    except Exception as exc:
        print(f"[WARN] Cricbuzz schedule failed: {exc}")
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
    matches = get_cricketdata_matches() if API_KEY else []
    source = "CricketData API" if matches else "Cricbuzz fallback"
    if not matches:
        matches = scrape_cricbuzz()

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=LOOKAHEAD_DAYS)
    output, seen = [], set()

    for raw in matches:
        m = dict(raw)
        if not m.get("matchType") and source == "Cricbuzz fallback":
            m["matchType"] = "t20"
        teams = m.get("teams") or [m.get("teamA"), m.get("teamB")]
        if len(teams) < 2:
            continue
        dt = parse_dt(m.get("dateTimeGMT") or m.get("date_time_gmt"))
        if dt and (dt < now - timedelta(hours=1) or dt > cutoff):
            continue
        key = f"{norm(m.get('name'))}|{norm(m.get('venue'))}"
        if key in seen:
            continue
        seen.add(key)

        team_a, team_b = teams[0], teams[1]
        standard = is_standard_t20(m)
        premium, tier = premium_market({"name": m.get("name"), "series_name": m.get("series_name") or m.get("series"), "teams": teams})
        venue = clean_text(m.get("venue")) or "UNKNOWN VENUE"

        xi = {"status": "NOT CONFIRMED", "teams": {}, "source": "Not requested"}
        dynamic_styles = {}
        if standard and premium and API_KEY and m.get("id") and dt and dt <= now + timedelta(minutes=XI_LOOKAHEAD_MIN):
            squad = get_match_squad(m.get("id"))
            _, dynamic_styles = extract_squad(squad)
            xi = confirmed_xi_status(squad)
        if xi.get("status") != "CONFIRMED" and m.get("cricbuzz_url"):
            page_xi = check_cricbuzz_xi(m.get("cricbuzz_url"))
            if page_xi.get("status") == "XI PUBLISHED":
                xi = page_xi

        venue_stats = analyze_venue(history, venue, dynamic_styles) if standard and venue != "UNKNOWN VENUE" else {"status": "NO DATA / UNKNOWN VENUE"}
        weather = get_match_weather(venue, dt) if standard and venue != "UNKNOWN VENUE" else {"status": "UNKNOWN", "reason": "No usable venue/time"}
        form_a, form_b = team_form(history, team_a), team_form(history, team_b)

        reasons, warnings = [], []
        if not standard:
            reasons.append("NOT STANDARD T20")
        if not premium:
            reasons.append("LOW/UNKNOWN MARKET TIER")
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
        if not form_a.get("matches") or not form_b.get("matches"):
            reasons.append("INSUFFICIENT RECENT T20 DATA")
        if venue_stats.get("spin_index_status") != "OK":
            warnings.append("SPIN CHOKE INDEX NOT FULLY CLASSIFIED")

        if not standard or not premium:
            verdict = "🔴 REJECTED: FORMAT/MARKET"
        elif reasons:
            verdict = "🟡 WAIT / NO-BET" if "PLAYING XI NOT CONFIRMED" in reasons else "🔴 NO-BET"
        else:
            verdict = "🟢 READY FOR PRICE CHECK"

        if weather.get("status") == "OK":
            weather_text = f"{weather.get('temperature_c')}°C | RH {weather.get('humidity_pct')}% | Rain {weather.get('rain_probability_pct')}% | Wind {weather.get('wind_kmh')} km/h"
        else:
            weather_text = "NO DATA"
        pp = venue_stats.get("powerplay_avg")
        spin_share = venue_stats.get("spin_wicket_share_pct")

        output.append({
            "teamA": team_a, "teamB": team_b, "name": m.get("name"),
            "matchType": m.get("matchType"), "venue": venue, "dateTimeGMT": m.get("dateTimeGMT"),
            "series": m.get("series_name") or m.get("series"),
            "marketTier": tier, "premiumMarketProxy": premium, "standardT20": standard,
            "playingXI": xi, "formA": form_a, "formB": form_b, "weather": weather,
            "weatherText": weather_text, "dewRisk": weather.get("dew_risk", "UNKNOWN"),
            "tossBias": f"{venue_stats.get('chasing_win_pct')}% Chasing Wins" if venue_stats.get("chasing_win_pct") is not None else "UNKNOWN",
            "tossTrend": "Historical venue trend",
            "ppScoreAvg": f"{pp:.2f}" if isinstance(pp, (int, float)) else "UNKNOWN",
            "ppRunRate": "Historical 1st-innings PP",
            "spinIndex": f"{spin_share:.2f}% Spinner Wicket Share (7-14)" if isinstance(spin_share, (int, float)) else "UNKNOWN",
            "spinNote": venue_stats.get("spin_index_status", "UNKNOWN"),
            "venueIntel": venue_stats,
            "midOversWickets": venue_stats.get("mid_overs_7_14_wickets"),
            "spinnerWickets7to14": venue_stats.get("spinner_7_14_wickets"),
            "spinClassificationPct": venue_stats.get("spin_classification_pct"),
            "verdict": verdict, "noBetReasons": reasons, "warnings": warnings,
            "badgeClass": "badge-green" if verdict.startswith("🟢") else "badge-red" if verdict.startswith("🔴") else "badge-yellow",
            "strategyText": "; ".join(reasons) if reasons else "No critical pre-match data risk detected."
        })

    Path("intel.json").write_text(json.dumps({
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "rules": {
            "standard_t20_only": True,
            "premium_market_proxy": True,
            "no_fake_data": True,
            "min_venue_matches": MIN_VENUE_MATCHES,
            "min_spin_classification": MIN_SPIN_CLASSIFICATION
        },
        "matches": output
    }, indent=4, ensure_ascii=False), encoding="utf-8")
    print(f"[INFO] Saved {len(output)} T20 fixtures to intel.json")


if __name__ == "__main__":
    run_scanner()
