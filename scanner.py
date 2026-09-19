import json, re, math, zipfile
from pathlib import Path
from datetime import datetime, timezone
import requests
import xml.etree.ElementTree as ET

DATA_DIR = Path("./data")
HISTORY_DIR = DATA_DIR / "cricsheet_all"
CACHE_DIR = DATA_DIR / "cache"
CRICSHEET_URL = "https://cricsheet.org/downloads/all_json.zip"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "T20-PreMatch-Scanner/12.1"})

PREMIUM_KEYWORDS = [
    "ipl", "indian premier", "cpl", "caribbean", "bbl", "big bash", "psl", "pakistan super", 
    "sa20", "blast", "major league", "mlc", "ilt20", "lanka", "bpl", "super smash", "nepal"
]
MAJOR_TEAMS = ["england", "india", "australia", "sri lanka", "west indies", "south africa", "new zealand", "pakistan", "bangladesh", "afghanistan", "ireland", "zimbabwe"]
JUNK_WORDS = ["club", "university", "college", "academy", "regional", "minor", "county", "u19", "women's club"]

# ==========================================
# OPEN-METEO WEATHER ENGINE (DEW RISK)
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
        dew_str = calculate_dew_risk(temp, dp, rh, wind, precip)
        return wx_str, dew_str
    except: return "API Error", "Unknown"

