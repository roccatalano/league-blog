"""Waiver wire tracker for a 9-category league. Writes data/waivers.json.

Pickups: every free-agent / waiver add this season. A pickup only counts for the team that added him, in
games he STARTED for them, from the day of the add until he's dropped or traded (from the day-by-day
starting lineups in data/player_days.json, which trades.py keeps up to date).
  cats_swung    - re-score each of that team's matchups with and without his stats: how many category
                  results changed because of him (a win turned into a loss, a tie, etc.)
  matchups_swung - weeks where removing him changes the matchup result itself
  value         - a standard 9-cat player rating: for each game, how far above/below a typical waiver
                  pickup's game he was in each category (z-scores; FG%/FT% weighted by attempts; TO counts
                  against), summed over the 9 categories. Total = sum over his starts, per game = average.
Drops: every drop and what the player has done since, in all his games wherever he is (value, per game).
A player the same team later re-added doesn't count as a drop.

Adds and drops come from ESPN's transaction log and are remembered in data/waivers_log.json.
Run after trades.py. Needs ESPN_S2 / ESPN_SWID.
"""
import json, math, os, sys, logging
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
logging.disable(logging.CRITICAL)
import server as S  # noqa: E402

DAYS = os.path.join(ROOT, "data", "player_days.json")
LOG = os.path.join(ROOT, "data", "waivers_log.json")
OUT = os.path.join(ROOT, "data", "waivers.json")
K = ["0", "6", "3", "2", "1", "17", "11", "13", "14", "15", "16"]  # PTS REB AST STL BLK 3PM TO FGM FGA FTM FTA
I = {k: i for i, k in enumerate(K)}
COUNT = [("PTS", "0"), ("REB", "6"), ("AST", "3"), ("STL", "2"), ("BLK", "1"), ("3PM", "17"), ("TO", "11")]
CAT_NAMES = [c for c, _ in COUNT] + ["FG%", "FT%"]


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


# ---------------------------------------------------------------- transactions
def scan(d, log):
    """Remember every executed add / drop. Full-season scan the first time, then the last few days."""
    latest = d["status"].get("transactionScoringPeriod") or d["status"].get("latestScoringPeriod") or 1
    adds, drops = log.setdefault("adds", {}), log.setdefault("drops", {})
    start = 0 if not log.get("scanned_full") else max(0, latest - 4)
    for sp in range(start, latest + 2):
        try:
            tx = S._get(["mTransactions2"], params={"scoringPeriodId": sp}).get("transactions", [])
        except Exception:
            continue
        for t in tx:
            when = t.get("processDate") or t.get("proposedDate")  # free-agent adds only carry proposedDate
            if t.get("type") not in ("FREEAGENT", "WAIVER") or t.get("status") != "EXECUTED" or not when:
                continue
            for i in t.get("items", []):
                pid = i.get("playerId")
                if not pid:
                    continue
                if i.get("type") == "ADD":
                    adds.setdefault(f'{t["id"]}:{pid}', {"team": i.get("toTeamId") or t.get("teamId"), "player": pid,
                                                        "bid": t.get("bidAmount") or 0, "type": t["type"], "date_utc": iso(when)})
                elif i.get("type") == "DROP":
                    drops.setdefault(f'{t["id"]}:{pid}', {"team": i.get("fromTeamId") or t.get("teamId"), "player": pid,
                                                         "date_utc": iso(when)})
    log["scanned_full"] = True


def player_info(ids):
    """{id: {"name", "on": teamId or 0, "games": [(sp, stats K-list), ...]}} for this season."""
    out = {}
    ids = sorted(ids)
    for i in range(0, len(ids), 50):
        flt = {"players": {"filterIds": {"value": ids[i:i + 50]},
                           "filterStatsForTopScoringPeriodIds": {"value": 200, "additionalValue": []}}}
        try:
            got = S._get(["kona_player_info"], headers={"X-Fantasy-Filter": json.dumps(flt)}).get("players", [])
        except Exception:
            got = []
        for x in got:
            p = x["player"]
            g = [(s["scoringPeriodId"], [float(s["stats"].get(k, 0) or 0) for k in K]) for s in p.get("stats", [])
                 if s.get("statSplitTypeId") == 5 and s.get("seasonId") == int(S.SEASON) and s.get("statSourceId") == 0
                 and s.get("stats") and (s["stats"].get("40") or s["stats"].get("14"))]
            out[p["id"]] = {"name": p.get("fullName", str(p["id"])), "on": x.get("onTeamId") or 0, "games": sorted(g)}
    return out


