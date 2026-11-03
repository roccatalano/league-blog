"""Model track record: the model's pre-week predictions vs what actually happened.
Writes data/accuracy.json (for the site) and keeps data/prediction_log.json (opening predictions).

- Opening predictions: the first data/odds.json seen in each matchup period (live.py writes it every
  30 min, so this is the Monday-morning line, with per-category chances and projected totals).
  Weeks before this script existed are backfilled at matchup level from the blog's stored opening
  odds (scripts/league_posts_history.json), or that week's first daily snapshot.
- Results: final category totals from ESPN once every matchup in the period has a winner.
Run after live.py. Needs ESPN_S2 / ESPN_SWID like the other scripts.
"""
import json, os, sys, logging
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
logging.disable(logging.CRITICAL)
import server as S  # noqa: E402

LOG = os.path.join(ROOT, "data", "prediction_log.json")
OUT = os.path.join(ROOT, "data", "accuracy.json")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def capture_opening(log):
    """Save this period's first odds.json as its opening prediction."""
    o = load(os.path.join(ROOT, "data", "odds.json"), None)
    if not o or not o.get("matchups"):
        return
    mp = str(o["matchup_period"])
    if mp in log and log[mp].get("source") == "live":
        return
    log[mp] = {"source": "live", "captured_utc": o.get("updated_utc"), "matchups": [
        {"home": m["home"], "away": m["away"], "p_home": m["home_win_pct"] / 100, "p_tie": m.get("tie_pct", 0) / 100,
         "cats": {c["cat"]: {"p_home": c["home_win_pct"] / 100, "proj_home": c.get("proj_home"), "proj_away": c.get("proj_away")}
                  for c in m.get("categories", [])}}
        for m in o["matchups"]]}


def backfill(log, names):
    h = load(os.path.join(HERE, "league_posts_history.json"), {})
    periods = {str(v.get("mp")) for v in h.get("daily", {}).values() if v.get("mp")} | set(h.get("opening", {}))
    for mp in periods:
        if mp in log:
            continue
        src, odds = "stored Monday opening odds", h.get("opening", {}).get(mp)
        if not odds:
            days = sorted(k for k, v in h.get("daily", {}).items() if str(v.get("mp")) == mp)
            if not days:
                continue
            src, odds = f"first daily post ({days[0]})", h["daily"][days[0]]["odds"]
        ms = []
        for key, x in odds.items():
            a, b = (int(i) for i in key.split("-"))
            ms.append({"home": names.get(a, str(a)), "away": names.get(b, str(b)),
                       "p_home": x["ph"], "p_tie": x.get("pt", 0), "cats": None})
        log[mp] = {"source": src, "matchups": ms}


def results(d, names):
    """{period: {(home, away): {...}}} for fully finished periods."""
    out = {}
    for m in d["schedule"]:
        if "away" not in m:
            continue
        p = m["matchupPeriodId"]
        out.setdefault(p, {"done": True, "games": {}})
        if m.get("winner") in (None, "UNDECIDED"):
            out[p]["done"] = False
            continue
        h, a = m["home"], m["away"]
        _, sh = S._side_score(h, live=False)
        _, sa = S._side_score(a, live=False)
        wh, wa = S._cats_won(sh, sa)
        out[p]["games"][(names[h["teamId"]], names[a["teamId"]])] = {
            "winner": m["winner"], "score": [len(wh), len(wa), 9 - len(wh) - len(wa)],
            "cats": {S.CN[k]: {"home": sh[k], "away": sa[k],
                               "won": 1.0 if S.CN[k] in wh else 0.0 if S.CN[k] in wa else 0.5} for k in S.CATS}}
    return {p: v["games"] for p, v in out.items() if v["done"] and v["games"]}


def bucket(p):
    lo = min(int(p * 10), 9) * 10
    return f"{lo}-{lo + 10}%"


