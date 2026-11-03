"""Rebuild daily power rankings back to opening night, as if the site had been running from day 1.

For every NBA day (scoring period) up to today it rebuilds the league as it stood that morning: that day's
rosters (ESPN keeps them), each player's stats from games played before that day only (ESPN game logs,
blended with ESPN's preseason projection exactly like the live model), and injuries judged only from games
already missed (a player who missed his NBA team's last 2 games counts as out). Then it runs the normal
power model (_pairwise: average chance of beating every other team in a typical week) and stores the ranks.

Writes data/power_history.json, every rebuilt day marked "rebuilt". Live days are kept as they are.
Safe to re-run. Usage: python scripts/replay.py
"""
import json, os, sys, logging
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
logging.disable(logging.CRITICAL)
import server as S  # noqa: E402

SEASON = int(S.SEASON)
MISSED_FOR_OUT = 2
PATH = os.path.join(ROOT, "data", "power_history.json")


def fetch():
    d = S._get(["mTeam", "mStatus", "mSettings"])
    latest = d["status"]["latestScoringPeriod"]
    first = d["status"].get("firstScoringPeriod") or 1
    rosters = {sp: S._get(["mRoster", "mTeam"], params={"scoringPeriodId": sp}) for sp in range(first, latest + 1)}
    ids = sorted({e["playerId"] for r in rosters.values() for t in r["teams"] for e in t["roster"]["entries"]})
    logs, prior = {}, {}
    for i in range(0, len(ids), 50):
        flt = {"players": {"filterIds": {"value": ids[i:i + 50]},
                           "filterStatsForTopScoringPeriodIds": {"value": latest + 5, "additionalValue": [
                               f"10{SEASON}", f"00{SEASON - 1}"]}}}
        for x in S._get(["kona_player_info"], headers={"X-Fantasy-Filter": json.dumps(flt)}).get("players", []):
            p = x["player"]
            g = {}
            for s in p.get("stats", []):
                if s.get("statSplitTypeId") == 5 and s.get("seasonId") == SEASON and s.get("statSourceId") == 0 \
                        and s.get("stats") and (s["stats"].get("40") or s["stats"].get("14")):
                    g[s["scoringPeriodId"]] = s["stats"]
            logs[p["id"]] = g
            prior[p["id"]] = S._avg(p, f"10{SEASON}")[0] or S._avg(p, f"00{SEASON - 1}")[0]
    return d, rosters, logs, prior, first, latest


def main():
    d_now, rosters, logs, prior, first, latest = fetch()
    names = {t["id"]: t["name"] for t in d_now["teams"]}
    sched = S._schedule()
    opening = min(g for games in sched.values() for g in games).date()
    sp_date = lambda sp: opening + timedelta(days=sp - 1)  # noqa: E731
    state = {"sp": first}

    def rates(p):
        games = [g for s, g in logs.get(p["id"], {}).items() if s < state["sp"]]
        gp = len(games)
        cur = {k: sum(float(g.get(k, 0) or 0) for g in games) / gp for k in S.KEYS} if gp else None
        pr = prior.get(p["id"])
        if not pr and not cur:
            return None
        pr = pr or S.REPLACEMENT
        w = gp / (gp + 15) if cur else 0
        return {k: w * (cur or {}).get(k, 0) + (1 - w) * pr.get(k, 0) for k in S.KEYS}

    def status(p):
        pro = p.get("proTeamId") or 0
        if not pro:
            return "OUT"
        day = sp_date(state["sp"])
        team_days = sorted({g.date() for g in sched.get(pro, []) if g.date() < day})[-MISSED_FOR_OUT:]
        if len(team_days) < MISSED_FOR_OUT:
            return "ACTIVE"
        played = {sp_date(s) for s in logs.get(p["id"], {})}
        return "ACTIVE" if any(td in played for td in team_days) else "OUT"

    S._pinfo = lambda p, slot=None: {
        "id": p["id"], "name": p.get("fullName"), "pro": p.get("proTeamId", 0),
        "elig": [s for s in p.get("eligibleSlots", []) if s in (0, 1, 2, 3, 4, 5, 6, 11)],
        "status": status(p), "rates": rates(p), "slot": slot, "ret": None}
    S._injury_returns = lambda: {}

    try:
        ph = json.load(open(PATH))
    except Exception:
        ph = {"days": []}
    ph["days"] = [x for x in ph["days"] if not x.get("rebuilt")]
    live_dates = {x["date"] for x in ph["days"]}
    for sp in range(first, latest + 1):
        state["sp"] = sp
        key = sp_date(sp).isoformat()  # US Eastern date of that NBA day (same as the live job)
        if key in live_dates:
            continue
        dA = {"teams": rosters[sp]["teams"], "settings": d_now["settings"], "status": d_now["status"]}
        _, power = S._pairwise(dA)
        order = sorted(names, key=lambda t: -power[t])
        ph["days"].append({"date": key, "rebuilt": True,
                           "ranks": {names[t]: i + 1 for i, t in enumerate(order)},
                           "pct": {names[t]: round(100 * power[t]) for t in names}})
        print(f"day {sp} ({key}): #1 {names[order[0]]}")
    ph["days"].sort(key=lambda x: x["date"])
    with open(PATH, "w") as f:
        json.dump(ph, f, separators=(",", ":"))
    print("replay done")


if __name__ == "__main__":
    main()
