"""Trade offers tracker: every offer made in the league, live and past. Writes data/offers.json.

ESPN deletes an offer's player list once it is accepted or declined, so this keeps its own log
(data/offers_log.json, via server._sync_trade_log) and runs every 30 minutes with the live odds.

Each 2-team offer is rated by the model the first time it is seen (rest-of-season expected-wins
change for the team that sent it and the team that received it). Older offers whose players have
since moved can't be rated and are listed without a rating.

Team breakdown (offers each team SENT):
  generosity  - average expected-wins change for the receiving team. High = sends good offers.
  self_gain   - average expected-wins change for the sender.
  best/worst  - the sent offer that was best/worst for the receiver.
Run after live.py. Needs ESPN_S2 / ESPN_SWID.
"""
import json, os, sys, logging
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.environ["TRADE_LOG"] = os.path.join(ROOT, "data", "offers_log.json")
sys.path.insert(0, HERE)
logging.disable(logging.CRITICAL)
import server as S  # noqa: E402

LOG = os.environ["TRADE_LOG"]
OUT = os.path.join(ROOT, "data", "offers.json")
RATE_WINDOW_DAYS = 3  # try to rate an offer for this long after it was made


def backfill_accepted(log):
    """Accepted offers decided before logging began have no players in ESPN's data any more.
    Rebuild them from the trade tracker (data/trades.json): the team that accepted is the receiver."""
    try:
        with open(os.path.join(ROOT, "data", "trades.json")) as f:
            trades = json.load(f)["trades"]
    except Exception:
        return
    ts = lambda iso: datetime.fromisoformat(iso).timestamp()
    for oid, oc in log["outcomes"].items():
        if oc["outcome"] != "accepted" or oid in log["offers"]:
            continue
        cands = [t for t in trades if oc["by"] in t["gets"] and len(t["gets"]) == 2]
        cands.sort(key=lambda t: abs(ts(t["accepted_utc"]) - ts(oc["date"])))
        if not cands or abs(ts(cands[0]["accepted_utc"]) - ts(oc["date"])) > 1.2 * 86400:
            continue
        t = cands[0]
        if any(o.get("trade_key") == t["key"] for o in log["offers"].values()):
            continue
        recv = oc["by"]
        sender = next(n for n in t["gets"] if n != recv)
        log["offers"][oid] = {
            "proposed": oc["date"], "proposed_by": sender, "teams": sorted(t["gets"]),
            "moves": [{"player": p, "from": o, "to": n} for n in t["gets"] for o in t["gets"] if o != n for p in t["gets"][n]],
            "espn_status": "ACCEPTED", "live_now": False, "trade_key": t["key"],
            "rating": t.get("at_time"), "rated_on": "at the time (trade tracker)",
        }


