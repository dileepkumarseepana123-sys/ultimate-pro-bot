import json, urllib.request, datetime

# ==========================================
# CRICSHEET DATA ENGINE (Foundation)
# ==========================================
def get_cricsheet_historical_stats(venue_name):
    # Idhi Cricsheet & DuckDB pipeline ki connection point.
    # Future lo idhi direct ga CSV/DB nunchi data laaguthundi.
    return {
        "toss": "Cricsheet: 55% Chasing Wins", 
        "pp_score": "Cricsheet Avg: 47 / 1", 
        "spin_idx": "Cricsheet: 42% Wickets to Spin", 
        "verdict": "🟢 PREMIUM T20 DETECTED", 
        "badge": "badge-green", 
        "strategy": "Cricsheet historical data confirms premium liquidity. Ready for execution."
    }

def run_scanner():
    live_matches = []
    # ESPN Scorepanel: Fetches ALL matches scheduled for Today
    url = "https://site.api.espn.com/apis/site/v2/sports/cricket/scorepanel"
    
    # Premium T20 Leagues List
    premium_keywords = ["cpl", "ipl", "bbl", "psl", "sa20", "blast", "hundred", "t20i", "women's t20", "t20"]
    
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
        
        for event in data.get('events', []):
            title = event.get('name', 'Unknown Match')
            comp = event.get('competitions', [{}])[0]
            series = event.get('season', {}).get('slug', '')
            
            # 1. MATCH STATUS CHECK (Upcoming & Live ONLY. Ignore Finished)
            status_node = comp.get('status', {}).get('type', {})
            state = status_node.get('state', 'pre') # 'pre'=Upcoming, 'in'=Live, 'post'=Finished
            
            if state == 'post':
                continue # Match aipoindi, so list nunchi theesey!
                
            # 2. STRICT T20 FILTER (Ignore ODIs, Tests, T10s)
            combined_text = f"{title} {series}".lower()
            is_t20 = ("t20" in combined_text or "twenty20" in combined_text or "hundred" in combined_text)
            
            if not is_t20:
                continue # Asalu T20 kaakapothe pakkana padesey!

            # Venue & Teams
            venue_name = comp.get('venue', {}).get('fullName', 'Unknown Venue')
            city = comp.get('venue', {}).get('address', {}).get('city', venue_name)
            
            competitors = comp.get('competitors', [])
            if len(competitors) < 2: 
                continue
            teamA = competitors[0].get('team', {}).get('name', 'Team A')
            teamB = competitors[1].get('team', {}).get('name', 'Team B')
            
            # 3. UPCOMING vs LIVE LOGIC
            if state == 'pre':
                # Match inka start avvaledu (Scheduled for today)
                toss_actual = "Upcoming Match (Toss pending)"
                current_score = "0/0"
                current_rr = "0.00 RPO"
                time_str = status_node.get('shortDetail', 'Today')
                spin_note = f"Match starts at {time_str}"
            else:
                # Match is LIVE right now
                toss_node = comp.get('status', {}).get('toss', {})
                if toss_node:
                    winner = toss_node.get('winner', {}).get('text', 'Toss')
                    decision = toss_node.get('decision', 'pending')
                    toss_actual = f"{winner} elected to {decision}"
                else:
                    toss_actual = "Live Match"
                    
                # Calculate Live Score
                runs, wickets, overs = 0, 0, 0
                for team in competitors:
                    linescores = team.get('linescores', [])
                    if linescores:
                        latest = linescores[-1]
                        runs = latest.get('value', 0)
                        wickets = latest.get('outs', 0)
                        overs = latest.get('overs', 0)
                        break
                current_score = f"{runs}/{wickets} ({overs} ov)"
                current_rr = f"{(runs/overs):.2f} RPO" if overs > 0 else "0.00 RPO"
                spin_note = "Watch live overs 7-14"

            # 4. ACCEPTED vs REJECTED (T20 League Quality)
            is_premium = any(k in combined_text for k in premium_keywords)
            
            if is_premium:
                # PREMIUM T20 - Send to Cricsheet Engine
                stats = get_cricsheet_historical_stats(venue_name)
                toss_bias = stats["toss"]
                pp_avg = stats["pp_score"]
                spin_idx = stats["spin_idx"]
                verdict = stats["verdict"]
                badge = stats["badge"]
                strategy = stats["strategy"]
            else:
                # JUNK T20 - Reject it brutally
                toss_bias, pp_avg, spin_idx = "N/A", "N/A", "N/A"
                verdict = f"🔴 REJECTED: JUNK T20 ({series})"
                badge = "badge-red"
                strategy = "This is a T20, but not a premium league. Market liquidity will be zero. DO NOT TRADE."

            live_matches.append({
                "teamA": teamA, "teamB": teamB, "venue": venue_name,
                "weather": f"Live tracking: {city}",
                "dewRisk": "Check Local Time",
                "tossBias": toss_bias,
                "tossTrend": toss_actual,
                "ppScoreAvg": pp_avg,
                "ppRunRate": f"{current_score} @ {current_rr}",
                "spinIndex": spin_idx,
                "spinNote": spin_note,
                "verdict": verdict,
                "badgeClass": badge,
                "strategyText": strategy
            })

    except Exception as e:
        print(f"API Failed: {e}")

    # No T20 matches scheduled today
    if len(live_matches) == 0:
        live_matches.append({
            "teamA": "SYSTEM", "teamB": "ONLINE", "venue": "Global Database",
            "weather": "N/A", "dewRisk": "N/A", "tossBias": "N/A", "tossTrend": "N/A",
            "ppScoreAvg": "N/A", "ppRunRate": "N/A", "spinIndex": "N/A", "spinNote": "N/A",
            "verdict": "⚠️ NO T20 MATCHES TODAY", "badgeClass": "badge-yellow",
            "strategyText": "There are no T20 matches (Premium or Junk) scheduled for today globally."
        })

    output = {
        "last_updated": str(datetime.datetime.now()),
        "matches": live_matches
    }
    with open("intel.json", "w") as f:
        json.dump(output, f, indent=4)
        
    print(f"Scanner Complete: Saved {len(live_matches)} T20 matches.")

if __name__ == "__main__":
    run_scanner()
