from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import requests
import json
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

THUNDERPICK_BASE = "https://thunderpick.io/api"
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Content-Type": "application/json"
}

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

def to_decimal(american):
    if american < 0:
        return 1 + (100 / abs(american))
    return 1 + (american / 100)

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
    return {
        "lay_stake": lay_stake,
        "profit": profit,
        "conversion": conversion
    }

def get_thunderpick_nba():
    r = requests.post(f"{THUNDERPICK_BASE}/matches", json={"gameIds": [11]}, headers=HEADERS)
    r.raise_for_status()
    data = r.json()["data"]
    all_matches = data.get("upcoming", []) + data.get("live", [])
    return [m for m in all_matches if m.get("competition", {}).get("id") == 263]

def get_thunderpick_odds(market_ids):
    params = [("marketsIds", mid) for mid in market_ids]
    r = requests.get(f"{THUNDERPICK_BASE}/matches/with-markets-by-ids", headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json()["data"]["matches"]

def get_sportsbook_odds(api_key):
    r = requests.get(
        f"{ODDS_API_BASE}/sports/basketball_nba/odds",
        params={
            "apiKey": api_key,
            "bookmakers": "fanduel,betrivers",
            "markets": "h2h,alternate_spreads",
            "oddsFormat": "decimal"
        }
    )
    r.raise_for_status()

    h2h = {}
    spreads = {}

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
                elif market["key"] == "alternate_spreads":
                    for outcome in market["outcomes"]:
                        name = outcome["name"]
                        line = outcome.get("point")
                        if line is None:
                            continue
                        key = (name, float(line))
                        if key not in spreads:
                            spreads[key] = {}
                        spreads[key][book] = outcome["price"]

    return h2h, spreads

def fuzzy_match_h2h(team_name, h2h):
    team_lower = team_name.lower()
    for name, books in h2h.items():
        if any(word in name.lower() for word in team_lower.split() if len(word) > 3):
            return books
    return None

def fuzzy_match_spread(team_name, line, spreads):
    team_lower = team_name.lower()
    for (name, spread_line), books in spreads.items():
        if abs(spread_line - line) < 0.01:
            if any(word in name.lower() for word in team_lower.split() if len(word) > 3):
                return books
    return None

@app.route("/api/odds")
def get_odds():
    api_key = request.args.get("apiKey", "")
    stake = float(request.args.get("stake", 1000))

    if not api_key:
        return jsonify({"error": "Missing Odds API key"}), 400

    try:
        tp_matches = get_thunderpick_nba()
        market_ids = [m["id"] for match in tp_matches for m in match.get("preferredMarkets", [])]
        tp_data = get_thunderpick_odds(market_ids)
        h2h_odds, spread_odds = get_sportsbook_odds(api_key)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    moneyline_rows = []
    spread_rows = []

    for m in tp_data:
        home = m["teams"]["home"]["name"]
        away = m["teams"]["away"]["name"]
        start_iso = m.get("startTime", "")

        try:
            dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            time_str = dt.astimezone().strftime("%-I:%M %p")
        except Exception:
            time_str = "—"

        market = m.get("market")
        if market:
            for back_team, back_dec, hedge_team in [
                (away, market["away"]["odds"], home),
                (home, market["home"]["odds"], away),
            ]:
                book_odds = fuzzy_match_h2h(hedge_team, h2h_odds)
                if not book_odds:
                    continue

                best_book = max(book_odds, key=book_odds.get)
                best_dec = book_odds[best_book]
                calc = calc_matched(stake, back_dec, best_dec)

                moneyline_rows.append({
                    "type": "moneyline",
                    "matchup": f"{away} vs {home}",
                    "time": time_str,
                    "back_team": back_team,
                    "back_odds_tp": to_american(back_dec),
                    "line": None,
                    "hedge_team": hedge_team,
                    "fanduel": to_american(book_odds["FanDuel"]) if "FanDuel" in book_odds else None,
                    "betrivers": to_american(book_odds["BetRivers"]) if "BetRivers" in book_odds else None,
                    "best_book": best_book,
                    "best_odds": to_american(best_dec),
                    "hedge_stake": calc["lay_stake"],
                    "profit": calc["profit"],
                    "conversion": calc["conversion"],
                })

        for pm in m.get("preferredMarkets", []):
            if pm.get("nickName") != "Point Handicap":
                continue

            for sel in pm.get("selections", []):
                back_team = sel["name"]
                back_dec = sel["odds"]
                tp_line = float(sel["handicap"])
                hedge_team = home if back_team == away else away
                hedge_line = -tp_line

                hedge_books = fuzzy_match_spread(hedge_team, hedge_line, spread_odds)
                if not hedge_books:
                    continue

                best_book = max(hedge_books, key=hedge_books.get)
                best_dec = hedge_books[best_book]
                calc = calc_matched(stake, back_dec, best_dec)

                spread_rows.append({
                    "type": "spread",
                    "matchup": f"{away} vs {home}",
                    "time": time_str,
                    "back_team": back_team,
                    "back_odds_tp": to_american(back_dec),
                    "line": tp_line,
                    "hedge_team": hedge_team,
                    "hedge_line": hedge_line,
                    "fanduel": to_american(hedge_books["FanDuel"]) if "FanDuel" in hedge_books else None,
                    "betrivers": to_american(hedge_books["BetRivers"]) if "BetRivers" in hedge_books else None,
                    "best_book": best_book,
                    "best_odds": to_american(best_dec),
                    "hedge_stake": calc["lay_stake"],
                    "profit": calc["profit"],
                    "conversion": calc["conversion"],
                })

    def dedup(rows):
        seen = set()
        out = []
        for row in sorted(rows, key=lambda x: -x["conversion"]):
            if row["matchup"] not in seen:
                seen.add(row["matchup"])
                out.append(row)
        return out

    return jsonify({
        "moneyline": dedup(moneyline_rows),
        "spreads": dedup(spread_rows),
        "stake": stake
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port)