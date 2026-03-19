from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import requests
import os
from datetime import datetime


app = Flask(__name__)
CORS(app)

ODDS_API_KEY = "2e480f386f26b6d831d544cf01c96ff6"
THUNDERPICK_BASE = "https://thunderpick.io/api"
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Origin": "https://thunderpick.io",
    "Referer": "https://thunderpick.io/en/sports/hockey",
    "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Connection": "keep-alive",
}

SPORT_CONFIG = {
    "nba": {"competition_id": 263, "game_id": 11, "odds_key": "basketball_nba"},
    "nhl": {"competition_id": 300, "game_id": 14, "odds_key": "icehockey_nhl"},
}

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

def to_american(decimal):
    if decimal >= 2.0:
        return f"+{int((decimal - 1) * 100)}"
    return str(int(-100 / (decimal - 1)))

def calc_matched(stake, back_dec, lay_dec):
    lay_stake = round(stake * back_dec / lay_dec, 2)
    profit_back_wins = round(stake * back_dec - stake - lay_stake, 2)
    profit_lay_wins = round(lay_stake * lay_dec - lay_stake - stake, 2)
    profit = round(min(profit_back_wins, profit_lay_wins) + stake, 2)
    conversion = round(profit / stake * 100, 1)
    return {"lay_stake": lay_stake, "profit": profit, "conversion": conversion}

def get_thunderpick_matches(game_id, competition_id):
    r = requests.post(f"{THUNDERPICK_BASE}/matches", json={"gameIds": [game_id]}, headers=HEADERS)
    r.raise_for_status()
    data = r.json()["data"]
    all_matches = data.get("upcoming", []) + data.get("live", [])
    return [m for m in all_matches if m.get("competition", {}).get("id") == competition_id]

def get_thunderpick_odds(market_ids):
    all_matches = []
    all_markets = []
    batch_size = 10
    for i in range(0, len(market_ids), batch_size):
        batch = market_ids[i:i + batch_size]
        params = [("marketsIds", mid) for mid in batch]
        r = requests.get(f"{THUNDERPICK_BASE}/matches/with-markets-by-ids", headers=HEADERS, params=params)
        if r.status_code == 200:
            data = r.json()["data"]
            all_matches.extend(data.get("matches", []))
            all_markets.extend(data.get("markets", []))

    seen_ids = set()
    deduped = []
    for m in all_matches:
        if m["id"] not in seen_ids:
            seen_ids.add(m["id"])
            deduped.append(m)

    return (deduped, all_markets)

def get_sportsbook_h2h(odds_key):
    r = requests.get(
        f"{ODDS_API_BASE}/sports/{odds_key}/odds",
        params={
            "apiKey": ODDS_API_KEY,
            "bookmakers": "fanduel,betrivers",
            "markets": "h2h",
            "oddsFormat": "decimal"
        }
    )
    r.raise_for_status()
    h2h = {}
    for game in r.json():
        for bookie in game.get("bookmakers", []):
            book = bookie["title"]
            for market in bookie.get("markets", []):
                if market["key"] == "h2h":
                    for outcome in market["outcomes"]:
                        name = outcome["name"]
                        if name not in h2h:
                            h2h[name] = {}
                        h2h[name][book] = outcome["price"]
    return h2h

def fuzzy_match_h2h(team_name, h2h):
    team_lower = team_name.lower()
    for name, books in h2h.items():
        if any(word in name.lower() for word in team_lower.split() if len(word) > 3):
            return books
    return None

def dedup(rows):
    seen = set()
    out = []
    for row in sorted(rows, key=lambda x: -x["conversion"]):
        key = (row["matchup"], row["back_team"])
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out

import logging
logging.basicConfig(level=logging.DEBUG)

@app.route("/api/odds")
def get_odds():
    stake = float(request.args.get("stake", 1000))
    sport = request.args.get("sport", "nba")

    if sport not in SPORT_CONFIG:
        return jsonify({"error": f"Unknown sport: {sport}"}), 400

    cfg = SPORT_CONFIG[sport]

    try:
        tp_matches = get_thunderpick_matches(cfg["game_id"], cfg["competition_id"])

        if sport == "nhl":
            # NHL: preferredMarkets is empty, use 1X2 market ID to fetch overtime market
            market_ids = [m["market"]["id"] - 1 for m in tp_matches if m.get("market")]
        else:
            market_ids = [m["id"] for match in tp_matches for m in match.get("preferredMarkets", [])]

        tp_matches_data, tp_markets = get_thunderpick_odds(market_ids) if market_ids else ([], [])

        # NHL: build overtime odds lookup by event ID
        overtime_lookup = {}
        if sport == "nhl":
            for mk in tp_markets:
                if "overtime" in mk.get("name", "").lower():
                    event_id = mk["eventId"]
                    home_sel = next((s for s in mk["selections"] if s["type"] == "home"), None)
                    away_sel = next((s for s in mk["selections"] if s["type"] == "away"), None)
                    if home_sel and away_sel:
                        overtime_lookup[event_id] = {
                            "home": {"name": home_sel["name"], "odds": home_sel["odds"]},
                            "away": {"name": away_sel["name"], "odds": away_sel["odds"]},
                        }
        print(f"NHL overtime lookup keys: {list(overtime_lookup.keys())}")
        print(f"NHL tp_markets count: {len(tp_markets)}")
        for mk in tp_markets:
            print(f"  market name: {mk.get('name')}")

        h2h_odds = get_sportsbook_h2h(cfg["odds_key"])
    except Exception as e:
        import traceback
        print("ERROR:", traceback.format_exc())
        return jsonify({"error": str(e)}), 500

    rows = []
    for m in tp_matches_data:
        home = m["teams"]["home"]["name"]
        away = m["teams"]["away"]["name"]
        start_iso = m.get("startTime", "")

        try:
            dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            time_str = dt.astimezone().strftime("%-I:%M %p")
        except Exception:
            time_str = "—"

        # get the right odds source depending on sport
        if sport == "nhl":
            ot = overtime_lookup.get(m["id"])
            if not ot:
                continue
            sides = [
                (ot["away"]["name"], ot["away"]["odds"], ot["home"]["name"]),
                (ot["home"]["name"], ot["home"]["odds"], ot["away"]["name"]),
            ]
        else:
            market = m.get("market")
            if not market:
                continue
            sides = [
                (away, market["away"]["odds"], home),
                (home, market["home"]["odds"], away),
            ]

        for back_team, back_dec, hedge_team in sides:
            book_odds = fuzzy_match_h2h(hedge_team, h2h_odds)
            if not book_odds:
                continue

            best_book = max(book_odds, key=book_odds.get)
            best_dec = book_odds[best_book]
            calc = calc_matched(stake, back_dec, best_dec)

            rows.append({
                "matchup": f"{away} vs {home}",
                "time": time_str,
                "back_team": back_team,
                "back_odds_tp": to_american(back_dec),
                "hedge_team": hedge_team,
                "fanduel": to_american(book_odds["FanDuel"]) if "FanDuel" in book_odds else None,
                "betrivers": to_american(book_odds["BetRivers"]) if "BetRivers" in book_odds else None,
                "best_book": best_book,
                "best_odds": to_american(best_dec),
                "hedge_stake": calc["lay_stake"],
                "profit": calc["profit"],
                "conversion": calc["conversion"],
            })

    return jsonify({"moneyline": dedup(rows), "stake": stake})
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port)