# ==========================================
# CRICSHEET HISTORICAL MATH ENGINE
# ==========================================
def ensure_cricsheet_history():
    DATA_DIR.mkdir(exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    marker = HISTORY_DIR / ".ready"
    if marker.exists(): return
    print("[INFO] Downloading Cricsheet History...")
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
# THE OMNI-AGGREGATOR
# ==========================================
def run_scanner():
    ensure_cricsheet_history()
    t20_db = load_t20_history()
    live_matches = []
    seen_match_keys = set()

    def add_match(teamA, teamB, venue, combined_text, toss_str, score_str, rr_str):
        teamA, teamB = teamA.strip(), teamB.strip()
        match_key = f"{teamA} vs {teamB}".lower()
        if not teamA or not teamB or match_key in seen_match_keys: return
        
        c_text = combined_text.lower()
        if any(x in c_text for x in ["odi", "test", "one day", "one-day", "t10", "hundred"]): return
        
        is_franchise = any(k in c_text for k in PREMIUM_KEYWORDS)
        is_t20 = "t20" in c_text or "twenty20" in c_text or is_franchise
        if not is_t20: return
        
        seen_match_keys.add(match_key)
        
        is_intl = ("t20i" in c_text) and any(t in c_text for t in MAJOR_TEAMS)
        is_premium = is_franchise or is_intl
        
        if is_premium:
            # 💥 THE BUG FIX: Do NOT try to fetch weather/stats if venue is unknown
            if venue == "UNKNOWN VENUE":
                verdict, badge = "🟡 INSUFFICIENT VENUE DATA", "badge-yellow"
                strategy = "Match active via Fallback API. Venue is unknown. Rely strictly on live odds."
                wx_str, dew_str = "N/A", "N/A"
                toss_bias, pp_avg, spin_idx = "UNKNOWN", "UNKNOWN", "UNKNOWN"
            else:
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
            "teamA": teamA, "teamB": teamB, "venue": venue,
            "weather": wx_str, "dewRisk": dew_str,
            "tossBias": toss_bias, "tossTrend": toss_str,
            "ppScoreAvg": pp_avg, "ppRunRate": f"{score_str} @ {rr_str}",
            "spinIndex": spin_idx, "spinNote": "Match Radar Active",
            "verdict": verdict, "badgeClass": badge, "strategyText": strategy
        })

    # 1. ESPN
    try:
        espn_data = SESSION.get("https://site.api.espn.com/apis/site/v2/sports/cricket/scorepanel", timeout=15).json()
        for event in espn_data.get('events', []):
            comp = event.get('competitions', [{}])[0]
            title, series = event.get('name', ''), event.get('season', {}).get('slug', '')
            match_type = comp.get('type', {}).get('abbreviation', '')
            state = comp.get('status', {}).get('type', {}).get('state', 'pre') 
            if state == 'post': continue
                
            venue = comp.get('venue', {}).get('fullName', 'Unknown Venue')
            teams = [t.get('team', {}).get('name', '') for t in comp.get('competitors', [])]
            if len(teams) < 2: continue
            
            toss_str, score_str, rr_str = "Live Match", "0/0", "0.00 RPO"
            if state != 'pre':
                toss = comp.get('status', {}).get('toss', {})
                if toss: toss_str = f"{toss.get('winner', {}).get('text', '')} elected to {toss.get('decision', '')}"
                for c in comp.get('competitors', []):
                    ls = c.get('linescores', [])
                    if ls:
                        runs, wkts, overs = ls[-1].get('value', 0), ls[-1].get('outs', 0), ls[-1].get('overs', 0)
                        score_str, rr_str = f"{runs}/{wkts} ({overs} ov)", f"{(runs/overs):.2f} RPO" if overs > 0 else "0.00 RPO"
                        break
            add_match(teams[0], teams[1], venue, f"{title} {series} {match_type}", toss_str, score_str, rr_str)
    except: pass

    # 2. CRICBUZZ UNOFFICIAL API
    cb_urls = ["https://cricbuzz-live.vercel.app/v1/matches/live", "https://cricbuzz-live.vercel.app/v1/matches/upcoming?type=international", "https://cricbuzz-live.vercel.app/v1/matches/upcoming?type=league"]
    for url in cb_urls:
        try:
            cb_data = SESSION.get(url, timeout=15).json()
            match_list = []
            if "typeMatches" in cb_data:
                for tm in cb_data["typeMatches"]:
                    for sm in tm.get("seriesMatches", []):
                        if "seriesAdWrapper" in sm: match_list.extend(sm["seriesAdWrapper"].get("matches", []))
            elif "data" in cb_data: match_list = cb_data["data"].get("matches", [])
                
            for m in match_list:
                match_info = m.get("matchInfo", m)
                title = match_info.get("team1", {}).get("teamName", "") + " vs " + match_info.get("team2", {}).get("teamName", "")
                if title == " vs ": title = str(m.get("title", ""))
                if " vs " not in title: continue
                
                cb_teams = [x.strip() for x in title.split(" vs ")[:2]]
                series = str(match_info.get("seriesName", m.get("series", "")))
                venue = str(match_info.get("venueInfo", {}).get("ground", m.get("timeAndPlace", {}).get("place", "")))
                state = match_info.get("state", "Upcoming")
                if state in ["Complete", "Result"]: continue
                
                add_match(cb_teams[0], cb_teams[1], venue, f"{title} {series}", f"Status: {state}", "0/0", "0.00 RPO")
        except: pass

    # 3. CRICINFO RSS FALLBACK (Now with Honest Unknown Venue)
    try:
        xml_data = SESSION.get("http://static.cricinfo.com/rss/livescores.xml", timeout=10).text
        root = ET.fromstring(xml_data)
        for item in root.findall('./channel/item'):
            title = item.find('title').text
            if ' v ' in title:
                parts = title.split(' v ')
                teamA, teamB = re.sub(r'[0-9/\*\(\)]+', '', parts[0]).strip(), re.sub(r'[0-9/\*\(\)]+', '', parts[1]).strip()
                # BUG FIX: Passing "UNKNOWN VENUE" instead of "Live Market"
                add_match(teamA, teamB, "UNKNOWN VENUE", title, "Toss Unknown", "0/0", "0.00 RPO")
    except: pass

    if not live_matches:
        live_matches.append({
            "teamA": "SYSTEM", "teamB": "ONLINE", "venue": "Global Database",
            "weather": "N/A", "dewRisk": "N/A", "tossBias": "N/A", "tossTrend": "N/A",
            "ppScoreAvg": "N/A", "ppRunRate": "N/A", "spinIndex": "N/A", "spinNote": "N/A",
            "verdict": "⚠️ NO STANDARD T20 MATCHES TODAY", "badgeClass": "badge-yellow",
            "strategyText": "No major T20s found across 3 different APIs. Rest day."
        })

    with open("intel.json", "w", encoding="utf-8") as f:
        json.dump({"last_updated": str(datetime.now(timezone.utc)), "matches": live_matches}, f, indent=4)
    print(f"OMNI-SCAN COMPLETE. Saved {len(live_matches)} matches.")

if __name__ == "__main__":
    run_scanner()
