import json, re, zipfile
from pathlib import Path
from datetime import datetime, timezone
import requests
import xml.etree.ElementTree as ET

# ==========================================
# CONFIGURATION
# ==========================================
CRICAPI_KEY = "8ea9030f-60fe-46a7-a2aa-e985361b63bd"

DATA_DIR = Path("./data")
HISTORY_DIR = DATA_DIR / "cricsheet_all"
CACHE_DIR = DATA_DIR / "cache"
CRICSHEET_URL = "https://cricsheet.org/downloads/all_json.zip"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Pro-Terminal/CricAPI-Engine"})

PREMIUM_KEYWORDS = ["ipl", "cpl", "bbl", "psl", "sa20", "blast", "mlc", "ilt20", "lanka", "bpl", "super smash", "nepal", "guyana", "jamaica", "barbados", "lucia", "trinbago", "kitts", "antigua", "falcons"]
MAJOR_TEAMS = ["england", "india", "australia", "sri lanka", "west indies", "south africa", "new zealand", "pakistan", "bangladesh", "afghanistan", "ireland", "zimbabwe"]
JUNK_WORDS = ["test", "odi", "one day", "t10", "hundred", "women's club"]

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
    if None in (temp, dp, rh): return "UNKNOWN"
    spread = temp - dp
    score = 0
    if rh >= 80: score += 3
    elif rh >= 70: score += 2
    elif rh >= 60: score += 1
    if spread <= 2: score += 3
    elif spread <= 4: score += 2
    elif spread <= 6: score += 1
    if wind is not None:
        if wind <= 8: score += 1
        elif wind >= 20: score -= 1
    if precip is not None and precip >= 1.0: score -= 1
    if score >= 6: return "HIGH DEW RISK 🔴"
    if score >= 4: return "MEDIUM DEW RISK 🟡"
    return "LOW DEW RISK 🟢"

def get_live_weather(venue):
    geo = geocode_venue(venue)
    if not geo: return "Weather Unavailable", "Unknown"
    try:
        params = {"latitude": geo["lat"], "longitude": geo["lon"], "timezone": geo["tz"], "current": "temperature_2m,relative_humidity_2m,dew_point_2m,precipitation,wind_speed_10m"}
        c = SESSION.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=10).json().get("current", {})
        temp, rh, dp = c.get("temperature_2m"), c.get("relative_humidity_2m"), c.get("dew_point_2m")
        wind, precip = c.get("wind_speed_10m"), c.get("precipitation")
        wx_str = f"{temp}°C | RH: {rh}% | Wind: {wind}km/h"
        return wx_str, calculate_dew_risk(temp, dp, rh, wind, precip)
    except: return "API Error", "Unknown"

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

def analyze_venue(history, target_venue):
    vn = str(target_venue).lower().strip()
    first_inn_scores, pp_scores = [], []
    chase_wins, completed, mid_balls, mid_wickets = 0, 0, 0, 0

    for m in history:
        info = m.get("info", {})
        if vn not in str(info.get("venue", "")).lower(): continue
        innings = m.get("innings", [])
        if not innings: continue
        
        first_total, first_pp = 0, 0
        for over in innings[0].get("overs", []):
            o_no = int(over.get("over", -1))
            for d in over.get("deliveries", []):
                runs = int(d.get("runs", {}).get("total", 0))
                first_total += runs
                if 0 <= o_no <= 5: first_pp += runs
        first_inn_scores.append(first_total)
        pp_scores.append(first_pp)

        winner = info.get("outcome", {}).get("winner")
        if len(innings) >= 2 and winner:
            if str(winner).lower() == str(innings[1].get("team", "")).lower(): chase_wins += 1
            completed += 1

        for inn in innings:
            for over in inn.get("overs", []):
                if 6 <= int(over.get("over", -1)) <= 13:
                    for d in over.get("deliveries", []):
                        mid_balls += 1
                        if d.get("wickets"): mid_wickets += 1

    if len(first_inn_scores) < 5: return None 
    avg_pp = sum(pp_scores) / len(first_inn_scores)
    chase_pct = (chase_wins / completed * 100) if completed > 0 else 50
    mid_wkt_pct = (mid_wickets / mid_balls * 100) if mid_balls > 0 else 0
    return {"matches": len(first_inn_scores), "pp_avg": f"{avg_pp:.1f} / 1", "toss_bias": f"{chase_pct:.1f}% Chasing Wins", "spin_idx": f"{mid_wkt_pct:.1f}% Wkts (Overs 7-14)"}