def main():
    log = S._sync_trade_log()
    d = S._get(["mRoster", "mTeam", "mMatchup", "mSettings", "mStatus"])
    names = {t["id"]: t.get("name") for t in d["teams"]}
    ids = {v: k for k, v in names.items()}
    rosters = {t["id"]: S._team_players(t["id"], d)[1] for t in d["teams"]}
    dists = {tid: S._weekly_dist(pl) for tid, pl in rosters.items()}
    weeks = set(S._remaining_weeks(d))
    fixtures = {tid: [] for tid in rosters}
    for m in d["schedule"]:
        if m["matchupPeriodId"] in weeks and "away" in m:
            h, a = m["home"]["teamId"], m["away"]["teamId"]
            fixtures[h].append(a)
            fixtures[a].append(h)

    def exp_wins(tid, dist, override=None):
        override = override or {}
        return sum(S._match_win(S._cat_probs(dist, override.get(o, dists[o]))) for o in fixtures[tid])

    def rate(o):
        teams = [ids.get(n) for n in o["teams"]]
        if len(teams) != 2 or None in teams or not o["moves"]:
            return None
        a, b = teams
        by = lambda frm: {m["player"] for m in o["moves"] if m["from"] == names[frm]}
        a_out, b_out = by(a), by(b)
        a_pl = [p for p in rosters[a] if p["name"] in a_out]
        b_pl = [p for p in rosters[b] if p["name"] in b_out]
        if len(a_pl) != len(a_out) or len(b_pl) != len(b_out):
            return None  # someone has moved since: can't rate fairly
        new_a = [p for p in rosters[a] if p not in a_pl] + b_pl
        new_b = [p for p in rosters[b] if p not in b_pl] + a_pl
        da, db = S._weekly_dist(new_a), S._weekly_dist(new_b)
        ga = (exp_wins(a, da, {b: db}) - exp_wins(a, dists[a])) * S.GAIN_CALIBRATION
        gb = (exp_wins(b, db, {a: da}) - exp_wins(b, dists[b])) * S.GAIN_CALIBRATION
        return {names[a]: round(ga, 3), names[b]: round(gb, 3)}

    now = datetime.now(timezone.utc)
    today = str(now.date())
    backfill_accepted(log)
    for oid, o in log["offers"].items():
        if "rating" in o:
            continue
        r = rate(o)
        if r:
            o["rating"], o["rated_on"] = r, today
        else:
            try:
                old = now - datetime.fromisoformat(o["proposed"]) > timedelta(days=RATE_WINDOW_DAYS)
            except Exception:
                old = True
            if old:
                o["rating"] = None  # give up
    with open(LOG, "w") as f:
        json.dump(log, f, indent=1)

    offers = []
    for oid, o in log["offers"].items():
        sender = o["proposed_by"]
        recv = next((t for t in o["teams"] if t != sender), None)
        r = o.get("rating")
        out = {"id": oid, "proposed": o["proposed"], "sender": sender, "receiver": recv,
               "teams": o["teams"], "outcome": S._offer_outcome(oid, o, log), "moves": o["moves"],
               "sender_gain": r.get(sender) if r else None, "receiver_gain": r.get(recv) if r else None}
        if oid in log["outcomes"]:
            out["decided"] = log["outcomes"][oid]["date"]
        offers.append(out)
    offers.sort(key=lambda x: x["proposed"] or "", reverse=True)

    teams = []
    for tid in sorted(names):
        n = names[tid]
        sent = [x for x in offers if x["sender"] == n]
        rated = [x for x in sent if x["receiver_gain"] is not None]
        recd = [x for x in offers if x["receiver"] == n]
        oc = lambda L, k: sum(1 for x in L if x["outcome"] == k)
        pick = lambda f: (lambda x: {"id": x["id"], "receiver": x["receiver"], "receiver_gain": x["receiver_gain"],
                                     "sender_gain": x["sender_gain"], "moves": x["moves"], "outcome": x["outcome"]})(f(rated, key=lambda x: x["receiver_gain"])) if rated else None
        teams.append({
            "team": n, "sent": len(sent), "received": len(recd), "rated_sent": len(rated),
            "sent_accepted": oc(sent, "accepted"), "sent_declined": oc(sent, "declined"),
            "generosity": round(sum(x["receiver_gain"] for x in rated) / len(rated), 3) if rated else None,
            "self_gain": round(sum(x["sender_gain"] for x in rated) / len(rated), 3) if rated else None,
            "best_sent": pick(max), "worst_sent": pick(min),
        })
    teams.sort(key=lambda t: (t["generosity"] is None, -(t["generosity"] or 0)))
    lost = sum(1 for k in log["outcomes"] if k not in log["offers"])
    with open(OUT, "w") as f:
        json.dump({"updated_utc": now.isoformat(), "offers": offers, "teams": teams,
                   "lost_before_logging": lost}, f, indent=1)
    print(f"offers: {len(offers)} logged, {sum(1 for x in offers if x['receiver_gain'] is not None)} rated, {lost} lost")


if __name__ == "__main__":
    main()
