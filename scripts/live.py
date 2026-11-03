"""Refresh the site's live data (no posts, no AI). Writes:
  data/odds.json  - every current matchup: live category score, win chance, odds, per-category chances
  data/stats.json - Stat ranks page: per matchup / per day / per game averages, games played
  data/stats_days.json - cache of each finished day's starter totals per team
  data/model.json - rosters + remaining schedule, used by the trade calculator in the browser
Needs ESPN_S2 and ESPN_SWID in the environment.
"""
import json, os, sys, logging
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
logging.disable(logging.CRITICAL)
import server as S  # noqa: E402

# Players with no NBA team (unsigned) can't play: count them as OUT everywhere.
_team_players_orig = S._team_players


def _team_players(team_id, d=None):
    name, players = _team_players_orig(team_id, d)
    for p in players:
        if not p.get("pro"):
            p["status"] = "OUT"
    return name, players


S._team_players = _team_players


def price(p, margin=0.10):
    """Decimal odds with a bookmaker margin, like the daily post uses."""
    if p <= 0.005:
        return 51.0
    return round(max(1.01, 1 / (p * (1 + margin))), 2)


LIVE = {}


def odds(d, names):
    sched = S._schedule()
    now = datetime.now(S.ET)
    days = S._week_days(0)
    mp = d["status"].get("currentMatchupPeriod")
    out = []
    for m in d["schedule"]:
        if m["matchupPeriodId"] != mp or not m.get("away"):
            continue
        h, a = m["home"]["teamId"], m["away"]["teamId"]
        _, _, base_h, base_a = S._current_matchup(d, h)
        _, ph = S._team_players(h, d)
        _, pa = S._team_players(a, d)
        res = S._matchup_odds(ph, pa, days, sched, base_h, base_a, n=1000, now=now)
        ch, ca = S._cats(base_h), S._cats(base_a)
        now_w = now_l = 0
        cats = []
        for k in S.CATS:
            if S._beats(k, ch[k], ca[k]):
                now_w += 1
            elif ch[k] != ca[k]:
                now_l += 1
            nd = 3 if k in ("19", "20") else 0
            c = res["categories"][S.CN[k]]
            cats.append({"cat": S.CN[k], "now_home": round(ch[k], nd), "now_away": round(ca[k], nd),
                         "proj_home": c["proj_me"], "proj_away": c["proj_opp"],
                         "home_win_pct": round(100 * c["win_prob"])})
        ph_win, tie = res["win_prob"], res["tie_prob"]
        LIVE[(h, a)] = (ph_win, tie)
        pa_win = max(0.0, 1 - ph_win - tie)
        out.append({"home": names[h], "away": names[a],
                    "score_now": [now_w, now_l, 9 - now_w - now_l],
                    "home_win_pct": round(100 * ph_win), "away_win_pct": round(100 * pa_win),
                    "tie_pct": round(100 * tie),
                    "price_home": price(ph_win), "price_away": price(pa_win),
                    "categories": cats})
    out.sort(key=lambda x: abs(x["home_win_pct"] - x["away_win_pct"]))
    return {"matchup_period": mp, "days_left_US": [str(x) for x in days], "matchups": out}


def league(d, names, live_pairs):
    """Ladder + premiership / make-finals odds from the season simulator (same as the weekly post)."""
    P, power = S._pairwise(d)
    sim, po_n, in_playoffs = S._season_sim(d, P, live=live_pairs)
    ids = [t["id"] for t in d["teams"]]
    title_prices = S._book([sim[t]["title"] for t in ids], S.FUTURES_MARGIN, places=1)
    finals_prices = S._book([sim[t]["finals"] for t in ids], S.FUTURES_MARGIN, places=po_n)
    cat = {t: [0, 0, 0] for t in ids}  # categories won / lost / tied in finished weeks
    for m in d["schedule"]:
        if "away" not in m or m.get("winner") in (None, "UNDECIDED"):
            continue
        h, a = m["home"]["teamId"], m["away"]["teamId"]
        (_, hc), (_, ac) = S._side_score(m["home"], live=False), S._side_score(m["away"], live=False)
        hw, aw = S._cats_won(hc, ac)
        tie = 9 - len(hw) - len(aw)
        cat[h][0] += len(hw); cat[h][1] += len(aw); cat[h][2] += tie
        cat[a][0] += len(aw); cat[a][1] += len(hw); cat[a][2] += tie
    rows = []
    for i, t in enumerate(d["teams"]):
        r = t["record"]["overall"]
        rows.append({"team": names[t["id"]], "seed": t.get("playoffSeed"),
                     "w": r["wins"], "l": r["losses"], "t": r["ties"],
                     "cats": cat[t["id"]],
                     "faab_left": 500 - (t.get("transactionCounter", {}) or {}).get("acquisitionBudgetSpent", 0),
                     "power_pct": round(100 * power[t["id"]]),
                     "exp_wins": round(sim[t["id"]]["exp_wins"], 1),
                     "finals_pct": round(100 * sim[t["id"]]["finals"], 1),
                     "title_pct": round(100 * sim[t["id"]]["title"], 1),
                     "title_price": title_prices[i], "finals_price": finals_prices[i]})
    rows.sort(key=lambda x: (x["seed"] or 99))
    return {"finals_teams": po_n, "in_playoffs": in_playoffs, "sims": S.SEASON_SIMS, "ladder": rows}


