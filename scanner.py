import json, re, math, zipfile
from pathlib import Path
from datetime import datetime, timezone
import requests

DATA_DIR = Path("./data")
HISTORY_DIR = DATA_DIR / "cricsheet_all"
CACHE_DIR = DATA_DIR / "cache"
CRICSHEET_URL = "https://cricsheet.org/downloads/all_json.zip"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Ultimate-Pro-Terminal/11.0"})

PREMIUM_KEYWORDS = ["cpl", "caribbean", "ipl", "indian premier", "bbl", "big bash", "psl", "pakistan super", "sa20", "blast", "mlc", "lanka premier"]
MAJOR_TEAMS = ["england", "india", "australia", "sri lanka", "west indies", "south africa", "new zealand", "pakistan", "bangladesh", "afghanistan"]
JUNK_WORDS = ["club", "university", "college", "academy", "regional", "minor", "county", "u19", "women's club", "hundred", "t10", "odi", "test"]

# ==========================================
# OPEN-METEO WEATHER ENGINE (DEW RISK ALG)
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

def calculate_dew_risk(temp, dew_point, rh, wind, precip):
    if None in (temp, dew_point, rh): return "UNKNOWN"
    spread = temp - dew_point
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
        params = {
            "latitude": geo["lat"], "longitude": geo["lon"], "timezone": geo["tz"],
            "current": "temperature_2m,relative_humidity_2m,dew_point_2m,precipitation,wind_speed_10m"
        }
        r = SESSION.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=10)
        c = r.json().get("current", {})
        
        temp = c.get("temperature_2m")
        rh = c.get("relative_humidity_2m")
        dp = c.get("dew_point_2m")
        wind = c.get("wind_speed_10m")
        precip = c.get("precipitation")
        
        weather_str = f"{temp}°C | RH: {rh}% | Wind: {wind}km/h"
        dew_str = calculate_dew_risk(temp, dp, rh, wind, precip)
        return weather_str, dew_str
    except:
        return "API Error", "Unknown"

