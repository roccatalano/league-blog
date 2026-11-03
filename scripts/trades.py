"""Trade tracker: how every trade this season is actually turning out. Writes data/trades.json.

For each trade, three views:
  at_time - the model's expected-wins change for each side when the trade happened (from the
            published trade review, or computed when first seen)
  so_far  - what really happened: each side's incoming players' stats, counting only games they
            started for that team after the trade, scored head-to-head across the 9 categories
            (so a trade can be "leading 6-3"). Rebuilt day by day for the chart.
  now     - the same trade re-run through the model with today's rosters and injuries
            (only while every traded player is still on the team that received him)

Caches every NBA day's starting lineups per player in data/player_days.json (one ESPN call per new day).
Run after live.py. Needs ESPN_S2 / ESPN_SWID.
"""
import json, os, sys, logging
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
logging.disable(logging.CRITICAL)
import server as S  # noqa: E402

DAYS = os.path.join(ROOT, "data", "player_days.json")
LOG = os.path.join(ROOT, "data", "trades_log.json")
OUT = os.path.join(ROOT, "data", "trades.json")
SEED = os.path.join(HERE, "trades_seed.json")
K = ["0", "6", "3", "2", "1", "17", "11", "13", "14", "15", "16"]  # PTS REB AST STL BLK 3PM TO FGM FGA FTM FTA
CATS = [("PTS", "0"), ("REB", "6"), ("AST", "3"), ("STL", "2"), ("BLK", "1"), ("3PM", "17"), ("TO", "11"), ("FG%", None), ("FT%", None)]


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def player_days(d):
    """{sp: {"date": "YYYY-MM-DD", "p": {name: [team_id, started, *K stats]}}} for every finished day."""
    cache = load(DAYS, {})
    latest = d["status"].get("latestScoringPeriod") or 1
    first = d["status"].get("firstScoringPeriod") or 1
    today = datetime.now(S.ET).date()
    for sp in range(first, latest):
        if str(sp) in cache:
            continue
        try:
            dd = S._get(["mRoster", "mTeam"], params={"scoringPeriodId": sp})
        except Exception:
            continue
        p = {}
        for t in dd["teams"]:
            for e in t["roster"]["entries"]:
                pl = e["playerPoolEntry"]["player"]
                st = S._day_line(pl, sp)
                if st:
                    p[pl.get("fullName")] = [t["id"], e["lineupSlotId"] not in (12, 13)] + [round(float(st.get(k, 0) or 0), 1) for k in K]
        cache[str(sp)] = {"date": str(today - timedelta(days=latest - sp)), "p": p}
    with open(DAYS, "w") as f:
        json.dump(cache, f, separators=(",", ":"))
    return cache


def find_trades(names, log, seed):
    """One record per trade. The league message feed only shows recent items, so every trade is
    remembered in trades_log.json the first time it's seen (the job runs every few hours).
    Trades from before the tracker existed come from trades_seed.json."""
    ids = {v: k for k, v in names.items()}
    known = log.setdefault("_trades", {})
    for key, x in seed.items():
        if not key.startswith("_") and "gets" in x and key not in known:
            known[key] = {"gets": x["gets"], "dates": x["dates"]}
    for e in json.loads(S.get_recent_transactions(limit=1000)):
        moves = [m for m in e["moves"] if m["action"] in ("traded", "trade accepted")]
        teams = sorted({m["team"] for m in moves})
        if not moves or len(teams) != 2 or any(t not in ids for t in teams):
            continue
        key = "|".join(sorted({str(m["player"]) for m in moves}))
        rec = known.setdefault(key, {"gets": {}, "dates": []})
        for m in moves:  # ESPN logs each traded player under the team RECEIVING him
            lst = rec["gets"].setdefault(m["team"], [])
            if m["player"] not in lst:
                lst.append(m["player"])
        if not any(x[:16] == e["date"][:16] for x in rec["dates"]):  # same message, different precision
            rec["dates"] = sorted(rec["dates"] + [e["date"]])
    return [{"key": k, "gets": v["gets"], "dates": v["dates"]} for k, v in known.items() if len(v["gets"]) == 2]


def day_of(iso):
    return datetime.fromisoformat(iso).astimezone(S.ET).date()


