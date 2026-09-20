import json, re, zipfile
from pathlib import Path
from datetime import datetime, timezone
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
SESSION.headers.update({"User-Agent": "Pro-Terminal/Web-Agent-V15"})

# PREMIUM LEAGUES & TEAMS
PREMIUM_KEYWORDS = ["ipl", "cpl", "bbl", "psl", "sa20", "blast", "mlc", "ilt20", "lanka", "bpl", "super smash", "nepal", "guyana", "jamaica", "barbados", "lucia", "trinbago", "kitts", "antigua", "falcons"]
MAJOR_TEAMS = ["england", "india", "australia", "sri lanka", "west indies", "south africa", "new zealand", "pakistan", "bangladesh", "afghanistan", "ireland", "zimbabwe"]
JUNK_WORDS = ["test", "odi", "one day", "t10", "hundred", "women's club", "under-19", "u19", "odc", "county"]

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
# BROWSER AUTOMATION ENGINE (PLAYWRIGHT PURE)
# ==========================================
def scrape_cricbuzz():
    matches_found = []
    print("[INFO] Launching Standard Browser to Scrape Matches...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            
            # Using a normal browser identity (Bypasses the need for stealth_sync)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            
            # Going straight to the T20 matches tab
            page.goto("https://www.cricbuzz.com/cricket-schedule/upcoming-series/t20", timeout=60000)
            page.wait_for_timeout(3000) # Wait for page to render
            
            soup = BeautifulSoup(page.content(), "html.parser")
            
            for a_tag in soup.find_all("a", href=re.compile(r"live-cricket-scores")):
                title = str(a_tag.get("title", ""))
                if " vs " in title or " v " in title:
                    clean_title = title.replace(" - Live Cricket Score", "")
                    
                    teamA, teamB = "Team A", "Team B"
                    parts = clean_title.split(",")[0].split(" vs ") if " vs " in clean_title else clean_title.split(",")[0].split(" v ")
                    if len(parts) >= 2:
                        teamA, teamB = parts[0].strip(), parts[1].strip()
                        
                    venue = "UNKNOWN VENUE"
                    parent = a_tag.find_parent("div", class_="cb-col")
                    if parent:
                        venue_div = parent.find("div", class_="text-gray")
                        if venue_div:
                            venue_text = venue_div.text.strip()
                            venue = venue_text.split("•")[0].strip() if "•" in venue_text else venue_text
                            
                    matches_found.append({
                        "teamA": teamA, "teamB": teamB,
                        "title": clean_title, "venue": venue, "status": "Scheduled/Live"
                    })
                    
            browser.close()
    except Exception as e:
        print(f"[ERROR] Web Scraping Failed: {e}")
        
    return matches_found

# ==========================================
# CORE TERMINAL LOGIC
# ==========================================
def run_scanner():
    ensure_cricsheet_history()
    t20_db = load_t20_history()
    live_matches = []
    seen = set()
    
    scraped_data = scrape_cricbuzz()
    
    for m in scraped_data:
        teamA = m["teamA"]
        teamB = m["teamB"]
        title = m["title"].lower()
        venue = m["venue"]
        
        match_key = f"{teamA} vs {teamB}".lower()
        if match_key in seen: continue
        seen.add(match_key)
        
        # Checking Rules to Accept or Reject
        if any(j in title for j in JUNK_WORDS):
            verdict, badge = "🔴 REJECTED: JUNK MATCH", "badge-red"
            strategy = "Match format is not standard T20 (ODI/Test/T10). System ignored."
            is_premium = False
        else:
            is_franchise = any(k in title for k in PREMIUM_KEYWORDS)
            is_intl = any(t in title for t in MAJOR_TEAMS)
            is_premium = is_franchise or is_intl
            
            if is_premium:
                if venue == "UNKNOWN VENUE":
                    verdict, badge = "🟡 INSUFFICIENT VENUE DATA", "badge-yellow"
                    strategy = "Match is premium, but scraper couldn't read the stadium name. No math generated."
                else:
                    verdict, badge = "🟢 PRO DATA VERIFIED", "badge-green"
                    strategy = "Premium Match. Analyzing Venue Database."
            else:
                verdict, badge = "🔴 REJECTED: LOW LIQUIDITY", "badge-red"
                strategy = "Minor domestic league or unknown teams. Stop-loss will fail. DO NOT TRADE."
        
        # Stats generation only for Valid Premium Venues
        if is_premium and venue != "UNKNOWN VENUE":
            wx_str, dew_str = get_live_weather(venue)
            v_stats = analyze_venue(t20_db, venue)
            if v_stats:
                toss_bias, pp_avg, spin_idx = v_stats['toss_bias'], v_stats['pp_avg'], v_stats['spin_idx']
                strategy = f"Cricsheet History ({v_stats['matches']} matches). Trade safely."
            else:
                verdict, badge = "🟡 INSUFFICIENT VENUE DATA", "badge-yellow"
                strategy = "Less than 5 historical T20s found here. Rely strictly on live odds."
                toss_bias, pp_avg, spin_idx = "UNKNOWN", "UNKNOWN", "UNKNOWN"
        else:
            wx_str, dew_str, toss_bias, pp_avg, spin_idx = "N/A", "N/A", "N/A", "N/A", "N/A"
            
        live_matches.append({
            "teamA": teamA, "teamB": teamB, "venue": venue,
            "weather": wx_str, "dewRisk": dew_str,
            "tossBias": toss_bias, "tossTrend": m["status"],
            "ppScoreAvg": pp_avg, "ppRunRate": "Scan Active",
            "spinIndex": spin_idx, "spinNote": "Radar Online",
            "verdict": verdict, "badgeClass": badge, "strategyText": strategy
        })

    # Failsafe if absolutely nothing is scraped
    if not live_matches:
        live_matches.append({
            "teamA": "SYSTEM", "teamB": "ONLINE", "venue": "Web Scraper Output",
            "weather": "N/A", "dewRisk": "N/A", "tossBias": "N/A", "tossTrend": "N/A",
            "ppScoreAvg": "N/A", "ppRunRate": "N/A", "spinIndex": "N/A", "spinNote": "N/A",
            "verdict": "⚠️ SCRAPER RETURNED EMPTY", "badgeClass": "badge-yellow",
            "strategyText": "Web Agent found no fixtures on schedule. Rest day."
        })

    with open("intel.json", "w", encoding="utf-8") as f:
        json.dump({"last_updated": str(datetime.now(timezone.utc)), "matches": live_matches}, f, indent=4)
    print(f"Scrape Complete. Saved {len(live_matches)} matches.")

if __name__ == "__main__":
    run_scanner()