DAY_KEYS = ["0", "6", "3", "2", "1", "17", "11", "13", "14", "15", "16"]  # cats + FGM/FGA/FTM/FTA


def day_totals(d):
    """{sp: {team_id: {"g": games, stat_id: total}}} for every finished NBA day, starters only. Cached."""
    path = os.path.join(ROOT, "data", "stats_days.json")
    try:
        cache = json.load(open(path))
    except Exception:
        cache = {}
    latest = d["status"].get("latestScoringPeriod") or 1
    first = d["status"].get("firstScoringPeriod") or 1
    for sp in range(first, latest):  # today may still be in progress
        if str(sp) in cache:
            continue
        try:
            dd = S._get(["mRoster", "mTeam"], params={"scoringPeriodId": sp})
        except Exception:
            continue
        day = {}
        for t in dd["teams"]:
            row = {"g": 0, **{k: 0.0 for k in DAY_KEYS}}
            for e in t["roster"]["entries"]:
                if e["lineupSlotId"] in (12, 13):  # bench / IR
                    continue
                st = S._day_line(e["playerPoolEntry"]["player"], sp)
                if st:
                    row["g"] += 1
                    for k in DAY_KEYS:
                        row[k] += float(st.get(k, 0) or 0)
            day[str(t["id"])] = row
        cache[str(sp)] = day
    with open(path, "w") as f:
        json.dump(cache, f, separators=(",", ":"))
    return cache, latest