def score(got, gave):
    """Head-to-head across the 9 categories between two stat totals (lists in K order)."""
    out, w, l = [], 0, 0
    for cat, k in CATS:
        if k:
            a, b = got[K.index(k)], gave[K.index(k)]
        else:
            m, att = ("13", "14") if cat == "FG%" else ("15", "16")
            ga, gb = got[K.index(att)], gave[K.index(att)]
            a = got[K.index(m)] / ga if ga else None
            b = gave[K.index(m)] / gb if gb else None
        if a is None or b is None or abs(a - b) < 1e-9:
            res = 0
        else:
            res = 1 if (a < b if cat == "TO" else a > b) else -1
        w += res == 1; l += res == -1
        out.append({"cat": cat, "got": None if a is None else round(a, 3 if k is None else 1),
                    "gave": None if b is None else round(b, 3 if k is None else 1), "res": res})
    return w, l, out


def wins_added(team_id, delta, weeks, dists):
    """Team-aware value of what a trade has produced so far, in matchup wins.
    delta = this team's (got - gave) stat totals in K order; spread over `weeks` weeks.
    Compares the team's weekly totals with and without that per-week difference against every
    other team, so a category only counts as much as it moves this team's real chances."""
    if weeks <= 0:
        return 0.0
    mean, var = dists[team_id]
    without = (dict(mean), var)
    for i, k in enumerate(K):
        without[0][k] = mean.get(k, 0) - delta[i] / weeks
    opp = [dv for t, dv in dists.items() if t != team_id]
    w_with = sum(S._match_win(S._cat_probs((mean, var), o)) for o in opp) / len(opp)
    w_without = sum(S._match_win(S._cat_probs(without, o)) for o in opp) / len(opp)
    return round((w_with - w_without) * weeks * S.GAIN_CALIBRATION, 3)


def grade_wins(x, games):
    if games < 12:
        return "Too early"
    # x = matchup wins added PER WEEK since the trade, so early and late trades are graded alike
    for g, lo in (("A+", 0.04), ("A", 0.025), ("B+", 0.012), ("B", 0.004), ("C", -0.004), ("C-", -0.012), ("D+", -0.025), ("D", -0.04)):
        if x >= lo:
            return g
    return "F"


def grade(m, games):
    if games < 12:
        return "Too early"
    for g, lo in (("A+", 7), ("A", 5), ("B+", 3), ("B", 1), ("C", 0), ("C-", -2), ("D+", -4), ("D", -6)):
        if m >= lo:
            return g
    return "F"


def now_value(d, names, rosters, side, other, gets):
    """Model's rest-of-season expected-wins change for `side` from this trade, with today's info.
    None if any traded player has since left the team that received him."""
    ids = {v: k for k, v in names.items()}
    a, b = ids[side], ids[other]
    mine, theirs = rosters[a], rosters[b]
    got = [p for p in mine if p["name"] in gets[side]]
    gave = [p for p in theirs if p["name"] in gets[other]]
    if len(got) != len(gets[side]) or len(gave) != len(gets[other]):
        return None
    weeks = S._remaining_weeks(d)
    after = mine
    before = [p for p in mine if p not in got] + gave
    av, _ = S._fixture_value(after, d, a, weeks)
    bv, _ = S._fixture_value(before, d, a, weeks)
    return round((av - bv) * S.GAIN_CALIBRATION, 3)