# ==========================================
# CRICAPI CORE ENGINE
# ==========================================
def run_scanner():
    ensure_cricsheet_history()
    t20_db = load_t20_history()
    live_matches = []
    seen_matches = set()
    
    api_limit_exceeded = False
    
    url = f"https://api.cricapi.com/v1/currentMatches?apikey={CRICAPI_KEY}&offset=0"
    
    try:
        response = SESSION.get(url, timeout=10).json()
        
        # Check if API threw a quota/limit error
        if response.get("status") != "success" or "hitsToday" in str(response.get("info", {})):
            info = response.get("info", {})
            if info.get("hitsToday", 0) >= info.get("hitsLimit", 100):
                api_limit_exceeded = True

        matches = response.get("data", [])
        
        for m in matches:
            title = str(m.get("name", ""))
            match_type = str(m.get("matchType", "")).lower()
            combined_text = f"{title} {match_type}".lower()

            if any(j in combined_text for j in JUNK_WORDS): continue
            
            is_franchise = any(k in combined_text for k in PREMIUM_KEYWORDS)
            is_t20 = "t20" in combined_text or "twenty20" in combined_text or is_franchise
            if not is_t20: continue
            if m.get("matchEnded", False): continue

            teamA, teamB = "Team A", "Team B"
            if m.get("teamInfo") and len(m["teamInfo"]) >= 2:
                teamA = m["teamInfo"][0].get("name", "Team A")
                teamB = m["teamInfo"][1].get("name", "Team B")
            else:
                clean_title = title.split(",")[0].lower()
                parts = re.split(r'\s+vs\s+|\s+v\s+', clean_title)
                if len(parts) >= 2:
                    teamA, teamB = parts[0].strip().title(), parts[1].strip().title()

            venue = str(m.get("venue", "UNKNOWN VENUE"))
            if not venue or venue.lower() == "none" or venue == "": venue = "UNKNOWN VENUE"
            status_text = str(m.get("status", "Upcoming"))
            
            is_intl = any(t in combined_text for t in MAJOR_TEAMS)
            is_premium = is_franchise or is_intl
            
            score_str, rr_str = "0/0 (0.0 ov)", "0.00 RPO"
            score_data = m.get("score", [])
            if score_data and isinstance(score_data, list):
                latest_innings = score_data[-1]
                runs, wkts, overs = latest_innings.get("r", 0), latest_innings.get("w", 0), latest_innings.get("o", 0)
                score_str = f"{runs}/{wkts} ({overs} ov)"
                rr_str = f"{(runs/float(overs)):.2f} RPO" if float(overs) > 0 else "0.00 RPO"

            match_key = f"{teamA} vs {teamB}".lower()
            seen_matches.add(match_key)

            if is_premium:
                if venue != "UNKNOWN VENUE":
                    wx_str, dew_str = get_live_weather(venue)
                    v_stats = analyze_venue(t20_db, venue)
                    if v_stats:
                        verdict, badge = "🟢 PRO DATA VERIFIED", "badge-green"
                        strategy = f"Cricsheet History ({v_stats['matches']} matches). Trade safely."
                        toss_bias, pp_avg, spin_idx = v_stats['toss_bias'], v_stats['pp_avg'], v_stats['spin_idx']
                    else:
                        verdict, badge = "🟡 INSUFFICIENT VENUE DATA", "badge-yellow"
                        strategy, toss_bias, pp_avg, spin_idx = "Less than 5 historical T20s found here.", "UNKNOWN", "UNKNOWN", "UNKNOWN"
                else:
                    verdict, badge, strategy = "🟡 INSUFFICIENT VENUE DATA", "badge-yellow", "CricAPI did not provide a venue name yet. Rely strictly on live odds."
                    wx_str, dew_str, toss_bias, pp_avg, spin_idx = "N/A", "N/A", "UNKNOWN", "UNKNOWN", "UNKNOWN"
            else:
                verdict, badge, strategy = "🔴 REJECTED: JUNK T20", "badge-red", "Low liquidity match. Stop-loss logic will fail. DO NOT TRADE."
                wx_str, dew_str, toss_bias, pp_avg, spin_idx = "N/A", "N/A", "N/A", "N/A", "N/A"

            live_matches.append({
                "teamA": teamA, "teamB": teamB, "venue": venue, "weather": wx_str, "dewRisk": dew_str,
                "tossBias": toss_bias, "tossTrend": status_text, "ppScoreAvg": pp_avg, "ppRunRate": f"{score_str} @ {rr_str}",
                "spinIndex": spin_idx, "spinNote": "Live Match Radar Active", "verdict": verdict, "badgeClass": badge, "strategyText": strategy
            })

    except Exception as e:
        print(f"CricAPI Connection Error: {e}")

    # ==========================================
    # RSS FALLBACK (TRIGGERS IF CRICAPI IS EMPTY/LIMITED)
    # ==========================================
    if len(live_matches) == 0:
        try:
            xml_data = SESSION.get("http://static.cricinfo.com/rss/livescores.xml", timeout=10).text
            root = ET.fromstring(xml_data)
            for item in root.findall('./channel/item'):
                title = item.find('title').text
                if ' v ' in title:
                    parts = title.split(' v ')
                    teamA, teamB = re.sub(r'[0-9/\*\(\)]+', '', parts[0]).strip(), re.sub(r'[0-9/\*\(\)]+', '', parts[1]).strip()
                    
                    combined_text = title.lower()
                    if any(j in combined_text for j in JUNK_WORDS): continue
                    
                    is_franchise = any(k in combined_text for k in PREMIUM_KEYWORDS)
                    is_intl = any(t in combined_text for t in MAJOR_TEAMS)
                    is_premium = is_franchise or is_intl
                    
                    if is_premium:
                        live_matches.append({
                            "teamA": teamA, "teamB": teamB, "venue": "UNKNOWN VENUE",
                            "weather": "N/A", "dewRisk": "N/A", "tossBias": "UNKNOWN", "tossTrend": "Toss Unknown (via Backup Feed)",
                            "ppScoreAvg": "UNKNOWN", "ppRunRate": "0/0 @ 0.00 RPO", "spinIndex": "UNKNOWN", "spinNote": "Match Radar Active",
                            "verdict": "🟡 INSUFFICIENT VENUE DATA", "badgeClass": "badge-yellow",
                            "strategyText": "Match active via Fallback RSS API. Venue is unknown. Rely strictly on live odds."
                        })
        except: pass

    # ==========================================
    # FINAL SYSTEM DIAGNOSTICS
    # ==========================================
    if len(live_matches) == 0:
        if api_limit_exceeded:
            live_matches.append({
                "teamA": "SYSTEM", "teamB": "LIMIT HIT", "venue": "CricAPI Servers",
                "weather": "N/A", "dewRisk": "N/A", "tossBias": "N/A", "tossTrend": "N/A",
                "ppScoreAvg": "N/A", "ppRunRate": "N/A", "spinIndex": "N/A", "spinNote": "N/A",
                "verdict": "🔴 API QUOTA EXCEEDED", "badgeClass": "badge-red",
                "strategyText": "Your free 100 hits/day on CricAPI are exhausted. Fallback RSS is also empty. Rest day."
            })
        else:
            live_matches.append({
                "teamA": "SYSTEM", "teamB": "ONLINE", "venue": "Global Database",
                "weather": "N/A", "dewRisk": "N/A", "tossBias": "N/A", "tossTrend": "N/A",
                "ppScoreAvg": "N/A", "ppRunRate": "N/A", "spinIndex": "N/A", "spinNote": "N/A",
                "verdict": "⚠️ NO STANDARD T20 MATCHES TODAY", "badgeClass": "badge-yellow",
                "strategyText": "No major T20s found in any feed. Rest day."
            })

    with open("intel.json", "w", encoding="utf-8") as f:
        json.dump({"last_updated": str(datetime.now(timezone.utc)), "matches": live_matches}, f, indent=4)
    print(f"Scan Complete. Saved {len(live_matches)} matches.")

if __name__ == "__main__":
    run_scanner()
