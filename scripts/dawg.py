"""Dawg index: a 0-100 score for every rostered player. Writes data/dawg.json.

  Hustle    (40%)  dirty work per 36 minutes this season: rebounds (scaled by position), steals,
                   blocks, minus turnovers, then scaled by availability (games played / team games).
  Heat      (40%)  last 7 days' fantasy score per game against his normal level (ESPN projection).
  Big-game  (20%)  fantasy score in games played while his fantasy team was in a close matchup
                   (decided by 2 categories or fewer), against his own average in all games.
Each part is a percentile (0-100) among rostered players. If a player has too few close-matchup
games, Big-game is left out and Hustle/Heat count 50/50.
Game logs are cached in data/dawg_games.json so each run only fetches new days.
"""
import json, os, sys, logging
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
logging.disable(logging.CRITICAL)
import server as S  # noqa: E402

SEASON = S.SEASON
POS = {1: "PG", 2: "SG", 3: "SF", 4: "PF", 5: "C"}
REB_W = {"PG": 1.4, "SG": 1.4, "SF": 1.1, "PF": 0.8, "C": 0.6}  # a guard's rebound is worth more than a centre's
GAMES_FILE = os.path.join(ROOT, "data", "dawg_games.json")


def avg(p, sid):
    a, gp = S._avg(p, sid)
    return a or {}, gp


def fs(st):
    return S._fs(st)


def pct_rank(vals):
    """value -> percentile 0-100 (ties share the average rank)."""
    xs = sorted(v for v in vals if v is not None)
    n = len(xs)
    def f(v):
        if v is None or n < 2:
            return None
        below = sum(1 for x in xs if x < v)
        equal = sum(1 for x in xs if x == v)
        return round(100 * (below + 0.5 * (equal - 1)) / (n - 1))
    return f


def game_logs(d):
    """{sp: {team_id: [[player_id, fantasy_score, started], ...]}} for every finished day, cached."""
    try:
        cache = json.load(open(GAMES_FILE))
    except Exception:
        cache = {}
    latest = d["status"].get("latestScoringPeriod") or 1
    first = d["status"].get("firstScoringPeriod") or 1
    for sp in range(first, latest):  # today's day may still be in progress
        if str(sp) in cache:
            continue
        try:
            dd = S._get(["mRoster", "mTeam"], params={"scoringPeriodId": sp})
        except Exception:
            continue
        day = {}
        for t in dd["teams"]:
            rows = []
            for e in t["roster"]["entries"]:
                p = e["playerPoolEntry"]["player"]
                st = S._day_line(p, sp)
                if st:
                    rows.append([p["id"], round(fs(st), 1), e["lineupSlotId"] not in (12, 13)])
            day[str(t["id"])] = rows
        cache[str(sp)] = day
    os.makedirs(os.path.dirname(GAMES_FILE), exist_ok=True)
    with open(GAMES_FILE, "w") as f:
        json.dump(cache, f, separators=(",", ":"))
    return cache


def close_weeks(d):
    """{(matchup_period, team_id): True} where the matchup was close (final margin <= 2 categories,
    or for the current week, the live score is within 2)."""
    out = {}
    cur = d["status"].get("currentMatchupPeriod")
    for m in d["schedule"]:
        if "away" not in m:
            continue
        done = m.get("winner") not in (None, "UNDECIDED")
        if not done and m["matchupPeriodId"] != cur:
            continue
        (_, hc), (_, ac) = S._side_score(m["home"], live=not done), S._side_score(m["away"], live=not done)
        hw, aw = S._cats_won(hc, ac)
        close = abs(len(hw) - len(aw)) <= 2
        out[(m["matchupPeriodId"], m["home"]["teamId"])] = close
        out[(m["matchupPeriodId"], m["away"]["teamId"])] = close
    return out