# ---------------------------------------------------------------- value (9-cat z-scores per game)
def baseline(days, ref_games):
    """Zero point = the average game in ref_games (every start a waiver pickup made for the team that added
    him), so 0 = a typical waiver pickup. Spread (sd) comes from every started game by a rostered player."""
    games = [x[2:] for day in days.values() for x in day["p"].values() if x[1]]
    n = max(1, len(games))
    ref = ref_games or games
    nr = len(ref)
    mu = {k: sum(g[I[k]] for g in ref) / nr for _, k in COUNT}
    sd = {k: math.sqrt(sum((g[I[k]] - sum(h[I[k]] for h in games) / n) ** 2 for g in games) / n) or 1 for _, k in COUNT}
    fg = sum(g[I["13"]] for g in ref) / max(1, sum(g[I["14"]] for g in ref))
    ft = sum(g[I["15"]] for g in ref) / max(1, sum(g[I["16"]] for g in ref))
    # percentage impact = made above league rate on his attempts
    imp_fg = [g[I["13"]] - fg * g[I["14"]] for g in games]
    imp_ft = [g[I["15"]] - ft * g[I["16"]] for g in games]
    sfg = math.sqrt(sum(x * x for x in imp_fg) / n) or 1
    sft = math.sqrt(sum(x * x for x in imp_ft) / n) or 1
    return {"mu": mu, "sd": sd, "fg": fg, "ft": ft, "sfg": sfg, "sft": sft}


def game_value(g, B):
    v = 0.0
    for c, k in COUNT:
        z = (g[I[k]] - B["mu"][k]) / B["sd"][k]
        v += -z if c == "TO" else z
    v += (g[I["13"]] - B["fg"] * g[I["14"]]) / B["sfg"]
    v += (g[I["15"]] - B["ft"] * g[I["16"]]) / B["sft"]
    return v


# ---------------------------------------------------------------- matchups with and without a player
def cat_results(a, b):
    """+1 / 0 / -1 per category for side a (K-order totals)."""
    out = []
    for c, k in COUNT:
        x, y = a[I[k]], b[I[k]]
        out.append(0 if abs(x - y) < 1e-9 else (1 if (x < y if c == "TO" else x > y) else -1))
    for m, att in (("13", "14"), ("15", "16")):
        x = a[I[m]] / a[I[att]] if a[I[att]] else 0
        y = b[I[m]] / b[I[att]] if b[I[att]] else 0
        out.append(0 if abs(x - y) < 1e-9 else (1 if x > y else -1))
    return out


def result(cats):
    s = sum(cats)
    return 1 if s > 0 else (-1 if s < 0 else 0)