# ==========================================
# CRICSHEET HISTORICAL MATH ENGINE
# ==========================================
def ensure_cricsheet_history():
    DATA_DIR.mkdir(exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    marker = HISTORY_DIR / ".ready"
    
    if marker.exists(): return
    print("[INFO] Downloading Cricsheet History... This happens only once.")
    
    zip_path = DATA_DIR / "all_json.zip"
    r = SESSION.get(CRICSHEET_URL, stream=True)
    with zip_path.open("wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk: f.write(chunk)
            
    print("[INFO] Extracting JSON database...")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(HISTORY_DIR)
        
    marker.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    zip_path.unlink()

def load_t20_history():
    history = []
    print("[INFO] Loading T20 Match History into Memory...")
    for p in HISTORY_DIR.glob("*.json"):
        try:
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
                m_type = str(data.get("info", {}).get("match_type", "")).lower()
                if m_type in ["t20", "t20i", "twenty20"]:
                    history.append(data)
        except: continue
    return history

def analyze_venue(history, target_venue):
    vn = str(target_venue).lower().strip()
    first_inn_scores = []
    pp_scores = []
    chase_wins = 0
    completed = 0
    mid_balls, mid_wickets = 0, 0

    for m in history:
        info = m.get("info", {})
        if vn not in str(info.get("venue", "")).lower(): continue
        
        innings = m.get("innings", [])
        if not innings: continue
        
        first_total, first_pp = 0, 0
        for over in innings[0].get("overs", []):
            over_no = int(over.get("over", -1))
            for d in over.get("deliveries", []):
                runs = int(d.get("runs", {}).get("total", 0))
                first_total += runs
                if 0 <= over_no <= 5: first_pp += runs
        
        first_inn_scores.append(first_total)
        pp_scores.append(first_pp)

        winner = info.get("outcome", {}).get("winner")
        if len(innings) >= 2 and winner:
            second_team = innings[1].get("team")
            if second_team and str(winner).lower() == str(second_team).lower():
                chase_wins += 1
            completed += 1

        for inn in innings:
            for over in inn.get("overs", []):
                over_no = int(over.get("over", -1))
                if 6 <= over_no <= 13:
                    for d in over.get("deliveries", []):
                        mid_balls += 1
                        if d.get("wickets"): mid_wickets += 1

    matches_found = len(first_inn_scores)
    if matches_found < 5: return None 

    avg_pp = sum(pp_scores) / matches_found
    chase_pct = (chase_wins / completed * 100) if completed > 0 else 50
    mid_wkt_pct = (mid_wickets / mid_balls * 100) if mid_balls > 0 else 0

    return {
        "matches": matches_found,
        "pp_avg": f"{avg_pp:.1f} / 1",
        "toss_bias": f"{chase_pct:.1f}% Chasing Wins",
        "spin_idx": f"{mid_wkt_pct:.1f}% Wkts (Overs 7-14)"
    }

# ==========================================
# DATA FUSION ENGINE (ESPN + CRICBUZZ BACKUP)
# ==========================================
def run_scanner():
    ensure_cricsheet_history()
    t20_db = load_t20_history()

    live_matches = []
    seen_teams = set()
    
    # SOURCE 1: ESPN SCOREPANEL (Primary - Handles Live Scores)
    try:
        espn_url = "https://site.api.espn.com/apis/site/v2/sports/cricket/scorepanel"
        data = SESSION.get(espn_url, timeout=15).json()
        
        for event in data.get('events', []):
            comp = event.get('competitions', [{}])[0]
            title = event.get('name', '')
            series = event.get('season', {}).get('slug', '').replace('-', ' ')
            match_type = comp.get('type', {}).get('abbreviation', '').lower()
            state = comp.get('status', {}).get('type', {}).get('state', 'pre') 
            
            if state == 'post': continue
                
            combined_text = f"{title} {series} {match_type}".lower()
            if any(j in combined_text for j in JUNK_WORDS): continue
            
            # THE MASSIVE BUG FIX 💥
            is_franchise = any(k in combined_text for k in PREMIUM_KEYWORDS)
            is_t20 = "t20" in combined_text or "twenty20" in combined_text or is_franchise
            
            if not is_t20: continue
            
            is_intl = is_t20 and any(t in combined_text for t in MAJOR_TEAMS)
            is_premium = is_franchise or is_intl

            venue = comp.get('venue', {}).get('fullName', 'Unknown Venue')
            teams = [t.get('team', {}).get('name', 'Unknown') for t in comp.get('competitors', [])]
            if len(teams) < 2: continue
            
            seen_teams.add(teams[0].lower())

            # Live Scoring Setup
            if state == 'pre':
                toss_str = "Upcoming Match (Toss pending)"
                score_str, rr_str, spin_note = "0/0", "0.00 RPO", "Match starts later today"
            else:
                toss = comp.get('status', {}).get('toss', {})
                toss_str = f"{toss.get('winner', {}).get('text', 'Toss')} elected to {toss.get('decision', 'pending')}" if toss else "Live Match"
                
                runs, wkts, overs = 0, 0, 0
                for c in comp.get('competitors', []):
                    ls = c.get('linescores', [])
                    if ls:
                        runs, wkts, overs = ls[-1].get('value', 0), ls[-1].get('outs', 0), ls[-1].get('overs', 0)
                        break
                score_str = f"{runs}/{wkts} ({overs} ov)"
                rr_str = f"{(runs/overs):.2f} RPO" if overs > 0 else "0.00 RPO"
                spin_note = "Execute Phase 3 & Squeeze dynamically."

            if is_premium:
                wx_str, dew_str = get_live_weather(venue)
                v_stats = analyze_venue(t20_db, venue)
                
                if v_stats:
                    verdict, badge = "🟢 PRO DATA VERIFIED", "badge-green"
                    strategy = f"Cricsheet History ({v_stats['matches']} matches). Trade safely."
                    toss_bias, pp_avg, spin_idx = v_stats['toss_bias'], v_stats['pp_avg'], v_stats['spin_idx']
                else:
                    verdict, badge = "🟡 INSUFFICIENT VENUE DATA", "badge-yellow"
                    strategy = "Less than 5 historical T20s found here. Rely strictly on live odds."
                    toss_bias, pp_avg, spin_idx = "UNKNOWN", "UNKNOWN", "UNKNOWN"
            else:
                verdict, badge = "🔴 REJECTED: JUNK T20", "badge-red"
                strategy = "Low liquidity match. Stop-loss logic will fail. DO NOT TRADE."
                wx_str, dew_str, toss_bias, pp_avg, spin_idx = "N/A", "N/A", "N/A", "N/A", "N/A"

            live_matches.append({
                "teamA": teams[0], "teamB": teams[1], "venue": venue,
                "weather": wx_str, "dewRisk": dew_str,
                "tossBias": toss_bias, "tossTrend": toss_str,
                "ppScoreAvg": pp_avg, "ppRunRate": f"{score_str} @ {rr_str}",
                "spinIndex": spin_idx, "spinNote": spin_note,
                "verdict": verdict, "badgeClass": badge,
                "strategyText": strategy
            })
    except Exception as e: print(f"ESPN Error: {e}")

    # SOURCE 2: CRICBUZZ UNOFFICIAL API (Your Backup Addition!)
    cb_urls = [
        "https://cricbuzz-live.vercel.app/v1/matches/upcoming?type=international",
        "https://cricbuzz-live.vercel.app/v1/matches/upcoming?type=league"
    ]
    for cb_url in cb_urls:
        try:
            cb_data = SESSION.get(cb_url, timeout=10).json()
            for m in cb_data.get("data", {}).get("matches", []):
                title = str(m.get("title") or "")
                if " vs " not in title: continue
                cb_teams = [x.strip() for x in title.split(" vs ")[:2]]
                
                if cb_teams[0].lower() in seen_teams: continue # Already grabbed by ESPN!
                
                series = str(m.get("series", "")).lower()
                combined_text = f"{title} {series}".lower()
                if any(j in combined_text for j in JUNK_WORDS): continue
                
                is_franchise = any(k in combined_text for k in PREMIUM_KEYWORDS)
                is_t20 = "t20" in combined_text or "twenty20" in combined_text or is_franchise
                
                if not is_t20: continue
                
                is_intl = is_t20 and any(t in combined_text for t in MAJOR_TEAMS)
                is_premium = is_franchise or is_intl

                venue = str(m.get("timeAndPlace", {}).get("place", "")).replace(" at ", "").strip()
                time_raw = str(m.get("timeAndPlace", {}).get("time", "")).replace("&nbsp;", " ").strip()

                if is_premium:
                    wx_str, dew_str = get_live_weather(venue)
                    v_stats = analyze_venue(t20_db, venue)
                    
                    if v_stats:
                        verdict, badge = "🟢 PRO DATA VERIFIED", "badge-green"
                        strategy = f"Cricsheet History ({v_stats['matches']} matches). Trade safely."
                        toss_bias, pp_avg, spin_idx = v_stats['toss_bias'], v_stats['pp_avg'], v_stats['spin_idx']
                    else:
                        verdict, badge = "🟡 INSUFFICIENT VENUE DATA", "badge-yellow"
                        strategy = "Less than 5 historical T20s found here. Rely strictly on live odds."
                        toss_bias, pp_avg, spin_idx = "UNKNOWN", "UNKNOWN", "UNKNOWN"
                else:
                    verdict, badge = "🔴 REJECTED: JUNK T20", "badge-red"
                    strategy = "Low liquidity match. Stop-loss logic will fail. DO NOT TRADE."
                    wx_str, dew_str, toss_bias, pp_avg, spin_idx = "N/A", "N/A", "N/A", "N/A", "N/A"

                live_matches.append({
                    "teamA": cb_teams[0], "teamB": cb_teams[1], "venue": venue,
                    "weather": wx_str, "dewRisk": dew_str,
                    "tossBias": toss_bias, "tossTrend": f"Upcoming at {time_raw}",
                    "ppScoreAvg": pp_avg, "ppRunRate": "0/0 @ 0.00 RPO",
                    "spinIndex": spin_idx, "spinNote": "Match starts later",
                    "verdict": verdict, "badgeClass": badge,
                    "strategyText": strategy
                })
        except: pass

    if not live_matches:
        live_matches.append({
            "teamA": "SYSTEM", "teamB": "ONLINE", "venue": "Global Database",
            "weather": "N/A", "dewRisk": "N/A", "tossBias": "N/A", "tossTrend": "N/A",
            "ppScoreAvg": "N/A", "ppRunRate": "N/A", "spinIndex": "N/A", "spinNote": "N/A",
            "verdict": "⚠️ NO STANDARD T20 MATCHES TODAY", "badgeClass": "badge-yellow",
            "strategyText": "No major T20s found. System respects the Rules of Trading. Rest day."
        })

    output = {"last_updated": str(datetime.now(timezone.utc)), "matches": live_matches}
    with open("intel.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=4)
        
    print(f"Fusion Complete. Saved {len(live_matches)} matches.")

if __name__ == "__main__":
    run_scanner()