def stats(d, names):
    """Stat ranks page. Per matchup: finished matchups. Per day / per game: every finished day so far."""
    ids = [t["id"] for t in d["teams"]]
    days, latest = day_totals(d)
    # Map each day to its matchup week (Mon-Sun; week 1 is everything before the first Monday).
    from datetime import timedelta
    today = datetime.now(S.ET).date()
    cur = d["status"].get("currentMatchupPeriod") or 1
    monday = today - timedelta(days=today.weekday())
    week_of = {}
    for sp in range(1, latest + 1):
        day = today - timedelta(days=latest - sp)
        week_of[str(sp)] = max(1, cur + ((day - timedelta(days=day.weekday())) - monday).days // 7)

    wk = {t: {k: 0.0 for k in S.CATS} for t in ids}
    nm = {t: 0 for t in ids}
    gm = {t: 0 for t in ids}  # games played in finished matchups
    done = set()
    for m in d["schedule"]:
        if "away" not in m or m.get("winner") in (None, "UNDECIDED"):
            continue
        done.add(m["matchupPeriodId"])
        for side in (m["home"], m["away"]):
            _, sc = S._side_score(side, live=False)
            for k in S.CATS:
                wk[side["teamId"]][k] += sc[k]
            nm[side["teamId"]] += 1
    tot = {t: {"g": 0, **{k: 0.0 for k in DAY_KEYS}} for t in ids}
    for sp, day in days.items():
        for t in ids:
            r = day.get(str(t)) or {}
            for k in tot[t]:
                tot[t][k] += r.get(k, 0)
            if week_of.get(sp) in done:
                gm[t] += r.get("g", 0)
    nd = len(days)

    def season(t, k):
        T = tot[t]
        if k == "19":
            return T["13"] / T["14"] if T["14"] else None
        if k == "20":
            return T["15"] / T["16"] if T["16"] else None
        return T[k]

    cats = []
    for k in S.CATS:
        pct = k in ("19", "20")
        rows = []
        for t in ids:
            v = season(t, k)
            rows.append({"team": names[t],
                         "total": None if v is None else round(v, 4 if pct else 1),
                         "matchup": round(wk[t][k] / nm[t], 4 if pct else 2) if nm[t] else None,
                         "day": None if v is None or not nd else round(v if pct else v / nd, 4 if pct else 2),
                         "game": None if v is None or not tot[t]["g"] else round(v if pct else v / tot[t]["g"], 4 if pct else 2),
                         "matchups": nm[t], "days": nd, "games": tot[t]["g"]})
        cats.append({"cat": S.CN[k], "rows": rows})
    games = [{"team": names[t], "per_day": round(tot[t]["g"] / nd, 2) if nd else None,
              "per_matchup": round(gm[t] / nm[t], 1) if nm[t] else None,
              "games": tot[t]["g"]} for t in ids]
    return {"weeks_done": len(done), "days_done": nd, "games": games, "categories": cats}


def model(d, names):
    weeks = S._remaining_weeks(d)
    teams = []
    for t in d["teams"]:
        _, pl = S._team_players(t["id"], d)
        teams.append({"id": t["id"], "name": names[t["id"]],
                      "players": [{"name": p["name"], "status": p["status"], "slot": p["slot"],
                                   "avail": round(S._avail(p), 3), "ret": p["ret"].isoformat() if p.get("ret") else None,
                                   "rates": {k: round(v, 3) for k, v in p["rates"].items()} if p["rates"] else None}
                                  for p in pl]})
    fixtures = [{"week": m["matchupPeriodId"], "home": m["home"]["teamId"], "away": m["away"]["teamId"]}
                for m in d["schedule"] if m["matchupPeriodId"] in weeks and m.get("away")]
    return {"weeks": [weeks[0], weeks[-1]], "teams": teams, "fixtures": fixtures,
            "consts": {"KEYS": S.KEYS, "CATS": S.CATS, "CN": S.CN, "VAR_SCALE": S.VAR_SCALE,
                       "GAIN_CALIBRATION": S.GAIN_CALIBRATION, "PLAY_PROB": S.PLAY_PROB, "REPLACEMENT": S.REPLACEMENT}}


def main():
    d = S._get(["mRoster", "mTeam", "mMatchup", "mMatchupScore", "mStatus", "mSettings", "mStandings"])
    names = {t["id"]: t.get("name") for t in d["teams"]}
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    o = odds(d, names); o["updated_utc"] = stamp
    m = model(d, names); m["updated_utc"] = stamp
    lg = league(d, names, LIVE); lg["updated_utc"] = stamp
    st = stats(d, names); st["updated_utc"] = stamp
    # keep ~30h of title/finals history so the page can show movement over the last day
    lpath = os.path.join(ROOT, "data", "league.json")
    try:
        hist = json.load(open(lpath)).get("history", [])
    except Exception:
        hist = []
    hist.append({"t": stamp, "title": {r["team"]: r["title_pct"] for r in lg["ladder"]},
                 "finals": {r["team"]: r["finals_pct"] for r in lg["ladder"]}})
    hist = hist[-64:]
    now_ts = datetime.fromisoformat(stamp).timestamp()
    old = [h for h in hist if now_ts - datetime.fromisoformat(h["t"]).timestamp() >= 20 * 3600]
    ref = old[-1] if old else hist[0]
    for r in lg["ladder"]:
        r["title_move"] = round(r["title_pct"] - ref["title"].get(r["team"], r["title_pct"]), 1)
        r["finals_move"] = round(r["finals_pct"] - ref["finals"].get(r["team"], r["finals_pct"]), 1)
    lg["moves_since_utc"] = ref["t"]
    lg["history"] = hist
    with open(lpath, "w") as f:
        json.dump(lg, f, separators=(",", ":"))
    with open(os.path.join(ROOT, "data", "odds.json"), "w") as f:
        json.dump(o, f, separators=(",", ":"))
    with open(os.path.join(ROOT, "data", "stats.json"), "w") as f:
        json.dump(st, f, separators=(",", ":"))
    with open(os.path.join(ROOT, "data", "model.json"), "w") as f:
        json.dump(m, f, separators=(",", ":"))
    print(f"league: {len(lg['ladder'])} teams; odds: {len(o['matchups'])} matchups; model: {len(m['teams'])} teams, {len(m['fixtures'])} fixtures")


if __name__ == "__main__":
    main()