def main():
    d = S._get(["mRoster", "mTeam", "mMatchup", "mMatchupScore", "mStatus", "mSettings"])
    names = {t["id"]: t.get("name") for t in d["teams"]}
    pool = {x["player"]["id"]: x["player"] for x in S._pool()}
    team_games = {}
    for p in pool.values():
        _, gp = avg(p, f"00{SEASON}")
        team_games[p.get("proTeamId")] = max(team_games.get(p.get("proTeamId"), 0), gp)

    # which matchup period each scoring period belongs to
    sp_week = {}
    for mp in range(1, (d["status"].get("currentMatchupPeriod") or 1) + 1):
        for sp in S._period_sps(d, mp):
            sp_week[sp] = mp
    close = close_weeks(d)
    logs = game_logs(d)
    allg, bigg = {}, {}
    for sp, day in logs.items():
        mp = sp_week.get(int(sp))
        for tid, rows in day.items():
            hot = close.get((mp, int(tid)), False)
            for pid, score, started in rows:
                if not started:
                    continue
                allg.setdefault(pid, []).append(score)
                if hot:
                    bigg.setdefault(pid, []).append(score)

    players = []
    for t in d["teams"]:
        for e in t["roster"]["entries"]:
            p = pool.get(e["playerPoolEntry"]["player"]["id"]) or e["playerPoolEntry"]["player"]
            pid = p["id"]
            pos = POS.get(p.get("defaultPositionId"), "SF")
            season, gp = avg(p, f"00{SEASON}")
            last7, gp7 = avg(p, f"01{SEASON}")
            proj, _ = avg(p, f"10{SEASON}")
            if not proj:
                proj, _ = avg(p, f"00{int(SEASON) - 1}")
            row = {"id": pid, "name": p.get("fullName"), "team": names[t["id"]], "pos": pos,
                   "status": (p.get("injuryStatus") or "ACTIVE"), "gp": gp,
                   "unsigned": not p.get("proTeamId")}
            # Hustle
            mins = season.get("40", 0)
            if gp >= 2 and mins >= 8:
                per36 = 36 / mins
                reb36 = season.get("6", 0) * per36
                stk36 = (season.get("2", 0) + season.get("1", 0)) * per36
                to36 = season.get("11", 0) * per36
                tg = max(team_games.get(p.get("proTeamId"), gp), gp) or 1
                avail = gp / tg
                row["hustle_raw"] = (REB_W[pos] * reb36 + 2.5 * stk36 - to36) * (0.7 + 0.3 * avail)
                row["detail"] = {"reb36": round(reb36, 1), "stocks36": round(stk36, 1), "to36": round(to36, 1),
                                 "min": round(mins, 1), "avail_pct": round(100 * avail)}
            # Heat
            base = fs(proj) if proj else None
            if gp7 >= 2 and base and base > 5:
                row["heat_raw"] = fs(last7) / base
                row["heat_vs_normal_pct"] = round(100 * (row["heat_raw"] - 1))
            # Big-game
            g, b = allg.get(pid, []), bigg.get(pid, [])
            row["close_games"] = len(b)
            if len(b) >= 2 and len(g) >= 3 and sum(g) > 0:
                row["big_raw"] = (sum(b) / len(b)) / (sum(g) / len(g))
                row["big_vs_normal_pct"] = round(100 * (row["big_raw"] - 1))
            players.append(row)

    for key in ("hustle", "heat", "big"):
        f = pct_rank([r.get(key + "_raw") for r in players])
        for r in players:
            r[key] = f(r.get(key + "_raw"))
    for r in players:
        parts = [(r["hustle"], 0.4), (r["heat"], 0.4), (r["big"], 0.2)]
        parts = [(v, w) for v, w in parts if v is not None]
        r["dawg"] = round(sum(v * w for v, w in parts) / sum(w for _, w in parts)) if len(parts) >= 2 else None
        for k in ("hustle_raw", "heat_raw", "big_raw"):
            r.pop(k, None)
    players.sort(key=lambda r: (r["dawg"] is None, -(r["dawg"] or 0)))
    out = {"updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "weights": {"hustle": 0.4, "heat": 0.4, "big": 0.2}, "reb_weights": REB_W,
           "players": players}
    with open(os.path.join(ROOT, "data", "dawg.json"), "w") as f:
        json.dump(out, f, separators=(",", ":"))
    rated = sum(1 for r in players if r["dawg"] is not None)
    print(f"dawg: {rated} of {len(players)} players rated; {len(logs)} days of game logs")


if __name__ == "__main__":
    main()