def main():
    d = S._get(["mRoster", "mTeam", "mMatchup", "mSettings", "mStatus"])
    names = {t["id"]: t.get("name") for t in d["teams"]}
    rosters = {t["id"]: S._team_players(t["id"], d)[1] for t in d["teams"]}
    on_team = {p["name"]: tid for tid, pl in rosters.items() for p in pl}
    days = player_days(d)
    dists = {tid: S._weekly_dist(pl) for tid, pl in rosters.items()}
    seed, log = load(SEED, {}), load(LOG, {})
    today = str(datetime.now(S.ET).date())
    out = []
    log.pop("_scan", None)
    for t in find_trades(names, log, seed):
        key, gets = t["key"], t["gets"]
        A, B = sorted(gets)
        moved = all(on_team.get(n) == next(k for k, v in names.items() if v == side) for side in (A, B) for n in gets[side])
        if moved or len(t["dates"]) >= 2 or day_of(t["dates"][0]) < datetime.now(S.ET).date() - timedelta(days=3):
            start, status = t["dates"][-1], "processed"
        else:
            start, status = None, "in league review"
        rec = log.setdefault(key, {"first_seen": today, "history": []})
        # at the time
        if "at_time" not in rec:
            if key in seed:
                rec["at_time"], rec["post"], rec["at_time_source"] = seed[key]["at_time"], seed[key].get("post"), "trade review"
            else:
                at = {s: now_value(d, names, rosters, s, o, gets) for s, o in ((A, B), (B, A))} if status == "processed" else {}
                if status != "processed":
                    # not moved yet: score it as if it went through
                    at = {}
                    for s, o in ((A, B), (B, A)):
                        sid, oid = (next(k for k, v in names.items() if v == x) for x in (s, o))
                        got = [p for p in rosters[oid] if p["name"] in gets[s]]
                        gave = [p for p in rosters[sid] if p["name"] in gets[o]]
                        after = [p for p in rosters[sid] if p not in gave] + got
                        wk = S._remaining_weeks(d)
                        av, _ = S._fixture_value(after, d, sid, wk)
                        bv, _ = S._fixture_value(rosters[sid], d, sid, wk)
                        at[s] = round((av - bv) * S.GAIN_CALIBRATION, 3)
                if all(v is not None for v in at.values()) and at:
                    rec["at_time"], rec["at_time_source"] = at, "model when first seen"
        # so far: day by day from the day after processing
        rec_out = {"key": key, "accepted_utc": t["dates"][0], "status": status, "gets": gets,
                   "at_time": rec.get("at_time"), "post": rec.get("post")}
        if start:
            first_day = day_of(start) + timedelta(days=1)
            ids = {v: k for k, v in names.items()}
            tot = {A: [0.0] * len(K), B: [0.0] * len(K)}
            games = {A: 0, B: 0}
            series = []
            for sp in sorted(days, key=int):
                day = days[sp]
                if day["date"] < str(first_day):
                    continue
                for side in (A, B):
                    for n in gets[side]:
                        x = day["p"].get(n)
                        if x and x[0] == ids[side] and x[1]:
                            games[side] += 1
                            for i in range(len(K)):
                                tot[side][i] += x[2 + i]
                w, l, _ = score(tot[A], tot[B])
                nd = len(series) + 1
                dd = [tot[A][i] - tot[B][i] for i in range(len(K))]
                series.append([day["date"], w - l,
                               wins_added(ids[A], dd, nd / 7, dists), wins_added(ids[B], [-x for x in dd], nd / 7, dists)])
            w, l, cats = score(tot[A], tot[B])
            ndays = sum(1 for sp in days if days[sp]["date"] >= str(first_day))
            wk = ndays / 7
            dA = [tot[A][i] - tot[B][i] for i in range(len(K))]  # A got what B's new players didn't
            added = {A: wins_added(ids[A], dA, wk, dists), B: wins_added(ids[B], [-x for x in dA], wk, dists)}
            g = min(games.values())
            rec_out["so_far"] = {"from": str(first_day), "days": ndays, "score": [w, l, 9 - w - l], "cats": cats,
                                 "games": games, "series": series, "wins_added": added,
                                 "grade": {A: grade_wins(added[A] / max(wk, 1e-9), g), B: grade_wins(added[B] / max(wk, 1e-9), g)}}
            nv = {A: now_value(d, names, rosters, A, B, gets), B: now_value(d, names, rosters, B, A, gets)}
            if all(v is not None for v in nv.values()):
                rec["now"] = {"date": today, **nv}
                if not rec["history"] or rec["history"][-1][0] != today:
                    rec["history"].append([today, nv[A], nv[B]])
                else:
                    rec["history"][-1] = [today, nv[A], nv[B]]
            rec_out["now"] = rec.get("now")
            rec_out["now_frozen"] = not all(v is not None for v in nv.values())
            rec_out["now_history"] = rec["history"]
        out.append(rec_out)
    out.sort(key=lambda r: r["accepted_utc"], reverse=True)
    with open(LOG, "w") as f:
        json.dump(log, f, indent=1)
    with open(OUT, "w") as f:
        json.dump({"updated_utc": datetime.now(timezone.utc).isoformat(), "teams_order": "alphabetical",
                   "trades": out}, f, indent=1)
    print(f"trades: {len(out)} ({sum(r['status'] == 'processed' for r in out)} processed), days cached: {len(days)}")


if __name__ == "__main__":
    main()