def main():
    d = S._get(["mTeam", "mStatus", "mSettings", "mMatchup"])
    names = {t["id"]: t.get("name") for t in d["teams"]}
    days = load(DAYS, {})
    log = load(LOG, {})
    scan(d, log)
    with open(LOG, "w") as f:
        json.dump(log, f, indent=1)

    # NBA days -> fantasy week (Monday-Sunday, week 1 starts on opening night)
    order = sorted(days, key=int)
    week_of, w = {}, 1
    for i, sp in enumerate(order):
        dt = datetime.fromisoformat(days[sp]["date"]).date()
        if i and dt.weekday() == 0:
            w += 1
        week_of[sp] = w
    opp = {}  # (week, team) -> opponent
    for m in d["schedule"]:
        if "away" in m:
            h, a = m["home"]["teamId"], m["away"]["teamId"]
            opp[(m["matchupPeriodId"], h)] = a
            opp[(m["matchupPeriodId"], a)] = h
    # each team's weekly totals from its starters
    tot = {}
    for sp in order:
        for x in days[sp]["p"].values():
            if x[1]:
                t = tot.setdefault((week_of[sp], x[0]), [0.0] * len(K))
                for i in range(len(K)):
                    t[i] += x[2 + i]

    ids = {a["player"] for a in log["adds"].values()} | {x["player"] for x in log["drops"].values()}
    info = player_info(ids)

    # ---- pickups (a player dropped and re-added by the same team counts once, from his first add)
    merged = {}
    for a in log["adds"].values():
        k = (a["team"], a["player"])
        if k not in merged or a["date_utc"] < merged[k]["date_utc"]:
            merged[k] = dict(a, bid=max(a["bid"], merged.get(k, {}).get("bid", 0)))
    def his_games(tid, name, first):
        return [(sp, days[sp]["p"][name][2:]) for sp in order if days[sp]["date"] >= first
                and (x := days[sp]["p"].get(name)) and x[0] == tid and x[1]]
    ref = [g for a in merged.values() if a["team"] in names and a["player"] in info
           for _, g in his_games(a["team"], info[a["player"]]["name"],
                                 str(datetime.fromisoformat(a["date_utc"]).astimezone(S.ET).date()))]
    B = baseline(days, ref)
    picks = []
    for a in merged.values():
        tid, pid = a["team"], a["player"]
        if tid not in names or pid not in info:
            continue
        name = info[pid]["name"]
        first = str(datetime.fromisoformat(a["date_utc"]).astimezone(S.ET).date())
        his = {}  # week -> his started totals for this team
        starts, val = 0, 0.0
        for sp in order:
            if days[sp]["date"] < first:
                continue
            x = days[sp]["p"].get(name)
            if not x or x[0] != tid or not x[1]:
                continue
            starts += 1
            val += game_value(x[2:], B)
            h = his.setdefault(week_of[sp], [0.0] * len(K))
            for i in range(len(K)):
                h[i] += x[2 + i]
        cats_swung, m_swung, swung_names = 0, 0, {}
        for wk, h in his.items():
            o = opp.get((wk, tid))
            if o is None or (wk, tid) not in tot:
                continue
            mine, theirs = tot[(wk, tid)], tot.get((wk, o), [0.0] * len(K))
            without = [mine[i] - h[i] for i in range(len(K))]
            r_with, r_without = cat_results(mine, theirs), cat_results(without, theirs)
            for c, x, y in zip(CAT_NAMES, r_with, r_without):
                if x != y:
                    cats_swung += 1 if x > y else -1  # he helped (+) or cost them (-) this category
                    swung_names[c] = swung_names.get(c, 0) + (1 if x > y else -1)
            if result(r_with) != result(r_without):
                m_swung += 1 if result(r_with) > result(r_without) else -1
        picks.append({"player": name, "team": names[tid], "bid": a["bid"],
                      "type": "waiver claim" if a["type"] == "WAIVER" else "free agent", "date": first,
                      "starts": starts, "value": round(val, 1), "per_game": round(val / starts, 2) if starts else None,
                      "cats_swung": cats_swung, "matchups_swung": m_swung,
                      "swung": {c: n for c, n in swung_names.items() if n}})
    picks.sort(key=lambda x: (-x["cats_swung"], -x["value"]))

    # ---- drops: what he's done since, anywhere
    adds = log["adds"].values()
    latest = {}
    for x in log["drops"].values():
        k = (x["team"], x["player"])
        if k not in latest or x["date_utc"] > latest[k]["date_utc"]:
            latest[k] = x
    opening = datetime.fromisoformat(days[order[0]]["date"]).date() - timedelta(days=int(order[0]) - 1) if order else None
    drops = []
    for (tid, pid), x in latest.items():
        if tid not in names or pid not in info or any(
                a["team"] == tid and a["player"] == pid and a["date_utc"] > x["date_utc"] for a in adds):
            continue
        t = datetime.fromisoformat(x["date_utc"]).astimezone(S.ET)
        first = t.date() if t.hour < 19 else t.date() + timedelta(days=1)  # dropped before tip-off counts that day
        g = [st for sp, st in info[pid]["games"] if opening and opening + timedelta(days=sp - 1) >= first]
        v = sum(game_value(st, B) for st in g)
        drops.append({"player": info[pid]["name"], "team": names[tid], "now": names.get(info[pid]["on"], "free agent"),
                      "date": str(t.date()), "games": len(g), "value": round(v, 1),
                      "per_game": round(v / len(g), 2) if g else None})
    drops.sort(key=lambda r: -r["value"])

    by_team = sorted(({"team": tn, "pickups": sum(1 for p in picks if p["team"] == tn),
                       "cats_swung": sum(p["cats_swung"] for p in picks if p["team"] == tn),
                       "matchups_swung": sum(p["matchups_swung"] for p in picks if p["team"] == tn),
                       "value": round(sum(p["value"] for p in picks if p["team"] == tn), 1),
                       "faab": sum(p["bid"] for p in picks if p["team"] == tn)} for tn in names.values()),
                     key=lambda r: (-r["cats_swung"], -r["value"]))
    with open(OUT, "w") as f:
        json.dump({"updated_utc": datetime.now(timezone.utc).isoformat(), "days_tracked": len(order),
                   "pickups": {"count": len(picks), "all": picks, "by_team": by_team},
                   "drops": {"count": len(drops), "all": drops}}, f, separators=(",", ":"))
    print(f"waivers: {len(picks)} pickups, {len(drops)} drops")


if __name__ == "__main__":
    main()
