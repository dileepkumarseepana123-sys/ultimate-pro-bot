import json, requests, datetime

# Deep Venue Database
VENUE_DEEP_DB = {
    "Old Trafford": {"weather": "17°C, Clear", "dew": "Moderate Evening Dew", "toss": "51% Batting 1st Wins", "toss_trend": "Even toss impact", "pp_score": "54 / 1", "pp_rr": "9.0 RPO", "spin_idx": "38% Wickets to Spin", "spin_note": "Flat track with true bounce", "verdict": "🟡 CAUTION: DUAL ENTRY MANDATORY", "badge": "badge-yellow", "strategy": "High-scoring flat track. Do not Lay at 1.30 with single stake. Use the Dual Entry Helper (1.35 and 1.25)."},
    "Kensington Oval": {"weather": "28°C, Humid", "dew": "Zero Dew (Breezy)", "toss": "62% Chasing Wins", "toss_trend": "Toss Winner bowls first", "pp_score": "41 / 2 (Low Pace)", "pp_rr": "6.8 RPO", "spin_idx": "68% Wickets to Spin", "spin_note": "Grip in Overs 7-14 is severe", "verdict": "🟢 HIGH CONVICTION: SPIN SQUEEZE", "badge": "badge-green", "strategy": "Slow deck, zero dew risk. Lay favorite at 1.30."},
    "DEFAULT": {"weather": "25°C, Clear", "dew": "Unknown", "toss": "50-50 Bias", "toss_trend": "Neutral", "pp_score": "48 / 1", "pp_rr": "8.0 RPO", "spin_idx": "50% Wickets to Spin", "spin_note": "Standard T20 Pitch", "verdict": "🟡 AVERAGE TRACK: PLAY CAREFULLY", "badge": "badge-yellow", "strategy": "Read the game in powerplay before taking action."}
}

def get_venue_deep_stats(match_title, venue_name):
    combined = f"{match_title} {venue_name}".lower()
    for ground, stats in VENUE_DEEP_DB.items():
        if ground.lower() in combined:
            return stats
    return VENUE_DEEP_DB["DEFAULT"]

def run_scanner():
    # THE RESEARCHED ENDPOINT: Cricinfo's highly stable Mobile App API
    api_url = "https://hs-consumer-api.espncricinfo.com/v1/pages/matches/current?lang=en&v=1"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    
    premium_leagues = ["cpl", "ipl", "bbl", "psl", "sa20", "t20 blast", "hundred", "t20i", "t20"]
    live_matches = []

    try:
        print("Fetching data from Cricinfo Consumer API...")
        res = requests.get(api_url, headers=headers, timeout=15)
        res.raise_for_status() 
        
        data = res.json()
        matches = data.get("matches", [])
        print(f"API Connected! Found {len(matches)} active global matches.")
        
        for m in matches:
            try:
                # Safely parsing the Cricinfo JSON structure
                teams = m.get("teams", [])
                if len(teams) < 2:
                    continue
                    
                teamA = teams[0].get("team", {}).get("name", "Team A")
                teamB = teams[1].get("team", {}).get("name", "Team B")
                
                venue = m.get("ground", {}).get("name", "Unknown Venue")
                series_name = m.get("series", {}).get("name", "Unknown Series")
                match_title = f"{teamA} vs {teamB}"
                
                # Rule #5 Liquidity check
                is_premium = any(l in series_name.lower() or l in match_title.lower() for l in premium_leagues)
                
                if is_premium:
                    stats = get_venue_deep_stats(match_title, venue)
                else:
                    stats = {
                        "weather": "N/A", "dew": "N/A", "toss": "Unknown", "toss_trend": "Unpredictable",
                        "pp_score": "Wildcard", "pp_rr": "Erratic", "spin_idx": "Low Volume", "spin_note": "Liquidity Trap",
                        "verdict": f"🔴 REJECTED: JUNK ({series_name})", "badge": "badge-red",
                        "strategy": "TRAP DETECTED! Market liquidity will be dead. Stop-Loss orders won't execute. DO NOT TRADE."
                    }
                
                live_matches.append({
                    "teamA": teamA, "teamB": teamB, "venue": venue,
                    "weather": stats["weather"], "dewRisk": stats["dew"],
                    "tossBias": stats["toss"], "tossTrend": stats["toss_trend"],
                    "ppScoreAvg": stats["pp_score"], "ppRunRate": stats["pp_rr"],
                    "spinIndex": stats["spin_idx"], "spinNote": stats["spin_note"],
                    "verdict": stats["verdict"], "badgeClass": stats["badge"],
                    "strategyText": stats["strategy"]
                })
            except Exception as e:
                print(f"Skipped a match due to parsing error: {e}")
                continue
                
    except Exception as e:
        print(f"CRITICAL API FAILURE: {e}")

    # Output generation
    output = {
        "last_updated": str(datetime.datetime.now()),
        "matches": live_matches
    }
    
    with open("intel.json", "w") as f:
        json.dump(output, f, indent=4)
        
    print(f"Successfully Scanned & Saved {len(live_matches)} Live Matches.")

if __name__ == "__main__":
    run_scanner()
