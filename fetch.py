"""Pull today's league data for the blog post. Writes data/latest.json.

Usage: python scripts/fetch.py daily|weekly
Needs ESPN_S2 and ESPN_SWID in the environment (GitHub secrets).
"""
import json, os, sys, logging
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
logging.disable(logging.CRITICAL)
import server as S  # noqa: E402

TRADES_SEEN = os.path.join(HERE, "blog_trades_seen.json")


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _brief(p):
    """Name, injury status and the per-game rates the model uses."""
    r = p.get("rates") or {}
    pg = {S.CN[k]: round(v, 1) for k, v in r.items() if k in S.CN and S.CN[k] not in ("FG%", "FT%")}
    if r.get(14):
        pg["FG%"] = round(r[13] / r[14], 3)
    if r.get(16):
        pg["FT%"] = round(r[15] / r[16], 3)
    return {"name": p["name"], "injury": p["status"], "model_per_game": pg}


def trade_reviews():
    """Find trades not yet reviewed and score both sides with the model."""
    seen = set(_load(TRADES_SEEN, []))
    tx = json.loads(S.get_recent_transactions(limit=60))
    d = S._get(["mRoster", "mTeam", "mSettings", "mStatus", "mMatchup"])
    weeks = S._remaining_weeks(d)
    names = {t["id"]: t.get("name") for t in d["teams"]}
    ids = {v: k for k, v in names.items()}
    rosters = {t["id"]: S._team_players(t["id"], d)[1] for t in d["teams"]}
    out = []
    for entry in tx:
        moves = [m for m in entry["moves"] if m["action"] == "traded"]
        if not moves:
            continue
        key = entry["date"] + "|" + "|".join(sorted(m["player"] for m in moves))
        if key in seen:
            continue
        teams = sorted({m["team"] for m in moves})
        if len(teams) != 2 or any(t not in ids for t in teams):
            continue
        a, b = ids[teams[0]], ids[teams[1]]
        # "team X traded player P" = P leaves X
        gives = {a: [m["player"] for m in moves if m["team"] == teams[0]],
                 b: [m["player"] for m in moves if m["team"] == teams[1]]}

        def find(name):
            for tid, pl in rosters.items():
                for p in pl:
                    if (p["name"] or "") == name:
                        return tid, p
            return None, None

        objs = {}
        ok = True
        for side in (a, b):
            for n in gives[side]:
                tid, p = find(n)
                if p is None:
                    ok = False
                objs[n] = (tid, p)
        if not ok:
            continue
        processed = all(objs[n][0] != side for side in (a, b) for n in gives[side])
        other = {a: b, b: a}
        result = {"date_utc": entry["date"],
                  "status": "processed" if processed else "accepted, in league review (players not moved yet)",
                  "sides": []}
        for side in (a, b):
            give_p = [objs[n][1] for n in gives[side]]
            get_p = [objs[n][1] for n in gives[other[side]]]
            cur = rosters[side]
            if processed:
                after = cur
                before = [p for p in cur if p not in get_p] + give_p
            else:
                before = cur
                after = [p for p in cur if p not in give_p] + get_p
            bv, _ = S._fixture_value(before, d, side, weeks)
            av, _ = S._fixture_value(after, d, side, weeks)
            league = {t: S._weekly_dist(rosters[t]) for t in rosters if t != side}

            def prof(pl):
                md = S._weekly_dist(pl)
                pr = [S._cat_probs(md, od) for od in league.values()]
                return {S.CN[k]: sum(p[k] for p in pr) / len(pr) for k in S.CATS}
            pb, pa = prof(before), prof(after)
            result["sides"].append({
                "team": names[side],
                "gets": [_brief(p) | {"on_fantasy_team_now": names.get(objs[p["name"]][0])} for p in get_p],
                "gives": [_brief(p) for p in give_p],
                "expected_wins_change_rest_of_season": round((av - bv) * S.GAIN_CALIBRATION, 3),
                "category_win_rate_change_vs_league": {k: round(pa[k] - pb[k], 3) for k in pb if abs(pa[k] - pb[k]) >= 0.005},
            })
        result["weeks_counted"] = f"{weeks[0]}-{weeks[-1]}"
        out.append(result)
        seen.add(key)
    with open(TRADES_SEEN, "w") as f:
        json.dump(sorted(seen), f, indent=1)
    return out


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "daily"
    now_mel = datetime.now(timezone.utc) + timedelta(hours=11)
    data = {"kind": kind, "generated_utc": datetime.now(timezone.utc).isoformat(),
            "date_melbourne_approx": now_mel.strftime("%Y-%m-%d")}
    if kind == "weekly":
        data["weekly"] = json.loads(S.weekly_post_data())
    else:
        data["daily"] = json.loads(S.daily_post_data())
        data["trades"] = trade_reviews()
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    with open(os.path.join(ROOT, "data", "latest.json"), "w") as f:
        json.dump(data, f, indent=1, default=str)
    print(f"wrote data/latest.json ({kind}), trades: {len(data.get('trades', []))}")


if __name__ == "__main__":
    main()
