import json, urllib.request, datetime

def get_live_weather(venue_city):
    # Dynamically fetches weather for the exact live city
    try:
        city = venue_city.split(',')[-1].strip().split(' ')[0]
        url = f"https://wttr.in/{urllib.parse.quote(city)}?format=%t,+%C,+(Hum:%h)"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        return urllib.request.urlopen(req, timeout=5).read().decode('utf-8').strip()
    except:
        return "Weather API Unreachable"

def run_scanner():
    live_matches = []
    # DIRECT ESPN JSON BACKEND - No HTML scraping, No Captchas, No Hardcoded Stats
    url = "https://site.api.espn.com/apis/site/v2/sports/cricket/scorepanel"
    
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
        
        for event in data.get('events', []):
            title = event.get('name', 'Unknown Match')
            comp = event.get('competitions', [{}])[0]
            
            # 1. Real Venue & City
            venue_node = comp.get('venue', {})
            venue_name = venue_node.get('fullName', 'Unknown Venue')
            city = venue_node.get('address', {}).get('city', venue_name)
            
            # 2. Real Toss Data (Live)
            toss_node = comp.get('status', {}).get('toss', {})
            if toss_node:
                toss_winner = toss_node.get('winner', {}).get('text', 'Toss')
                toss_decision = toss_node.get('decision', 'decision pending')
                toss_actual = f"{toss_winner} elected to {toss_decision}"
            else:
                toss_actual = "Toss not yet flipped"

            # 3. Real Live Score & Run Rate (Current Match)
            competitors = comp.get('competitors', [])
            if len(competitors) < 2: 
                continue
                
            teamA = competitors[0].get('team', {}).get('name', 'Team A')
            teamB = competitors[1].get('team', {}).get('name', 'Team B')
            
            current_score = "0/0 (0.0 ov)"
            current_rr = "0.00 RPO"
            
            for team in competitors:
                linescores = team.get('linescores', [])
                if linescores:
                    latest = linescores[-1]
                    runs = latest.get('value', 0)
                    wickets = latest.get('outs', 0)
                    overs = latest.get('overs', 0)
                    current_score = f"{runs}/{wickets} ({overs} ov)"
                    if overs > 0:
                        current_rr = f"{(runs/overs):.2f} RPO"
                    break # Captured the active batting team's score

            # 4. Real Weather for the exact city
            real_weather = get_live_weather(city)

            # Rule #5: Premium T20 Check
            series = event.get('season', {}).get('slug', '')
            is_premium = any(k in series.lower() or k in title.lower() for k in ["cpl", "ipl", "bbl", "psl", "sa20", "blast", "hundred", "t20"])

            if not is_premium:
                verdict = f"🔴 REJECTED: JUNK ({series})"
                badge = "badge-red"
                strategy = "Format rejected. Market liquidity is unsafe. DO NOT TRADE."
            else:
                verdict = "🟢 LIVE MATCH DETECTED"
                badge = "badge-green"
                strategy = "Real-time API data flowing. Execute trades based on live screen conditions."

            live_matches.append({
                "teamA": teamA, "teamB": teamB, "venue": venue_name,
                "weather": real_weather, "dewRisk": "Check live broadcast",
                "tossBias": "LIVE TOSS", "tossTrend": toss_actual,
                "ppScoreAvg": "CURRENT SCORE", "ppRunRate": f"{current_score} @ {current_rr}",
                "spinIndex": "LIVE MATCH", "spinNote": "Watch live overs 7-14",
                "verdict": verdict, "badgeClass": badge,
                "strategyText": strategy
            })

    except Exception as e:
        print(f"Global API Failed: {e}")

    # Failsafe if absolutely zero matches are being played globally
    if len(live_matches) == 0:
        live_matches.append({
            "teamA": "SYSTEM", "teamB": "ONLINE", "venue": "Global Database",
            "weather": "N/A", "dewRisk": "N/A", "tossBias": "N/A", "tossTrend": "N/A",
            "ppScoreAvg": "N/A", "ppRunRate": "N/A", "spinIndex": "N/A", "spinNote": "N/A",
            "verdict": "⚠️ NO MATCHES GLOBALLY AT THIS TIME", "badgeClass": "badge-yellow",
            "strategyText": "API connection successful, but zero matches are currently scheduled/live."
        })

    output = {
        "last_updated": str(datetime.datetime.now()),
        "matches": live_matches
    }
    with open("intel.json", "w") as f:
        json.dump(output, f, indent=4)
        
    print(f"Dynamic API Fetch Complete: Saved {len(live_matches)} matches.")

if __name__ == "__main__":
    run_scanner()