def build(log, res):
    weeks, all_m, all_c = [], [], []
    for mp in sorted(log, key=int):
        g = res.get(int(mp))
        if not g:
            continue
        rows = []
        for m in log[mp]["matchups"]:
            r = g.get((m["home"], m["away"]))
            if not r:
                continue
            y = 1.0 if r["winner"] == "HOME" else 0.0 if r["winner"] == "AWAY" else 0.5
            p = m["p_home"] + m.get("p_tie", 0) / 2
            fav_home = p >= 0.5
            row = {"home": m["home"], "away": m["away"], "p_home": round(m["p_home"], 3), "score": r["score"],
                   "winner": r["winner"], "fav": m["home"] if fav_home else m["away"],
                   "fav_p": round(max(p, 1 - p), 3), "called": (y == 1.0) == fav_home and y != 0.5,
                   "brier": round((p - y) ** 2, 4), "cats": None}
            all_m.append((max(p, 1 - p), 1.0 if row["called"] else 0.5 if y == 0.5 else 0.0, row["brier"]))
            if m.get("cats"):
                cats = []
                for c, x in m["cats"].items():
                    act = r["cats"].get(c)
                    if not act:
                        continue
                    pc = x["p_home"]
                    cats.append({"cat": c, "p_home": round(pc, 3), "won": act["won"],
                                 "proj_home": x.get("proj_home"), "proj_away": x.get("proj_away"),
                                 "act_home": act["home"], "act_away": act["away"], "brier": round((pc - act["won"]) ** 2, 4)})
                    if act["won"] != 0.5:
                        fp = max(pc, 1 - pc)
                        all_c.append((c, fp, 1.0 if (act["won"] == 1.0) == (pc >= 0.5) else 0.0, (pc - act["won"]) ** 2))
                row["cats"] = cats
            rows.append(row)
        if not rows:
            continue
        cb = [c["brier"] for r in rows for c in (r["cats"] or [])]
        weeks.append({"period": int(mp), "source": log[mp]["source"], "matchups": rows,
                      "called": sum(r["called"] for r in rows), "n": len(rows),
                      "brier": round(sum(r["brier"] for r in rows) / len(rows), 3),
                      "cat_brier": round(sum(cb) / len(cb), 3) if cb else None,
                      "cat_called": sum(1 for r in rows for c in (r["cats"] or []) if c["won"] != 0.5 and (c["won"] == 1.0) == (c["p_home"] >= 0.5)),
                      "cat_n": sum(1 for r in rows for c in (r["cats"] or []) if c["won"] != 0.5)})

    def calib(items):
        b = {}
        for fp, hit in items:
            b.setdefault(bucket(fp), []).append((fp, hit))
        return [{"bucket": k, "n": len(v), "predicted": round(sum(x[0] for x in v) / len(v), 3),
                 "actual": round(sum(x[1] for x in v) / len(v), 3)} for k, v in sorted(b.items())]

    per_cat = {}
    for c, fp, hit, br in all_c:
        per_cat.setdefault(c, []).append((fp, hit, br))
    overall = {
        "matchups": len(all_m), "called": sum(1 for x in all_m if x[1] == 1.0),
        "brier": round(sum(x[2] for x in all_m) / len(all_m), 3) if all_m else None,
        "calibration": calib([(x[0], x[1]) for x in all_m]),
        "categories": len(all_c), "cat_called": sum(x[2] for x in all_c),
        "cat_brier": round(sum(x[3] for x in all_c) / len(all_c), 3) if all_c else None,
        "cat_calibration": calib([(x[1], x[2]) for x in all_c]),
        "by_category": [{"cat": c, "n": len(v), "called": sum(x[1] for x in v),
                         "brier": round(sum(x[2] for x in v) / len(v), 3)} for c, v in per_cat.items()],
    }
    return {"updated_utc": datetime.now(timezone.utc).isoformat(), "overall": overall, "weeks": weeks[::-1]}


def main():
    d = S._get(["mMatchup", "mTeam", "mSettings", "mStatus"])
    names = {t["id"]: t.get("name") for t in d["teams"]}
    log = load(LOG, {})
    capture_opening(log)
    backfill(log, names)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "w") as f:
        json.dump(log, f, indent=1)
    acc = build(log, results(d, names))
    with open(OUT, "w") as f:
        json.dump(acc, f, indent=1)
    o = acc["overall"]
    print(f"accuracy: {len(acc['weeks'])} weeks, {o['called']}/{o['matchups']} matchups called, brier {o['brier']}")


if __name__ == "__main__":
    main()
