"""Refresh the site's live data (no posts, no AI). Writes:
  data/odds.json  - every current matchup: live category score, win chance, odds, per-category chances
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


def price(p, margin=0.10):
    """Decimal odds with a bookmaker margin, like the daily post uses."""
    if p <= 0.005:
        return 51.0
    return round(max(1.01, 1 / (p * (1 + margin))), 2)


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
            lose = None
            cats.append({"cat": S.CN[k], "now_home": round(ch[k], nd), "now_away": round(ca[k], nd),
                         "proj_home": c["proj_me"], "proj_away": c["proj_opp"],
                         "home_win_pct": round(100 * c["win_prob"])})
        ph_win, tie = res["win_prob"], res["tie_prob"]
        pa_win = max(0.0, 1 - ph_win - tie)
        out.append({"home": names[h], "away": names[a],
                    "score_now": [now_w, now_l, 9 - now_w - now_l],
                    "home_win_pct": round(100 * ph_win), "away_win_pct": round(100 * pa_win),
                    "tie_pct": round(100 * tie),
                    "price_home": price(ph_win), "price_away": price(pa_win),
                    "categories": cats})
    out.sort(key=lambda x: abs(x["home_win_pct"] - x["away_win_pct"]))
    return {"matchup_period": mp, "days_left_US": [str(x) for x in days], "matchups": out}


def model(d, names):
    weeks = S._remaining_weeks(d)
    teams = []
    for t in d["teams"]:
        _, pl = S._team_players(t["id"], d)
        teams.append({"id": t["id"], "name": names[t["id"]],
                      "players": [{"name": p["name"], "status": p["status"], "slot": p["slot"],
                                   "rates": {k: round(v, 3) for k, v in p["rates"].items()} if p["rates"] else None}
                                  for p in pl]})
    fixtures = [{"week": m["matchupPeriodId"], "home": m["home"]["teamId"], "away": m["away"]["teamId"]}
                for m in d["schedule"] if m["matchupPeriodId"] in weeks and m.get("away")]
    return {"weeks": [weeks[0], weeks[-1]], "teams": teams, "fixtures": fixtures,
            "consts": {"KEYS": S.KEYS, "CATS": S.CATS, "CN": S.CN, "VAR_SCALE": S.VAR_SCALE,
                       "GAIN_CALIBRATION": S.GAIN_CALIBRATION, "PLAY_PROB": S.PLAY_PROB}}


def main():
    d = S._get(["mRoster", "mTeam", "mMatchup", "mMatchupScore", "mStatus", "mSettings"])
    names = {t["id"]: t.get("name") for t in d["teams"]}
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    o = odds(d, names); o["updated_utc"] = stamp
    m = model(d, names); m["updated_utc"] = stamp
    with open(os.path.join(ROOT, "data", "odds.json"), "w") as f:
        json.dump(o, f, separators=(",", ":"))
    with open(os.path.join(ROOT, "data", "model.json"), "w") as f:
        json.dump(m, f, separators=(",", ":"))
    print(f"odds: {len(o['matchups'])} matchups; model: {len(m['teams'])} teams, {len(m['fixtures'])} fixtures")


if __name__ == "__main__":
    main()
