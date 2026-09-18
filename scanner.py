import json, requests, datetime
from bs4 import BeautifulSoup

# Deep Venue Database
VENUE_DEEP_DB = {
    "Kensington Oval": {"weather": "28°C, Humid", "dew": "Zero Dew (Breezy)", "toss": "62% Chasing Wins", "toss_trend": "Toss Winner bowls first", "pp_score": "41 / 2 (Low Pace)", "pp_rr": "6.8 RPO", "spin_idx": "68% Wickets to Spin", "spin_note": "Grip in Overs 7-14 is severe", "verdict": "🟢 HIGH CONVICTION: SPIN SQUEEZE", "badge": "badge-green", "strategy": "Slow deck, zero dew risk. Lay favorite at 1.30."},
    "Old Trafford": {"weather": "17°C, Clear", "dew": "Moderate Evening Dew", "toss": "51% Batting 1st Wins", "toss_trend": "Even toss impact", "pp_score": "54 / 1", "pp_rr": "9.0 RPO", "spin_idx": "38% Wickets to Spin", "spin_note": "Flat track with true bounce", "verdict": "🟡 CAUTION: DUAL ENTRY MANDATORY", "badge": "badge-yellow", "strategy": "High-scoring flat track. Use Dual Entry Helper (1.35 & 1.25)."},
    "Providence": {"weather": "30°C, Overcast", "dew": "Low Dew", "toss": "55% Batting 1st Wins", "toss_trend": "Pitch slows down in 2nd Innings", "pp_score": "45 / 1", "pp_rr": "7.5 RPO", "spin_idx": "72% Wickets to Spin", "spin_note": "Extreme grip for spinners", "verdict": "🟢 HIGH CONVICTION: SPIN SQUEEZE", "badge": "badge-green", "strategy": "Sluggish deck. Spinners dictate terms."},
    "DEFAULT": {"weather": "25°C, Clear", "dew": "Unknown", "toss": "50-50 Bias", "toss_trend": "Neutral", "pp_score": "48 / 1", "pp_rr": "8.0 RPO", "spin_idx": "50% Wickets to Spin", "spin_note": "Standard T20 Pitch", "verdict": "🟡 AVERAGE TRACK: PLAY CAREFULLY", "badge": "badge-yellow", "strategy": "Read the game in powerplay before taking action."}
}

def get_venue_deep_stats(venue_name):
    for ground, stats in VENUE_DEEP_DB.items():
        if ground.lower() in venue_name.lower():
            return stats
    return VENUE_DEEP_DB["DEFAULT"]

def run_scanner():
    url = "https://www.cricbuzz.com/cricket-schedule/upcoming-series/all"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    premium_leagues = ["cpl", "ipl", "bbl", "psl", "sa20", "t20i", "t20 blast", "hundred"]
    live_matches = []

    try:
        res = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, "html.parser")
        blocks = soup.find_all("div", class_="cb-col-100 cb-col")
        
        for block in blocks:
            text = block.get_text()
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            
            if len(lines) >= 2 and " vs " in lines[0]:
                teamA = lines[0].split(" vs ")[0].strip()
                teamB = lines[0].split(" vs ")[1].split(",")[0].strip()
                venue = lines[1].strip()
                
                is_premium = any(league in text.lower() for league in premium_leagues)
                
                if is_premium:
                    stats = get_venue_deep_stats(venue)
                else:
                    # THE REJECTION PROFILE
                    stats = {
                        "weather": "N/A", "dew": "N/A", "toss": "Unknown", "toss_trend": "Unpredictable",
                        "pp_score": "Wildcard", "pp_rr": "Erratic", "spin_idx": "Low Volume", "spin_note": "Liquidity Trap",
                        "verdict": "🔴 REJECTED: JUNK LEAGUE / LOW LIQUIDITY", "badge": "badge-red",
                        "strategy": "TRAP DETECTED! This match failed Rule #5. Market liquidity will be dead. Stop-Loss orders won't execute. DO NOT TRADE."
                    }
                
                if not any(m['teamA'] == teamA for m in live_matches):
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
        print(f"Error scraping: {e}")

    # THE FAILSAFE: If Github IP gets blocked, force inject today's active junk matches!
    if len(live_matches) == 0:
        print("Cricbuzz blocked GitHub IP! Injecting Failsafe Matches...")
        live_matches = [
            {
                "teamA": "India Women", "teamB": "Japan Women", "venue": "Asian Games (Pingfeng Campus)",
                "weather": "N/A", "dewRisk": "N/A", "tossBias": "Unknown", "tossTrend": "Unpredictable",
                "ppScoreAvg": "Wildcard", "ppRunRate": "Erratic", "spinIndex": "Low Volume", "spinNote": "Liquidity Trap",
                "verdict": "🔴 REJECTED: MASSIVE SKILL GAP", "badgeClass": "badge-red",
                "strategyText": "TRAP DETECTED! Odds locked at 1.01. Lay orders will not match. DO NOT TRADE."
            },
            {
                "teamA": "Kenya", "teamB": "Uganda", "venue": "Africa Continental Cup",
                "weather": "N/A", "dewRisk": "N/A", "tossBias": "Unknown", "tossTrend": "Unpredictable",
                "ppScoreAvg": "Wildcard", "ppRunRate": "Erratic", "spinIndex": "Low Volume", "spinNote": "Liquidity Trap",
                "verdict": "🔴 REJECTED: ZERO LIQUIDITY", "badgeClass": "badge-red",
                "strategyText": "TRAP DETECTED! No public money on exchange. Auto Cut-Loss will fail. DO NOT TRADE."
            }
        ]

    output = {
        "last_updated": str(datetime.datetime.now()),
        "matches": live_matches
    }
    with open("intel.json", "w") as f:
        json.dump(output, f, indent=4)
    print(f"Scanned & Saved {len(live_matches)} Matches.")

if __name__ == "__main__":
    run_scanner()
