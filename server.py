"""ESPN Fantasy Basketball MCP server (read-only).

Config via environment variables:
  ESPN_LEAGUE_ID  - your league id (e.g. 665726760)
  ESPN_TEAM_ID    - your team id in the league (e.g. 8)
  ESPN_SEASON     - season year as ESPN labels it (2027 = 2026-27)
  ESPN_S2         - espn_s2 cookie value
  ESPN_SWID       - SWID cookie value, including the {curly braces}
"""
import json
import os
from datetime import datetime, timezone

import httpx
from mcp.server.fastmcp import FastMCP

LEAGUE_ID = os.environ.get("ESPN_LEAGUE_ID", "665726760")
TEAM_ID = int(os.environ.get("ESPN_TEAM_ID", "8"))
SEASON = os.environ.get("ESPN_SEASON", "2027")
BASE = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{SEASON}/segments/0/leagues/{LEAGUE_ID}"
COOKIES = {"espn_s2": os.environ.get("ESPN_S2", ""), "SWID": os.environ.get("ESPN_SWID", "")}

SLOT = {0: "PG", 1: "SG", 2: "SF", 3: "PF", 4: "C", 5: "G", 6: "F", 11: "UTIL", 12: "BENCH", 13: "IR"}
POS = {1: "PG", 2: "SG", 3: "SF", 4: "PF", 5: "C"}
PRO = {0: "FA", 1: "ATL", 2: "BOS", 3: "NOP", 4: "CHI", 5: "CLE", 6: "DAL", 7: "DEN", 8: "DET", 9: "GSW",
       10: "HOU", 11: "IND", 12: "LAC", 13: "LAL", 14: "MIA", 15: "MIL", 16: "MIN", 17: "BKN", 18: "NYK",
       19: "ORL", 20: "PHI", 21: "PHX", 22: "POR", 23: "SAC", 24: "SAS", 25: "OKC", 26: "UTA", 27: "WAS",
       28: "TOR", 29: "MEM", 30: "CHA"}
STAT = {"0": "PTS", "6": "REB", "3": "AST", "2": "STL", "1": "BLK", "17": "3PM", "11": "TO",
        "19": "FG%", "20": "FT%", "40": "MIN", "42": "GP"}
SPLITS = {"season": f"00{SEASON}", "last7": f"01{SEASON}", "last15": f"02{SEASON}",
          "last30": f"03{SEASON}", "last_season": f"00{int(SEASON) - 1}", "projected": f"10{SEASON}"}

mcp = FastMCP("espn-fantasy")


def _get(views, params=None, headers=None, url=BASE):
    q = [("view", v) for v in views] + list((params or {}).items())
    r = httpx.get(url, params=q, cookies=COOKIES, headers=headers or {}, timeout=30, follow_redirects=False)
    if r.status_code in (301, 302, 401, 403):
        raise RuntimeError(f"ESPN returned {r.status_code}. Your espn_s2/SWID cookies are probably missing or expired.")
    r.raise_for_status()
    return r.json()


def _stats(player, split="season"):
    sid = SPLITS.get(split, split)
    for s in player.get("stats", []):
        if s.get("id") == sid and s.get("averageStats"):
            a = s["averageStats"]
            out = {STAT[k]: round(v, 3 if "%" in STAT[k] else 1) for k, v in a.items() if k in STAT}
            out["GP"] = int(s.get("stats", {}).get("42", 0))
            return out
    return None


def _player(p, splits=("season", "last15", "last_season")):
    return {
        "name": p.get("fullName"),
        "id": p.get("id"),
        "nba_team": PRO.get(p.get("proTeamId"), "?"),
        "positions": [SLOT[s] for s in p.get("eligibleSlots", []) if s in (0, 1, 2, 3, 4)],
        "injury": p.get("injuryStatus", "ACTIVE"),
        "pct_owned": round(p.get("ownership", {}).get("percentOwned", 0), 1),
        "stats": {sp: _stats(p, sp) for sp in splits if _stats(p, sp)},
    }


def _teams():
    d = _get(["mTeam"])
    return {t["id"]: t.get("name", f"Team {t['id']}") for t in d["teams"]}


_POOL = {"t": 0, "data": []}


def _pool():
    """Every relevant player (rostered + available), cached for 10 minutes."""
    import time
    if time.time() - _POOL["t"] > 600:
        flt = {"players": {"filterStatus": {"value": ["FREEAGENT", "WAIVERS", "ONTEAM"]}, "limit": 1000,
                           "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
                           "filterStatsForTopScoringPeriodIds": {"value": 5, "additionalValue": [
                               f"00{SEASON}", f"01{SEASON}", f"02{SEASON}", f"10{SEASON}", f"00{int(SEASON) - 1}"]}}}
        _POOL["data"] = _get(["kona_player_info"], headers={"X-Fantasy-Filter": json.dumps(flt)}).get("players", [])
        _POOL["t"] = time.time()
    return _POOL["data"]


@mcp.tool()
def league_overview() -> str:
    """League settings, scoring categories, current week, and standings."""
    d = _get(["mSettings", "mTeam", "mStandings"])
    s = d["settings"]
    teams = sorted(d["teams"], key=lambda t: t.get("playoffSeed", 99))
    return json.dumps({
        "name": s["name"],
        "scoring": s["scoringSettings"]["scoringType"],
        "categories": [STAT.get(str(i["statId"]), i["statId"]) for i in s["scoringSettings"]["scoringItems"]],
        "current_matchup_period": d["status"].get("currentMatchupPeriod"),
        "current_scoring_period": d["scoringPeriodId"],
        "faab_budget": s["acquisitionSettings"].get("acquisitionBudget"),
        "my_team_id": TEAM_ID,
        "standings": [{
            "seed": t.get("playoffSeed"), "id": t["id"], "name": t.get("name"),
            "record": f"{t['record']['overall']['wins']}-{t['record']['overall']['losses']}-{t['record']['overall']['ties']}",
            "faab_spent": t.get("transactionCounter", {}).get("acquisitionBudgetSpent"),
            "adds_this_matchup": t.get("transactionCounter", {}).get("matchupAcquisitionTotals", {}),
        } for t in teams],
    }, indent=1)


@mcp.tool()
def get_roster(team_id: int = 0) -> str:
    """Roster for a team (default: my team) with lineup slot, injury status and per-game stats
    (season, last 15 days, last season). team_id 0 = my team."""
    tid = team_id or TEAM_ID
    d = _get(["mRoster", "mTeam"])
    t = next(t for t in d["teams"] if t["id"] == tid)
    return json.dumps({"team": t.get("name"), "roster": [
        {"slot": SLOT.get(e["lineupSlotId"], e["lineupSlotId"]), **_player(e["playerPoolEntry"]["player"])}
        for e in t["roster"]["entries"]]}, indent=1)


@mcp.tool()
def get_matchup(matchup_period: int = 0, team_id: int = 0) -> str:
    """Category-by-category score for a team's matchup (default: my team, current week)."""
    tid = team_id or TEAM_ID
    d = _get(["mMatchup", "mMatchupScore", "mBoxscore", "mTeam", "mStatus"])
    mp = matchup_period or d["status"].get("currentMatchupPeriod")
    names = {t["id"]: t.get("name") for t in d["teams"]}
    for m in d["schedule"]:
        if m["matchupPeriodId"] == mp and tid in (m["home"]["teamId"], m.get("away", {}).get("teamId")):
            me, opp = (m["home"], m["away"]) if m["home"]["teamId"] == tid else (m["away"], m["home"])
            cats = {}
            src = "cumulativeScoreLive" if (me.get("cumulativeScoreLive") or {}).get("scoreByStat") else "cumulativeScore"
            opp_stats = (opp.get(src) or {}).get("scoreByStat") or {}
            for k, v in ((me.get(src) or {}).get("scoreByStat") or {}).items():
                if k in STAT and k not in ("40", "42"):
                    o = opp_stats.get(k, {})
                    cats[STAT[k]] = {"me": round(v["score"], 3), "opp": round(o.get("score", 0), 3), "result": v.get("result")}
            return json.dumps({"matchup_period": mp, "me": names[tid], "opponent": names[opp["teamId"]],
                               "opponent_id": opp["teamId"],
                               "record": f"{me.get('cumulativeScore', {}).get('wins')}-{me.get('cumulativeScore', {}).get('losses')}-{me.get('cumulativeScore', {}).get('ties')}",
                               "categories": cats}, indent=1)
    return "No matchup found."


@mcp.tool()
def get_free_agents(position: str = "", limit: int = 40, sort_by: str = "last15") -> str:
    """Best available free agents/waiver players. position: PG/SG/SF/PF/C or blank for all.
    sort_by: 'last15', 'last7', 'season' or 'last_season' (ranked by ESPN's rating for that window)."""
    slot = {"PG": 0, "SG": 1, "SF": 2, "PF": 3, "C": 4, "G": 5, "F": 6}.get(position.upper())
    # Pull a wide pool sorted by ownership, then re-rank locally by production in the chosen window.
    flt = {"players": {
        "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
        "limit": max(limit * 4, 150),
        "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        "filterStatsForTopScoringPeriodIds": {"value": 5, "additionalValue": [
            f"00{SEASON}", f"01{SEASON}", f"02{SEASON}", f"10{SEASON}", f"00{int(SEASON) - 1}"]},
    }}
    if slot is not None:
        flt["players"]["filterSlotIds"] = {"value": [slot]}
    d = _get(["kona_player_info"], headers={"X-Fantasy-Filter": json.dumps(flt)})
    players = [_player(x["player"], splits=(sort_by, "season", "last_season")) for x in d.get("players", [])]
    # rank by a simple 9-cat-ish composite for the chosen window so the best producers come first
    def score(p):
        s = p["stats"].get(sort_by) or p["stats"].get("season") or p["stats"].get("last_season") or {}
        return (s.get("PTS", 0) + 1.2 * s.get("REB", 0) + 1.5 * s.get("AST", 0) + 3 * s.get("STL", 0)
                + 3 * s.get("BLK", 0) + s.get("3PM", 0) - s.get("TO", 0))
    players.sort(key=score, reverse=True)
    return json.dumps(players[:limit], indent=1)


@mcp.tool()
def get_player(name: str) -> str:
    """Look up any player by name (rostered or not), with ownership, injury, fantasy team and stats."""
    q = name.lower().strip()
    hits = [x for x in _pool() if q in x["player"].get("fullName", "").lower()][:5]
    names = _teams()
    return json.dumps([{**_player(x["player"], splits=("season", "last7", "last15", "last_season", "projected")),
                        "fantasy_team": names.get(x.get("onTeamId"), "Free agent")}
                       for x in hits], indent=1) if hits else f"No player matching '{name}'."


@mcp.tool()
def get_recent_transactions(limit: int = 25) -> str:
    """Recent league adds, drops and trades (lineup moves and other noise filtered out)."""
    d = _get(["kona_league_communication"])
    names = _teams()
    pnames = {x["player"]["id"]: x["player"].get("fullName") for x in _pool()}
    kinds = {178: "added", 180: "claimed off waivers", 179: "dropped", 181: "dropped",
             239: "dropped", 224: "traded", 225: "traded", 226: "trade accepted", 244: "traded"}
    out = []
    for t in sorted(d.get("communication", {}).get("topics", []), key=lambda t: -t.get("date", 0)):
        moves = []
        for m in t.get("messages", []):
            kind = kinds.get(m.get("messageTypeId"))
            if not kind:
                continue
            team = names.get(m.get("to")) or names.get(m.get("from")) or names.get(m.get("for"))
            moves.append({"team": team, "action": kind, "player": pnames.get(m.get("targetId"), m.get("targetId"))})
        if moves:
            out.append({"date": datetime.fromtimestamp(t["date"] / 1000, timezone.utc).isoformat(), "moves": moves})
        if len(out) >= limit:
            break
    return json.dumps(out, indent=1)


@mcp.tool()
def get_nba_schedule(days: int = 7, early_only: bool = False) -> str:
    """NBA games for the next N days with tip-off times in US Eastern and Melbourne time.
    'before_waivers' = tips off before the daily 6pm ET waiver run, so a player claimed that day
    misses it (add free agents / set lineups the night before instead). early_only=True lists only those."""
    from zoneinfo import ZoneInfo
    et, mel = ZoneInfo("America/New_York"), ZoneInfo("Australia/Melbourne")
    url = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{SEASON}"
    d = _get(["proTeamSchedules_wl"], url=url)
    abbr = {t["id"]: t["abbrev"] for t in d["settings"]["proTeams"]}
    now = datetime.now(timezone.utc).timestamp() * 1000
    seen, by_day = set(), {}
    for team in d["settings"]["proTeams"]:
        for games in (team.get("proGamesByScoringPeriod") or {}).values():
            for g in games:
                key = (g["date"], g.get("homeProTeamId"), g.get("awayProTeamId"))
                if key in seen or not (now - 3 * 3600e3 <= g["date"] <= now + days * 86400e3):
                    continue
                seen.add(key)
                t = datetime.fromtimestamp(g["date"] / 1000, timezone.utc)
                te, tm = t.astimezone(et), t.astimezone(mel)
                early = te.hour < 18
                if early_only and not early:
                    continue
                by_day.setdefault(te.strftime("%Y-%m-%d (%a) US"), []).append({
                    "game": f"{abbr.get(g.get('awayProTeamId'), '?')} @ {abbr.get(g.get('homeProTeamId'), '?')}",
                    "tip_ET": te.strftime("%a %-I:%M%p"), "tip_Melbourne": tm.strftime("%a %d %b %-I:%M%p"),
                    "before_waivers": early, "_ts": g["date"]})
    out = {}
    for day, games in sorted(by_day.items()):
        games.sort(key=lambda x: x["_ts"])
        for x in games:
            x.pop("_ts")
        out[day] = games
    return json.dumps(out, indent=1)


# ---------------------------------------------------------------------------
# MODEL: projections, NBA schedule, lineup sim, matchup / pickup / trade tools
# ---------------------------------------------------------------------------
import math
import random
import time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
KEYS = ["0", "6", "3", "2", "1", "17", "11", "13", "14", "15", "16"]  # + FGM FGA FTM FTA
CATS = ["0", "6", "3", "2", "1", "17", "11", "19", "20"]
CN = {"0": "PTS", "6": "REB", "3": "AST", "2": "STL", "1": "BLK", "17": "3PM", "11": "TO", "19": "FG%", "20": "FT%"}
SLOTS = [0, 1, 2, 3, 4, 5, 6, 11, 11, 11]  # PG SG SF PF C G F UT UT UT
# typical waiver-wire player per game (used to shrink small samples with no projection)
REPLACEMENT = {"0": 8.0, "6": 3.5, "3": 1.8, "2": 0.7, "1": 0.4, "17": 0.9, "11": 1.0,
               "13": 3.1, "14": 7.0, "15": 1.0, "16": 1.3}
VAR_SCALE = 1.8
# The fast season model overstates the effect of roster changes ~2x versus the full day-by-day
# simulator (checked on 2-for-2 trades, Oct 2026), so reported win changes are scaled by this.
GAIN_CALIBRATION = 0.5  # widens week-to-week randomness (calibrated so single moves don't swing too much)
PLAY_PROB = {"ACTIVE": 0.93, "DAY_TO_DAY": 0.6, "QUESTIONABLE": 0.5, "OUT": 0.0, "INJURY_RESERVE": 0.0, "SUSPENSION": 0.0}
_SCHED = {"t": 0, "data": None}


def _avg(p, sid):
    for s in p.get("stats", []):
        if s.get("id") == sid and s.get("averageStats"):
            return s["averageStats"], int(s.get("stats", {}).get("42", 0) or 0)
    return None, 0


def _rates(p):
    """Per-game projection: this season's averages blended with ESPN projection / last season.
    This season gets more weight as games accumulate (weight = gp / (gp + 15))."""
    cur, gp = _avg(p, f"00{SEASON}")
    prior, _ = _avg(p, f"10{SEASON}")
    if not prior:
        prior, _ = _avg(p, f"00{int(SEASON) - 1}")
    if not prior and not cur:
        return None
    if not prior:
        prior = REPLACEMENT  # no ESPN projection/history: assume a fringe player until proven otherwise
    w = gp / (gp + 15) if cur else 0
    return {k: w * (cur or {}).get(k, 0) + (1 - w) * prior.get(k, 0) for k in KEYS}


def _value(r):
    return (r["0"] + 1.2 * r["6"] + 1.5 * r["3"] + 3 * r["2"] + 3 * r["1"] + r["17"] - r["11"]) if r else 0


def _pinfo(p, slot=None):
    return {"id": p["id"], "name": p.get("fullName"), "pro": p.get("proTeamId", 0),
            "elig": [s for s in p.get("eligibleSlots", []) if s in (0, 1, 2, 3, 4, 5, 6, 11)],
            "status": p.get("injuryStatus", "ACTIVE") or "ACTIVE", "rates": _rates(p), "slot": slot}


def _schedule():
    """{proTeamId: [game datetime ET, ...]} for the season, cached 6h."""
    if time.time() - _SCHED["t"] > 6 * 3600 or _SCHED["data"] is None:
        url = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{SEASON}"
        d = _get(["proTeamSchedules_wl"], url=url)
        out = {}
        for team in d["settings"]["proTeams"]:
            for games in (team.get("proGamesByScoringPeriod") or {}).values():
                for g in games:
                    out.setdefault(team["id"], []).append(datetime.fromtimestamp(g["date"] / 1000, ET))
        _SCHED.update(t=time.time(), data=out)
    return _SCHED["data"]


def _week_days(weeks_ahead=0):
    """US-Eastern dates of a fantasy week (Mon-Sun). weeks_ahead=0 -> rest of current week from today."""
    from datetime import timedelta
    today = datetime.now(ET).date()
    monday = today - timedelta(days=today.weekday())
    start = monday + timedelta(weeks=weeks_ahead)
    days = [start + timedelta(days=i) for i in range(7)]
    return [d for d in days if d >= today] if weeks_ahead == 0 else days


def _games_on(pro, day, sched, after=None):
    for g in sched.get(pro, []):
        if g.date() == day and (after is None or g > after):
            return True
    return False


def _next_claim_time(now):
    """FAAB claims process daily at 6 PM ET; a bid placed now lands at the next run."""
    from datetime import timedelta
    t = now.replace(hour=18, minute=0, second=0, microsecond=0)
    return t if now < t else t + timedelta(days=1)


def _can_play(p, day, sched, now=None):
    """Has a game on `day` that hasn't started yet, and (for a pending pickup with
    p['active_from']) tips off after the claim processes."""
    cut = now if (now and day == now.date()) else None
    af = p.get("active_from")
    if af:
        if day < af.date():
            return False
        if day == af.date():
            cut = max(cut, af) if cut else af
    return _games_on(p["pro"], day, sched, cut)


def _daily_lineup(players):
    """Greedy: best players first, each into the most restrictive open slot they're eligible for."""
    open_slots = list(SLOTS)
    chosen = []
    for p in sorted(players, key=lambda p: -_value(p["rates"])):
        fits = [s for s in open_slots if s in p["elig"]]
        if not fits:
            continue
        s = min(fits, key=lambda s: (s == 11, s in (5, 6)))  # fill specific > G/F > UTIL
        open_slots.remove(s)
        chosen.append(p)
    return chosen


def _team_players(team_id, d=None):
    d = d or _get(["mRoster", "mTeam"])
    t = next(t for t in d["teams"] if t["id"] == team_id)
    return t.get("name"), [_pinfo(e["playerPoolEntry"]["player"], e["lineupSlotId"]) for e in t["roster"]["entries"]]


def _sim_totals(players, days, sched, rng, now=None):
    tot = dict.fromkeys(KEYS, 0.0)
    for day in days:
        playing = [p for p in players if p["rates"] and p["slot"] != 13
                   and _can_play(p, day, sched, now)]
        for p in _daily_lineup(playing):
            if rng.random() > PLAY_PROB.get(p["status"], 0.8):
                continue
            f = max(0.2, rng.gauss(1.0, 0.25))  # game-to-game form (minutes, shooting night)
            for k in KEYS:
                m = p["rates"][k] * f
                tot[k] += max(0.0, rng.gauss(m, math.sqrt(m) * 0.9)) if m > 0 else 0
    return tot


def _cats(t):
    c = {k: t.get(k, 0) for k in CATS[:7]}
    c["19"] = t["13"] / t["14"] if t.get("14") else 0
    c["20"] = t["15"] / t["16"] if t.get("16") else 0
    return c


def _beats(k, a, b):
    return a < b if k == "11" else a > b


def _matchup_odds(mine, theirs, days, sched, base_me=None, base_opp=None, n=1000, seed=7, now=None):
    rng = random.Random(seed)
    base_me = base_me or dict.fromkeys(KEYS, 0.0)
    base_opp = base_opp or dict.fromkeys(KEYS, 0.0)
    wins = ties = 0
    cw = dict.fromkeys(CATS, 0)
    proj_me = dict.fromkeys(CATS, 0.0)
    proj_opp = dict.fromkeys(CATS, 0.0)
    for _ in range(n):
        a = _sim_totals(mine, days, sched, rng, now)
        b = _sim_totals(theirs, days, sched, rng, now)
        a = {k: a[k] + base_me.get(k, 0) for k in KEYS}
        b = {k: b[k] + base_opp.get(k, 0) for k in KEYS}
        ca, cb = _cats(a), _cats(b)
        wa = wb = 0
        for k in CATS:
            proj_me[k] += ca[k] / n
            proj_opp[k] += cb[k] / n
            if _beats(k, ca[k], cb[k]):
                wa += 1
                cw[k] += 1
            elif ca[k] != cb[k]:
                wb += 1
        wins += wa > wb
        ties += wa == wb
    rnd = lambda k, v: round(v, 3) if k in ("19", "20") else round(v, 1)
    return {"win_prob": round(wins / n, 2), "tie_prob": round(ties / n, 2),
            "categories": {CN[k]: {"win_prob": round(cw[k] / n, 2), "proj_me": rnd(k, proj_me[k]),
                                   "proj_opp": rnd(k, proj_opp[k])} for k in CATS}}


def _current_matchup(d, tid):
    mp = d["status"].get("currentMatchupPeriod")
    for m in d["schedule"]:
        if m["matchupPeriodId"] == mp and tid in (m["home"]["teamId"], m.get("away", {}).get("teamId")):
            me, opp = (m["home"], m["away"]) if m["home"]["teamId"] == tid else (m["away"], m["home"])
            def base(side):
                src = side.get("cumulativeScoreLive") if (side.get("cumulativeScoreLive") or {}).get("scoreByStat") \
                    else side.get("cumulativeScore") or {}
                sbs = src.get("scoreByStat") or {}
                return {k: float((sbs.get(k) or {}).get("score", 0) or 0) for k in KEYS}
            return mp, opp["teamId"], base(me), base(opp)
    return mp, None, None, None


@mcp.tool()
def simulate_week(team_id: int = 0) -> str:
    """LIVE matchup forecast: starts from this week's actual category totals, then simulates every
    remaining game this week (real NBA schedule, daily 10-man lineup limits, injuries) 1000 times.
    Returns win probability overall and per category, projected final totals, and games left per team.
    Use it to decide mid-week which categories to chase, protect, or give up."""
    tid = team_id or TEAM_ID
    d = _get(["mRoster", "mTeam", "mMatchup", "mMatchupScore", "mStatus"])
    mp, opp_id, base_me, base_opp = _current_matchup(d, tid)
    if opp_id is None:
        return "No current matchup found."
    sched = _schedule()
    now = datetime.now(ET)
    days = _week_days(0)
    names = {t["id"]: t.get("name") for t in d["teams"]}
    _, mine = _team_players(tid, d)
    _, theirs = _team_players(opp_id, d)

    def games_left(players):
        total = 0
        for day in days:
            playing = [p for p in players if p["rates"] and p["slot"] != 13 and PLAY_PROB.get(p["status"], 0.8) > 0
                       and _games_on(p["pro"], day, sched, now if day == now.date() else None)]
            total += len(_daily_lineup(playing))
        return total

    res = _matchup_odds(mine, theirs, days, sched, base_me, base_opp, n=1000, now=now)
    cur_me, cur_opp = _cats(base_me), _cats(base_opp)
    for k in CATS:
        res["categories"][CN[k]]["now_me"] = round(cur_me[k], 3 if k in ("19", "20") else 1)
        res["categories"][CN[k]]["now_opp"] = round(cur_opp[k], 3 if k in ("19", "20") else 1)
    res.update({"matchup_period": mp, "me": names[tid], "opponent": names[opp_id],
                "days_left_US": [str(x) for x in days],
                "starter_games_left": {"me": games_left(mine), "opp": games_left(theirs)},
                "note": "Assumes week ends Sunday US time. Opening and All-Star weeks can run longer."})
    _safe_log("simulate_week", mp, {"team_id": tid, "opp_id": opp_id, **res},
              players={"me": mine, "opp": theirs})
    return json.dumps(res, indent=1)


# ---- season-level analytic model (fast; used for pickups and trades) ----

def _weekly_dist(players, games_per_player=3.4, cap=38.0):
    """Mean & variance of weekly category totals for a roster, using its best players up to
    ~10 starters x 3.4 games of capacity. Analytic (normal approx) so it's fast."""
    ps = sorted([p for p in players if p["rates"] and p["slot"] != 13 and PLAY_PROB.get(p["status"], 0.8) > 0],
                key=lambda p: -_value(p["rates"]))
    mean = dict.fromkeys(KEYS, 0.0)
    var = dict.fromkeys(KEYS, 0.0)
    used = 0.0
    for p in ps:
        g = min(games_per_player * PLAY_PROB.get(p["status"], 0.8), max(0.0, cap - used))
        if g <= 0:
            break
        used += g
        for k in KEYS:
            m = p["rates"][k]
            mean[k] += g * m
            var[k] += g * (0.8 * m + 0.07 * m * m) + 0.5 * m * m  # per-game noise + games-played noise
    return mean, var


def _cat_probs(da, db):
    (ma, va), (mb, vb) = da, db
    out = {}
    for k in CATS[:7]:
        sd = math.sqrt(VAR_SCALE * (va[k] + vb[k])) or 1
        z = (ma[k] - mb[k]) / sd
        out[k] = 0.5 * (1 + math.erf((-z if k == "11" else z) / math.sqrt(2)))
    for k, (mk, ak) in {"19": ("13", "14"), "20": ("15", "16")}.items():
        if not ma[ak] or not mb[ak]:
            out[k] = 0.5
            continue
        pa, pb = ma[mk] / ma[ak], mb[mk] / mb[ak]
        sd = math.sqrt(pa * (1 - pa) / ma[ak] * 3 * VAR_SCALE + pb * (1 - pb) / mb[ak] * 3 * VAR_SCALE) or 1  # x3: shooting is streaky
        out[k] = 0.5 * (1 + math.erf((pa - pb) / sd / math.sqrt(2)))
    return out


def _match_win(probs):
    """P(win more of the 9 categories than opponent), ties counted as half."""
    dist = [1.0]
    for p in probs.values():
        new = [0.0] * (len(dist) + 1)
        for i, x in enumerate(dist):
            new[i] += x * (1 - p)
            new[i + 1] += x * p
        dist = new
    return sum(dist[5:])


def _fixture_value(my_players, d, tid, weeks):
    """Expected wins over the given future matchup periods."""
    mine = _weekly_dist(my_players)
    exp = 0.0
    rows = []
    for m in d["schedule"]:
        if m["matchupPeriodId"] in weeks and tid in (m["home"]["teamId"], m.get("away", {}).get("teamId")):
            o = m["away"]["teamId"] if m["home"]["teamId"] == tid else m["home"]["teamId"]
            _, op = _team_players(o, d)
            w = _match_win(_cat_probs(mine, _weekly_dist(op)))
            exp += w
            rows.append((m["matchupPeriodId"], o, w))
    return exp, rows


def _remaining_weeks(d):
    cur = d["status"].get("currentMatchupPeriod") or 1
    total = d["settings"]["scheduleSettings"]["matchupPeriodCount"]
    return list(range(cur, total + 1))


@mcp.tool()
def rank_pickups(position: str = "", limit: int = 15, pool_size: int = 120, this_week_top: int = 10) -> str:
    """Ranks free agents by how much they raise MY expected wins over the rest of the season (not by
    generic value): each candidate is swapped in for my least valuable player and every remaining
    fixture is re-scored. Category-aware, so it automatically favours what my team needs and ignores
    categories I'm punting. Also shows the suggested drop and games left this week.
    For the top `this_week_top` candidates it also re-runs the LIVE matchup simulation with the
    add/drop applied: this_week_win_prob_change, future_weeks_expected_wins_change (season value
    excluding the current week), and total_value = sum of the two (the decision number: >0 = do it)."""
    d = _get(["mRoster", "mTeam", "mSettings", "mStatus", "mMatchup", "mMatchupScore"])
    weeks = _remaining_weeks(d)
    _, mine = _team_players(TEAM_ID, d)
    base, _ = _fixture_value(mine, d, TEAM_ID, weeks)
    # drop candidate = player whose removal costs least
    droppable = [p for p in mine if p["slot"] != 13]
    drop_cost = []
    for p in droppable:
        v, _ = _fixture_value([q for q in mine if q is not p], d, TEAM_ID, weeks)
        drop_cost.append((base - v, p))
    drop_cost.sort(key=lambda x: x[0])
    drop = drop_cost[0][1]
    fa = json.loads(get_free_agents(position=position, limit=pool_size, sort_by="season"))
    pool = {x["player"]["id"]: x["player"] for x in _pool() if x.get("onTeamId", 0) == 0}
    sched = _schedule()
    days = _week_days(0)
    now = datetime.now(ET)
    out = []
    cand_by_name = {}
    for f in fa:
        p = pool.get(f["id"])
        if not p:
            continue
        cand = _pinfo(p, 12)
        cand["active_from"] = _next_claim_time(now)  # waiver bid -> playable only after next 6 PM ET run
        if cand["pro"] == 0:  # unsigned
            continue
        if not cand["rates"] or PLAY_PROB.get(cand["status"], 0.8) == 0:
            continue
        cand_by_name[cand["name"]] = cand
        v, _ = _fixture_value([q for q in mine if q is not drop] + [cand], d, TEAM_ID, weeks)
        gl = sum(_can_play(cand, day, sched, now) for day in days)
        out.append({"name": cand["name"], "nba_team": PRO.get(cand["pro"], "?"), "injury": cand["status"],
                    "pct_owned": f["pct_owned"], "added_expected_wins": round((v - base) * GAIN_CALIBRATION, 2),
                    "games_left_this_week": gl, "per_game": {CN[k]: round(cand["rates"][k], 1) for k in CATS[:7]}})
    out.sort(key=lambda x: -x["added_expected_wins"])
    # ---- this-week view: live matchup sim with the add/drop applied ----
    tw = {}
    mp, opp_id, base_me, base_opp = _current_matchup(d, TEAM_ID)
    if opp_id is not None and this_week_top > 0:
        _, theirs = _team_players(opp_id, d)
        sim = lambda roster: _matchup_odds(roster, theirs, days, sched, base_me, base_opp, n=1000, now=now)["win_prob"]
        tw_base = sim(mine)  # same seed for every run -> common random numbers, fair comparison
        fut = weeks[1:]
        fut_base, _ = _fixture_value(mine, d, TEAM_ID, fut) if fut else (0.0, None)
        for x in out[:this_week_top]:
            cand = cand_by_name.get(x["name"])
            if cand is None:
                continue
            roster = [q for q in mine if q is not drop] + [cand]
            dw = sim(roster) - tw_base
            fv = (_fixture_value(roster, d, TEAM_ID, fut)[0] - fut_base) * GAIN_CALIBRATION if fut else 0.0
            x["this_week_win_prob_change"] = round(dw, 2)
            x["future_weeks_expected_wins_change"] = round(fv, 2)
            x["total_value"] = round(dw + fv, 2)
        tw = {"this_week_win_prob_now": tw_base,
              "claims_process_at_ET": _next_claim_time(now).strftime("%a %d %b %I:%M %p")}
    _safe_log("rank_pickups", d["status"].get("currentMatchupPeriod"),
              {"drop": drop["name"], "pickups": out[:limit], "position": position})
    return json.dumps({"baseline_expected_wins_rest_of_season": round(base, 2),
                       "weeks_counted": f"{weeks[0]}-{weeks[-1]}",
                       **tw,
                       "suggested_drop": drop["name"],
                       "drop_options_cheapest_first": [{"name": p["name"], "expected_wins_lost": round(c * GAIN_CALIBRATION, 2)}
                                                       for c, p in drop_cost[:4]],
                       "pickups": out[:limit]}, indent=1)


@mcp.tool()
def evaluate_trade(give: list[str], get: list[str], other_team_id: int = 0) -> str:
    """Trade checker: expected wins for the rest of the season before vs after a trade, for me and
    (if other_team_id given or found) for the other team. give/get = player names (partial ok)."""
    d = _get(["mRoster", "mTeam", "mSettings", "mStatus", "mMatchup"])
    weeks = _remaining_weeks(d)
    _, mine = _team_players(TEAM_ID, d)

    def find(name, players):
        q = name.lower()
        return next((p for p in players if q in (p["name"] or "").lower()), None)

    out_p = [find(n, mine) for n in give]
    if None in out_p:
        return f"Couldn't find on my roster: {[n for n, p in zip(give, out_p) if p is None]}"
    other = other_team_id
    if not other:
        for t in d["teams"]:
            if t["id"] != TEAM_ID and get:
                _, pl = _team_players(t["id"], d)
                if find(get[0], pl):
                    other = t["id"]
                    break
    if not other:
        return f"Couldn't find which team has {get[0]}."
    oname, theirs = _team_players(other, d)
    in_p = [find(n, theirs) for n in get]
    if None in in_p:
        return f"Couldn't find on {oname}: {[n for n, p in zip(get, in_p) if p is None]}"
    before, rows_b = _fixture_value(mine, d, TEAM_ID, weeks)
    new_mine = [p for p in mine if p not in out_p] + in_p
    after, rows_a = _fixture_value(new_mine, d, TEAM_ID, weeks)
    ob, _ = _fixture_value(theirs, d, other, weeks)
    oa, _ = _fixture_value([p for p in theirs if p not in in_p] + out_p, d, other, weeks)
    league = {}
    for t in d["teams"]:
        if t["id"] != TEAM_ID:
            league[t["id"]] = _weekly_dist(_team_players(t["id"], d)[1])
    def cat_profile(players):
        me_d = _weekly_dist(players)
        probs = [_cat_probs(me_d, od) for od in league.values()]
        return {CN[k]: round(sum(p[k] for p in probs) / len(probs), 2) for k in CATS}
    names = {t["id"]: t.get("name") for t in d["teams"]}
    _safe_log("evaluate_trade", d["status"].get("currentMatchupPeriod"),
              {"give": [p["name"] for p in out_p], "get": [p["name"] for p in in_p], "with": oname,
               "my_change": round((after - before) * GAIN_CALIBRATION, 2),
               "rates_in": {p["name"]: p["rates"] for p in in_p}, "rates_out": {p["name"]: p["rates"] for p in out_p}})
    return json.dumps({
        "trade": {"give": [p["name"] for p in out_p], "get": [p["name"] for p in in_p], "with": oname},
        "my_expected_wins_change": round((after - before) * GAIN_CALIBRATION, 2),
        "their_expected_wins_change": round((oa - ob) * GAIN_CALIBRATION, 2),
        "my_category_win_rate_vs_league": {"before": cat_profile(mine), "after": cat_profile(new_mine)},
        "by_week": [{"week": wk, "opponent": names[o], "before": round(wb, 2), "after": round(wa, 2)}
                    for (wk, o, wb), (_, _, wa) in zip(rows_b, rows_a)],
        "weeks_counted": f"{weeks[0]}-{weeks[-1]}"}, indent=1)


@mcp.tool()
def season_outlook() -> str:
    """Rest-of-season forecast: my expected wins by remaining week, and league power rankings
    (each team's average chance of beating every other team)."""
    d = _get(["mRoster", "mTeam", "mSettings", "mStatus", "mMatchup", "mStandings"])
    weeks = _remaining_weeks(d)
    names = {t["id"]: t.get("name") for t in d["teams"]}
    dists = {t["id"]: _weekly_dist(_team_players(t["id"], d)[1]) for t in d["teams"]}
    power = {a: sum(_match_win(_cat_probs(dists[a], dists[b])) for b in dists if b != a) / (len(dists) - 1)
             for a in dists}
    _, mine = _team_players(TEAM_ID, d)
    exp, rows = _fixture_value(mine, d, TEAM_ID, weeks)
    rec = next(t for t in d["teams"] if t["id"] == TEAM_ID)["record"]["overall"]
    _safe_log("season_outlook", d["status"].get("currentMatchupPeriod"),
              {"fixture": [{"week": wk, "opp_id": o, "win_prob": round(w, 4)} for wk, o, w in rows]})
    return json.dumps({
        "current_record": f"{rec['wins']}-{rec['losses']}-{rec['ties']}",
        "expected_wins_remaining": round(exp, 1),
        "fixture": [{"week": wk, "opponent": names[o], "win_prob": round(w, 2)} for wk, o, w in rows],
        "power_rankings": [{"team": names[t], "avg_win_prob": round(v, 2)}
                           for t, v in sorted(power.items(), key=lambda x: -x[1])]}, indent=1)



# ---------------------------------------------------------------------------
# LEAGUE UPDATE (for the group-chat post)
# ---------------------------------------------------------------------------
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "power_history.json")


def _power_rankings(d):
    dists = {t["id"]: _weekly_dist(_team_players(t["id"], d)[1]) for t in d["teams"]}
    return {a: sum(_match_win(_cat_probs(dists[a], dists[b])) for b in dists if b != a) / (len(dists) - 1)
            for a in dists}


def _day_line(p, sp):
    """A player's actual stat line for scoring period sp (one NBA day), or None."""
    for s in p.get("stats", []):
        if s.get("scoringPeriodId") == sp and s.get("statSourceId") == 0 and s.get("statSplitTypeId") == 5 and s.get("stats"):
            st = s["stats"]
            if not st.get("40") and not st.get("14"):
                return None
            return st
    return None


@mcp.tool()
def league_update_data(scoring_period: int = 0, matchup_period: int = 0) -> str:
    """Everything for a league-wide update post: every matchup's live category score, standings,
    model power rankings with movement since the last saved snapshot, the day's best and worst
    performances (starters), notable bench points left on the table, and recent transactions.
    scoring_period 0 = the latest NBA day ESPN has scored. matchup_period 0 = current week; pass the
    previous week's number to get its FINAL results for a weekly roundup."""
    base = _get(["mStatus", "mSettings"])
    sp = scoring_period or base["status"].get("latestScoringPeriod") or base.get("scoringPeriodId")
    d = _get(["mMatchup", "mMatchupScore", "mTeam", "mStatus", "mStandings", "mRoster", "mSettings"],
             params={"scoringPeriodId": sp})
    names = {t["id"]: t.get("name") for t in d["teams"]}
    mp = matchup_period or d["status"].get("currentMatchupPeriod")

    def side_cats(side):
        src = side.get("cumulativeScoreLive") if (side.get("cumulativeScoreLive") or {}).get("scoreByStat") \
            else side.get("cumulativeScore") or {}
        return src, src.get("scoreByStat") or {}

    matchups = []
    for m in d["schedule"]:
        if m["matchupPeriodId"] != mp or "away" not in m:
            continue
        (hs, hc), (as_, ac) = side_cats(m["home"]), side_cats(m["away"])
        cats = {}
        for k in CATS:
            if k in hc:
                cats[CN[k]] = [round(hc[k].get("score", 0), 3), round(ac.get(k, {}).get("score", 0), 3)]
        matchups.append({"home": names[m["home"]["teamId"]], "away": names[m["away"]["teamId"]],
                         "score": f"{hs.get('wins', 0)}-{as_.get('wins', 0)}-{hs.get('ties', 0)}",
                         "categories_home_vs_away": cats})

    standings = sorted(d["teams"], key=lambda t: t.get("playoffSeed", 99))
    standings = [{"seed": t.get("playoffSeed"), "team": t.get("name"),
                  "record": f"{t['record']['overall']['wins']}-{t['record']['overall']['losses']}-{t['record']['overall']['ties']}",
                  "faab_left": 500 - (t.get("transactionCounter", {}).get("acquisitionBudgetSpent") or 0)}
                 for t in standings]

    power = _power_rankings(d)
    order = sorted(power, key=lambda t: -power[t])
    prev, prev_pct = {}, {}
    try:
        with open(HISTORY_FILE) as f:
            hist = json.load(f)
        prev = {int(k): v for k, v in hist.get("ranks", {}).items()}
        prev_pct = {int(k): v for k, v in hist.get("pct", {}).items()}
        prev_when = hist.get("saved")
    except Exception:
        prev_when = None
    ranks = []
    for i, t in enumerate(order, 1):
        move = (prev[t] - i) if t in prev else None
        pct = round(power[t] * 100)
        ranks.append({"rank": i, "team": names[t], "chance_vs_avg_team_pct": pct, "rank_move": move,
                      "pct_change": (pct - prev_pct[t]) if t in prev_pct else None})
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump({"saved": datetime.now(timezone.utc).isoformat(),
                       "ranks": {str(t): i for i, t in enumerate(order, 1)},
                       "pct": {str(t): round(power[t] * 100) for t in order}}, f)
    except Exception:
        pass

    perfs, bench = [], []
    for t in d["teams"]:
        for e in t["roster"]["entries"]:
            p = e["playerPoolEntry"]["player"]
            st = _day_line(p, sp)
            if not st:
                continue
            score = (st.get("0", 0) + 1.2 * st.get("6", 0) + 1.5 * st.get("3", 0) + 3 * st.get("2", 0)
                     + 3 * st.get("1", 0) + st.get("17", 0) - 1.5 * st.get("11", 0))
            line = {"player": p.get("fullName"), "team": t.get("name"),
                    "line": f"{int(st.get('0', 0))} pts, {int(st.get('6', 0))} reb, {int(st.get('3', 0))} ast, "
                            f"{int(st.get('2', 0))} stl, {int(st.get('1', 0))} blk, {int(st.get('17', 0))} 3pm, "
                            f"{int(st.get('11', 0))} to, {int(st.get('13', 0))}/{int(st.get('14', 0))} FG, "
                            f"{int(st.get('15', 0))}/{int(st.get('16', 0))} FT",
                    "score": round(score, 1)}
            (bench if e["lineupSlotId"] in (12, 13) else perfs).append(line)
    perfs.sort(key=lambda x: -x["score"])
    bench.sort(key=lambda x: -x["score"])

    return json.dumps({
        "scoring_period": sp, "matchup_period": mp,
        "matchups": matchups, "standings": standings,
        "power_rankings": ranks, "power_rankings_previous_snapshot": prev_when,
        "top_performances": perfs[:5], "worst_performances": perfs[-3:][::-1] if len(perfs) > 5 else [],
        "bench_blunders": [b for b in bench if b["score"] >= 25][:3],
        "recent_transactions": json.loads(get_recent_transactions(limit=15)),
    }, indent=1)


# ---------------------------------------------------------------------------
# TRADE FINDER
# ---------------------------------------------------------------------------
def _espn_rank(pid, pool_by_id):
    p = pool_by_id.get(pid, {})
    r = (p.get("draftRanksByRankType") or {}).get("STANDARD", {}).get("rank")
    return r or 999


@mcp.tool()
def find_trades(max_results: int = 5, min_my_gain: float = 0.1, partner_team_id: int = 0) -> str:
    """Searches realistic 1-for-1 and 2-for-2 trades with every other team and returns the best ones
    that (1) raise MY expected wins for the rest of the season, (2) also raise the PARTNER's expected
    wins (so they'd want it), (3) look fair on paper (similar ESPN rank / generic per-game value, so it's
    accepted and not vetoed), and (4) don't hand a big boost to a direct playoff rival. Head-to-head
    meetings with the partner are re-scored with their new roster. For each trade it returns both teams'
    gains and the categories each side improves - use those to write the pitch message."""
    d = _get(["mRoster", "mTeam", "mSettings", "mStatus", "mMatchup"])
    weeks = set(_remaining_weeks(d))
    names = {t["id"]: t.get("name") for t in d["teams"]}
    rosters = {t["id"]: _team_players(t["id"], d)[1] for t in d["teams"]}
    dists = {tid: _weekly_dist(pl) for tid, pl in rosters.items()}
    fixtures = {tid: [] for tid in rosters}
    for m in d["schedule"]:
        if m["matchupPeriodId"] in weeks and "away" in m:
            h, a = m["home"]["teamId"], m["away"]["teamId"]
            fixtures[h].append(a)
            fixtures[a].append(h)
    pool_by_id = {x["player"]["id"]: x["player"] for x in _pool()}
    power = _power_rankings(d)
    power_rank = {t: i for i, t in enumerate(sorted(power, key=lambda t: -power[t]), 1)}

    def exp_wins(tid, dist, override=None):
        override = override or {}
        return sum(_match_win(_cat_probs(dist, override.get(o, dists[o]))) for o in fixtures[tid])

    def league_cats(dist, exclude):
        probs = [_cat_probs(dist, dists[o]) for o in dists if o != exclude]
        return {CN[k]: sum(p[k] for p in probs) / len(probs) for k in CATS}

    me = TEAM_ID
    base_me = exp_wins(me, dists[me])
    tradeable = lambda pl: [p for p in pl if p["rates"] and p["slot"] != 13]
    mine = tradeable(rosters[me])
    results = []
    partners = [partner_team_id] if partner_team_id else [t for t in rosters if t != me]
    for pt in partners:
        theirs = tradeable(rosters[pt])
        base_pt = exp_wins(pt, dists[pt])

        def evaluate(give, get):
            new_me = [p for p in rosters[me] if p not in give] + list(get)
            new_pt = [p for p in rosters[pt] if p not in get] + list(give)
            dm, dp = _weekly_dist(new_me), _weekly_dist(new_pt)
            gm = (exp_wins(me, dm, {pt: dp}) - base_me) * GAIN_CALIBRATION
            gp = (exp_wins(pt, dp, {me: dm}) - base_pt) * GAIN_CALIBRATION
            return gm, gp, dm, dp

        singles = []
        for g in mine:
            for r in theirs:
                gm, gp, _, _ = evaluate([g], [r])
                singles.append((gm, gp, g, r))
        cands = [([g], [r], gm, gp) for gm, gp, g, r in singles]
        # 2-for-2 from the most promising legs
        good_give = sorted({id(x[2]): x[2] for x in sorted(singles, key=lambda x: -(x[0] + x[1]))[:40]}.values(),
                           key=lambda p: -_value(p["rates"]))[:5]
        good_get = sorted({id(x[3]): x[3] for x in sorted(singles, key=lambda x: -(x[0] + x[1]))[:40]}.values(),
                          key=lambda p: -_value(p["rates"]))[:5]
        for i in range(len(good_give)):
            for j in range(i + 1, len(good_give)):
                for k in range(len(good_get)):
                    for l in range(k + 1, len(good_get)):
                        give, get = [good_give[i], good_give[j]], [good_get[k], good_get[l]]
                        gm, gp, _, _ = evaluate(give, get)
                        cands.append((give, get, gm, gp))
        for give, get, gm, gp in cands:
            if gm < min_my_gain or gp <= 0.01:
                continue
            vg = sum(_value(p["rates"]) for p in give)
            vr = sum(_value(p["rates"]) for p in get)
            rg = sum(_espn_rank(p["id"], pool_by_id) for p in give) / len(give)
            rr = sum(_espn_rank(p["id"], pool_by_id) for p in get) / len(get)
            value_ratio = vr / vg if vg else 9
            looks_fair = 0.85 <= value_ratio <= 1.12 and rg <= rr * 1.35 + 15
            if not looks_fair:
                continue
            rival = power_rank[pt] <= 9  # in or near the playoff race
            score = gm - (0.4 * gp if rival else 0.1 * gp)
            results.append({"score": score, "pt": pt, "give": give, "get": get, "gm": gm, "gp": gp,
                            "vr": value_ratio, "rg": rg, "rr": rr, "rival": rival})
    results.sort(key=lambda x: -x["score"])
    out, seen = [], set()
    for r in results:
        key = (r["pt"], tuple(sorted(p["name"] for p in r["give"])))
        if key in seen:
            continue
        seen.add(key)
        new_me = [p for p in rosters[me] if p not in r["give"]] + r["get"]
        new_pt = [p for p in rosters[r["pt"]] if p not in r["get"]] + r["give"]
        before_me, after_me = league_cats(dists[me], me), league_cats(_weekly_dist(new_me), me)
        before_pt, after_pt = league_cats(dists[r["pt"]], r["pt"]), league_cats(_weekly_dist(new_pt), r["pt"])
        delta = lambda a, b: {k: round(b[k] - a[k], 2) for k in a if abs(b[k] - a[k]) >= 0.03}
        out.append({
            "partner": names[r["pt"]], "partner_power_rank": power_rank[r["pt"]],
            "i_give": [{"name": p["name"], "espn_rank": _espn_rank(p["id"], pool_by_id),
                        "per_game": {CN[k]: round(p["rates"][k], 1) for k in CATS[:7]}} for p in r["give"]],
            "i_get": [{"name": p["name"], "espn_rank": _espn_rank(p["id"], pool_by_id),
                       "per_game": {CN[k]: round(p["rates"][k], 1) for k in CATS[:7]}} for p in r["get"]],
            "my_expected_wins_change": round(r["gm"], 2),
            "their_expected_wins_change": round(r["gp"], 2),
            "looks_fair": {"generic_value_ratio_get_vs_give": round(r["vr"], 2),
                           "avg_espn_rank_give": round(r["rg"]), "avg_espn_rank_get": round(r["rr"])},
            "partner_is_playoff_rival": r["rival"],
            "my_category_changes": delta(before_me, after_me),
            "their_category_changes": delta(before_pt, after_pt),
        })
        if len(out) >= max_results:
            break
    # flag trades not suggested in previous runs (so a daily brief only surfaces new ideas)
    seen_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_seen.json")
    try:
        with open(seen_file) as f:
            seen_before = set(json.load(f))
    except Exception:
        seen_before = set()
    for t in out:
        sig = f"{t['partner']}|{'+'.join(sorted(p['name'] for p in t['i_give']))}|{'+'.join(sorted(p['name'] for p in t['i_get']))}"
        t["new_since_last_run"] = sig not in seen_before
        seen_before.add(sig)
    try:
        with open(seen_file, "w") as f:
            json.dump(sorted(seen_before)[-500:], f)
    except Exception:
        pass
    return json.dumps({"baseline_my_expected_wins": round(base_me, 2), "trades": out,
                       "note": "Category changes = change in chance of winning that category vs an average opponent."},
                      indent=1)


# ---------------------------------------------------------------------------
# INCOMING / PENDING TRADES
# ---------------------------------------------------------------------------
@mcp.tool()
def get_pending_trades() -> str:
    """Trade offers involving my team that are still pending (offers I've received AND ones I've sent),
    each evaluated: my and their expected-win change for the rest of the season, category changes,
    and whether I proposed it or received it. Use it to recommend accept / counter / decline."""
    d = _get(["mPendingTransactions", "mTransactions2", "mRoster", "mTeam", "mSettings", "mStatus", "mMatchup"])
    names = {t["id"]: t.get("name") for t in d["teams"]}
    raw = (d.get("pendingTransactions") or []) + [t for t in (d.get("transactions") or [])
                                                  if t.get("status") in ("PENDING", "PROPOSED")]
    trades = [t for t in raw if "TRADE" in str(t.get("type", "")).upper()]
    if not trades:
        return json.dumps({"pending_trades": [], "note": "No pending trades found.",
                           "debug_keys": sorted(d.keys()),
                           "debug_pending_count": len(d.get("pendingTransactions") or [])})
    weeks = set(_remaining_weeks(d))
    rosters = {t["id"]: _team_players(t["id"], d)[1] for t in d["teams"]}
    dists = {tid: _weekly_dist(pl) for tid, pl in rosters.items()}
    fixtures = {tid: [] for tid in rosters}
    for m in d["schedule"]:
        if m["matchupPeriodId"] in weeks and "away" in m:
            h, a = m["home"]["teamId"], m["away"]["teamId"]
            fixtures[h].append(a)
            fixtures[a].append(h)

    def exp_wins(tid, dist, override=None):
        override = override or {}
        return sum(_match_win(_cat_probs(dist, override.get(o, dists[o]))) for o in fixtures[tid])

    def league_cats(dist, exclude):
        probs = [_cat_probs(dist, dists[o]) for o in dists if o != exclude]
        return {CN[k]: sum(p[k] for p in probs) / len(probs) for k in CATS}

    out, seen = [], set()
    for t in trades:
        if t.get("id") in seen:
            continue
        seen.add(t.get("id"))
        items = [i for i in t.get("items", []) if i.get("playerId")]
        teams = {i.get("fromTeamId") for i in items} | {i.get("toTeamId") for i in items}
        if TEAM_ID not in teams:
            continue
        other = next((x for x in teams if x and x != TEAM_ID), None)
        if other not in rosters:
            continue
        give_ids = {i["playerId"] for i in items if i.get("fromTeamId") == TEAM_ID}
        get_ids = {i["playerId"] for i in items if i.get("toTeamId") == TEAM_ID}
        give = [p for p in rosters[TEAM_ID] if p["id"] in give_ids]
        get = [p for p in rosters[other] if p["id"] in get_ids]
        new_me = [p for p in rosters[TEAM_ID] if p["id"] not in give_ids] + get
        new_ot = [p for p in rosters[other] if p["id"] not in get_ids] + give
        dm, do = _weekly_dist(new_me), _weekly_dist(new_ot)
        gm = (exp_wins(TEAM_ID, dm, {other: do}) - exp_wins(TEAM_ID, dists[TEAM_ID])) * GAIN_CALIBRATION
        go = (exp_wins(other, do, {TEAM_ID: dm}) - exp_wins(other, dists[other])) * GAIN_CALIBRATION
        bm, am = league_cats(dists[TEAM_ID], TEAM_ID), league_cats(dm, TEAM_ID)
        when = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms else None
        out.append({
            "with": names.get(other), "proposed_by": "me" if t.get("teamId") == TEAM_ID else names.get(t.get("teamId")),
            "status": t.get("status"), "type": t.get("type"),
            "proposed": when(t.get("proposedDate")), "expires": when(t.get("expirationDate")),
            "i_give": [p["name"] for p in give], "i_get": [p["name"] for p in get],
            "my_expected_wins_change": round(gm, 2), "their_expected_wins_change": round(go, 2),
            "my_category_changes": {k: round(am[k] - bm[k], 2) for k in bm if abs(am[k] - bm[k]) >= 0.03},
            "unmatched_players": len(give) != len(give_ids) or len(get) != len(get_ids),
        })
    return json.dumps({"pending_trades": out}, indent=1)


# ---------------------------------------------------------------------------
# SELF-REVIEW: prediction log + scoring against what actually happened
# ---------------------------------------------------------------------------
import sqlite3

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "predictions.db")


def _db():
    c = sqlite3.connect(DB_FILE)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS predictions (id INTEGER PRIMARY KEY, ts TEXT, tool TEXT, period INTEGER, data TEXT);
    CREATE TABLE IF NOT EXISTS player_rates (period INTEGER, player_id INTEGER, name TEXT, ts TEXT, rates TEXT,
        PRIMARY KEY (period, player_id));
    CREATE TABLE IF NOT EXISTS reviews (period INTEGER PRIMARY KEY, ts TEXT, data TEXT);
    """)
    return c


def _safe_log(tool, period, data, players=None):
    """Never let logging break a tool."""
    try:
        c = _db()
        now = datetime.now(timezone.utc).isoformat()
        c.execute("INSERT INTO predictions (ts, tool, period, data) VALUES (?,?,?,?)",
                  (now, tool, period, json.dumps(data, default=str)))
        for group in (players or {}).values():
            for p in group:
                if p.get("rates"):
                    c.execute("INSERT OR IGNORE INTO player_rates VALUES (?,?,?,?,?)",
                              (period, p["id"], p["name"], now, json.dumps(p["rates"])))
        c.commit()
        c.close()
    except Exception:
        pass


def _final_scores(d, period, tid):
    for m in d["schedule"]:
        if m["matchupPeriodId"] == period and tid in (m["home"]["teamId"], m.get("away", {}).get("teamId")):
            me, opp = (m["home"], m["away"]) if m["home"]["teamId"] == tid else (m["away"], m["home"])
            if m.get("winner") in (None, "UNDECIDED"):
                return None
            sc = lambda side: {CN[k]: float(((side.get("cumulativeScore") or {}).get("scoreByStat") or {})
                                            .get(k, {}).get("score", 0) or 0) for k in CATS}
            won = (m["winner"] == "HOME") == (m["home"]["teamId"] == tid)
            return {"opp_id": opp["teamId"], "me": sc(me), "opp": sc(opp),
                    "result": "TIE" if m["winner"] == "TIE" else ("W" if won else "L")}
    return None


@mcp.tool()
def review_week(matchup_period: int = 0) -> str:
    """Scores the model against reality for a finished week: compares the earliest and latest
    simulate_week forecasts and the season_outlook win chance with the actual result, category by
    category, and checks recommended pickups against how those players have actually played.
    Saves the review so model_report can track accuracy over time. Default = last finished week."""
    d = _get(["mMatchup", "mMatchupScore", "mStatus", "mTeam"])
    cur = d["status"].get("currentMatchupPeriod")
    period = matchup_period or (cur - 1)
    actual = _final_scores(d, period, TEAM_ID)
    if not actual:
        return json.dumps({"period": period, "note": "That week isn't finished (or wasn't found) yet."})
    c = _db()
    rows = c.execute("SELECT ts, tool, data FROM predictions WHERE period=? ORDER BY ts", (period,)).fetchall()
    sims = [json.loads(r[2]) for r in rows if r[1] == "simulate_week"]
    won = 1.0 if actual["result"] == "W" else 0.5 if actual["result"] == "TIE" else 0.0
    out = {"period": period, "result": actual["result"], "actual": actual, "forecasts_logged": len(sims)}

    def score_sim(s):
        cats = {}
        for cat, v in s["categories"].items():
            a_me, a_opp = actual["me"][cat], actual["opp"][cat]
            cat_won = (a_me < a_opp) if cat == "TO" else (a_me > a_opp)
            cats[cat] = {"proj_me": v["proj_me"], "actual_me": a_me, "err_me": round(a_me - v["proj_me"], 3),
                         "proj_opp": v["proj_opp"], "actual_opp": a_opp, "err_opp": round(a_opp - v["proj_opp"], 3),
                         "cat_win_prob": v["win_prob"], "cat_won": cat_won,
                         "brier": round((v["win_prob"] - cat_won) ** 2, 3)}
        return {"win_prob": s["win_prob"], "brier": round((s["win_prob"] - won) ** 2, 3),
                "days_left": len(s.get("days_left_US", [])), "categories": cats}

    if sims:
        out["earliest_forecast"] = score_sim(sims[0])
        if len(sims) > 1:
            out["latest_forecast"] = score_sim(sims[-1])
    outlooks = [json.loads(r[2]) for r in rows if r[1] == "season_outlook"]
    pre = c.execute("SELECT data FROM predictions WHERE tool='season_outlook' AND period<? ORDER BY ts DESC LIMIT 1",
                    (period,)).fetchone()
    for o in ([json.loads(pre[0])] if pre else []) + outlooks:
        f = next((x for x in o["fixture"] if x["week"] == period), None)
        if f:
            out["preseason_style_win_prob"] = {"win_prob": f["win_prob"], "brier": round((f["win_prob"] - won) ** 2, 3)}
            break

    # pickups recommended in that period: predicted per-game vs what they've done since (last 7 days)
    picks = [json.loads(r[2]) for r in rows if r[1] == "rank_pickups"]
    if picks:
        pool = {x["player"].get("fullName"): x["player"] for x in _pool()}
        checks = []
        for pk in picks[-1]["pickups"][:5]:
            p = pool.get(pk["name"])
            a, gp = _avg(p, f"01{SEASON}") if p else (None, 0)
            if a:
                act = {CN[k]: round(a.get(k, 0), 1) for k in CATS[:7]}
                checks.append({"name": pk["name"], "predicted_per_game": pk["per_game"], "last7_per_game": act,
                               "last7_gp": gp, "value_pred": round(_value({k: pk["per_game"].get(CN[k], 0) for k in CATS[:7]}), 1),
                               "value_actual": round(_value({k: act[CN[k]] for k in CATS[:7]}), 1)})
        out["pickup_check"] = checks

    c.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?)", (period, datetime.now(timezone.utc).isoformat(), json.dumps(out)))
    c.commit()
    c.close()
    return json.dumps(out, indent=1)


@mcp.tool()
def model_report() -> str:
    """Accuracy across every reviewed week: overall and per-category Brier scores (lower = better,
    0.25 = coin flip), calibration buckets, and systematic bias (does the model keep over- or
    under-projecting a category?). Use this to decide what to tune: blend weight (gp/(gp+15)),
    VAR_SCALE, PLAY_PROB, REPLACEMENT, GAIN_CALIBRATION or the _value weights."""
    c = _db()
    revs = [json.loads(r[0]) for r in c.execute("SELECT data FROM reviews ORDER BY period")]
    c.close()
    if not revs:
        return json.dumps({"note": "No reviewed weeks yet. Run review_week after a week finishes."})
    rep = {"weeks_reviewed": [r["period"] for r in revs],
           "record_in_reviewed_weeks": "".join(r["result"] for r in revs)}
    for key in ("earliest_forecast", "latest_forecast"):
        fs = [r[key] for r in revs if key in r]
        if not fs:
            continue
        cats = {}
        for cat in fs[0]["categories"]:
            xs = [f["categories"][cat] for f in fs]
            cats[cat] = {"brier": round(sum(x["brier"] for x in xs) / len(xs), 3),
                         "bias_me": round(sum(x["err_me"] for x in xs) / len(xs), 3),
                         "bias_opp": round(sum(x["err_opp"] for x in xs) / len(xs), 3)}
        buckets = {}
        for f in fs:
            for x in f["categories"].values():
                b = f"{int(min(x['cat_win_prob'], 0.99) * 5) * 20}-{int(min(x['cat_win_prob'], 0.99) * 5) * 20 + 20}%"
                buckets.setdefault(b, []).append(x["cat_won"])
        rep[key] = {"n": len(fs), "match_brier": round(sum(f["brier"] for f in fs) / len(fs), 3),
                    "categories": cats,
                    "calibration": {b: {"n": len(v), "actual_win_rate": round(sum(v) / len(v), 2)}
                                    for b, v in sorted(buckets.items())}}
    pw = [r["preseason_style_win_prob"]["brier"] for r in revs if "preseason_style_win_prob" in r]
    if pw:
        rep["season_outlook_brier"] = round(sum(pw) / len(pw), 3)
    pc = [x for r in revs for x in r.get("pickup_check", [])]
    if pc:
        rep["pickups"] = {"n": len(pc), "avg_value_predicted": round(sum(x["value_pred"] for x in pc) / len(pc), 1),
                          "avg_value_actual_last7": round(sum(x["value_actual"] for x in pc) / len(pc), 1)}
    rep["warning"] = "Under ~6 weeks of data, treat biases as noise unless they're large and consistent."
    return json.dumps(rep, indent=1)


# ---------------------------------------------------------------------------
# LEAGUE POSTS v2: bookmaker odds, futures, awards (daily_post_data / weekly_post_data)
# ---------------------------------------------------------------------------
POSTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "league_posts_history.json")
H2H_MARGIN = 0.10      # head-to-head matchup markets (~110% book)
FUTURES_MARGIN = 0.30  # premiership and make-the-finals markets (~130% book)
SEASON_SIMS = 20000
MEL = ZoneInfo("Australia/Melbourne")


def _posts_load():
    try:
        with open(POSTS_FILE) as f:
            h = json.load(f)
    except Exception:
        h = {}
    for k in ("daily", "weekly", "opening", "tx_seen"):
        h.setdefault(k, {} if k != "tx_seen" else [])
    return h


def _posts_save(h):
    try:
        h["tx_seen"] = h.get("tx_seen", [])[-500:]
        if "tx_seen_base" in h:
            h["tx_seen_base"] = h["tx_seen_base"][-500:]
        with open(POSTS_FILE, "w") as f:
            json.dump(h, f)
    except Exception:
        pass


def _fs(st):
    """Fantasy score of a stat line (same formula as league_update_data)."""
    return (st.get("0", 0) + 1.2 * st.get("6", 0) + 1.5 * st.get("3", 0) + 3 * st.get("2", 0)
            + 3 * st.get("1", 0) + st.get("17", 0) - 1.5 * st.get("11", 0))


def _fmt_line(st, games=None):
    g = f"{games} g: " if games is not None else ""
    fg = f"{int(st.get('13', 0))}/{int(st.get('14', 0))} FG"
    return (g + f"{int(st.get('0', 0))} pts, {int(st.get('6', 0))} reb, {int(st.get('3', 0))} ast, "
            f"{int(st.get('2', 0))} stl, {int(st.get('1', 0))} blk, {int(st.get('17', 0))} 3pm, "
            f"{int(st.get('11', 0))} to, {fg}, {int(st.get('15', 0))}/{int(st.get('16', 0))} FT")


def _tick(o):
    """Round a decimal price to the steps Australian bookmakers use."""
    for lim, st in ((2, .01), (3, .05), (5, .1), (10, .25), (20, .5), (50, 1), (101, 5), (1e9, 25)):
        if o < lim:
            return max(1.01, round(round(o / st) * st, 2))


def _book(probs, margin, places=1, cap=1001.0):
    """Bookmaker decimal prices for mutually exclusive outcomes (places = how many 'win', e.g. 6 for
    make-the-top-6). Implied probabilities are p**k with k<1 chosen so they sum to places*(1+margin):
    most of the margin lands on outsiders (favourite-longshot bias), like Australian bookmakers."""
    ps = [min(max(p, 0.001), 0.999) for p in probs]
    target = places * (1 + margin)
    total = lambda k: sum(min(p ** k, 1 / 1.01) for p in ps)
    if total(1.0) >= target:
        k = 1.0
    else:
        lo, hi = 0.02, 1.0
        for _ in range(60):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if total(mid) > target else (lo, mid)
        k = (lo + hi) / 2
    return [min(cap, _tick(1 / min(p ** k, 1 / 1.01))) for p in ps]


def _h2h_market(p_home, p_tie):
    """Prices for one matchup. A draw price is only offered when the model gives a tie >= 2%."""
    if p_tie >= 0.02:
        ph, pd, pa = _book([p_home, p_tie, max(0.0, 1 - p_home - p_tie)], H2H_MARGIN)
        return {"home": ph, "away": pa, "draw": pd}
    s = max(1e-9, 1 - p_tie)
    ph, pa = _book([p_home / s, 1 - p_home / s], H2H_MARGIN)
    return {"home": ph, "away": pa, "draw": None}


def _side_score(side, live=True):
    src = side.get("cumulativeScoreLive") if live and (side.get("cumulativeScoreLive") or {}).get("scoreByStat") \
        else side.get("cumulativeScore") or {}
    sbs = src.get("scoreByStat") or {}
    return src, {k: float((sbs.get(k) or {}).get("score", 0) or 0) for k in CATS}


def _cats_won(a, b):
    wa = [CN[k] for k in CATS if _beats(k, a[k], b[k])]
    wb = [CN[k] for k in CATS if _beats(k, b[k], a[k])]
    return wa, wb


def _closeness(k, a, b):
    """0 = dead level. Counting cats: relative gap. FG%/FT%: 1 percentage point ~ 5% gap."""
    if k in ("19", "20"):
        return abs(a - b) * 5
    return abs(a - b) / max(1.0, (a + b) / 2)


def _fmt_cat(k, v):
    return f"{v:.3f}".lstrip("0") if k in ("19", "20") else f"{int(round(v))}"


def _live_odds(d, n=1000):
    """{(home_id, away_id): (p_home, p_tie)} for every matchup in the current period, using the same
    day-by-day simulator (and seed) as simulate_week(home)."""
    mp = d["status"].get("currentMatchupPeriod")
    sched = _schedule()
    now = datetime.now(ET)
    days = _week_days(0)
    out = {}
    for m in d["schedule"]:
        if m["matchupPeriodId"] != mp or "away" not in m:
            continue
        h, a = m["home"]["teamId"], m["away"]["teamId"]
        _, _, bh, ba = _current_matchup(d, h)
        _, ph = _team_players(h, d)
        _, pa = _team_players(a, d)
        r = _matchup_odds(ph, pa, days, sched, bh, ba, n=n, now=now)
        out[(h, a)] = (r["win_prob"], r["tie_prob"])
    return out


def _pairwise(d):
    """P[(a, b)] = chance a beats b in a typical full week (season model), made symmetric."""
    dists = {t["id"]: _weekly_dist(_team_players(t["id"], d)[1]) for t in d["teams"]}
    raw = {(a, b): _match_win(_cat_probs(dists[a], dists[b])) for a in dists for b in dists if a != b}
    P = {(a, b): (raw[(a, b)] + 1 - raw[(b, a)]) / 2 for (a, b) in raw}
    power = {a: sum(P[(a, b)] for b in dists if b != a) / (len(dists) - 1) for a in dists}
    return P, power


def _bracket_order(size):
    order = [1]
    while len(order) < size:
        m = len(order) * 2 + 1
        order = [x for s in order for x in (s, m - s)]
    return order


def _season_sim(d, P, live=None, n=SEASON_SIMS, seed=11):
    """Monte Carlo of the rest of the regular season (real fixture) and the finals bracket.
    Current-week games use live/opening sim odds when given, other weeks the pairwise model.
    Returns {team: {"title": p, "finals": p, "top2": p, "exp_wins": w}}."""
    ss = d["settings"]["scheduleSettings"]
    reg = ss["matchupPeriodCount"]
    po_n = ss.get("playoffTeamCount") or 6
    reseed = bool(ss.get("playoffReseed"))
    teams = [t["id"] for t in d["teams"]]
    live = live or {}
    base = {}
    for t in d["teams"]:
        r = t["record"]["overall"]
        base[t["id"]] = r["wins"] + 0.5 * r["ties"]
    remaining, po_results = [], {}
    for m in d["schedule"]:
        if "away" not in m:
            continue
        h, a, pid = m["home"]["teamId"], m["away"]["teamId"], m["matchupPeriodId"]
        if pid <= reg:
            if m.get("winner") in (None, "UNDECIDED"):
                ph, pt = live.get((h, a), (P[(h, a)], 0.0))
                remaining.append((h, a, ph, pt))
        elif m.get("playoffTierType") == "WINNERS_BRACKET" and m.get("winner") in ("HOME", "AWAY"):
            po_results[frozenset((h, a))] = h if m["winner"] == "HOME" else a
    cur = d["status"].get("currentMatchupPeriod") or 1
    in_playoffs = cur > reg or not remaining
    fixed_seeds = None
    if in_playoffs:
        st = sorted(d["teams"], key=lambda t: t.get("playoffSeed", 99))
        fixed_seeds = [t["id"] for t in st[:po_n]]
    rng = random.Random(seed)
    size = 1
    while size < po_n:
        size *= 2
    order = _bracket_order(size)
    live_pairs = {frozenset(k): (k[0], v[0], v[1]) for k, v in live.items()}

    def play(a, b, rnd):
        key = frozenset((a, b))
        if key in po_results:
            return po_results[key]
        if cur == reg + rnd and key in live_pairs:
            h, ph, pt = live_pairs[key]
            ph = ph / max(1e-9, 1 - pt)  # playoff ties are broken; split the draw chance pro rata
            return h if rng.random() < ph else (b if h == a else a)
        return a if rng.random() < P[(a, b)] else b

    out = {t: {"title": 0, "finals": 0, "top2": 0, "exp_wins": 0.0} for t in teams}
    for _ in range(n):
        w = dict(base)
        for h, a, ph, pt in remaining:
            r = rng.random()
            if r < ph:
                w[h] += 1
            elif r < ph + pt:
                w[h] += 0.5
                w[a] += 0.5
            else:
                w[a] += 1
        for t in teams:
            out[t]["exp_wins"] += w[t] / n
        seeds = fixed_seeds or sorted(teams, key=lambda t: (-w[t], rng.random()))[:po_n]
        for i, t in enumerate(seeds):
            out[t]["finals"] += 1
            if i < 2:
                out[t]["top2"] += 1
        alive = [seeds[s - 1] if s <= po_n else None for s in order]
        rnd = 1
        while len(alive) > 1:
            if reseed and rnd > 1:
                alive = sorted([x for x in alive if x], key=lambda t: seeds.index(t))
                alive = [x for i in range(len(alive) // 2) for x in (alive[i], alive[-1 - i])] or alive
            nxt = []
            for i in range(0, len(alive), 2):
                a, b = alive[i], alive[i + 1] if i + 1 < len(alive) else None
                nxt.append(a if b is None else b if a is None else play(a, b, rnd))
            alive = nxt
            rnd += 1
        if alive and alive[0]:
            out[alive[0]]["title"] += 1
    for t in teams:
        for k in ("title", "finals", "top2"):
            out[t][k] /= n
    return out, po_n, in_playoffs


def _period_sps(d, mp):
    for m in d["schedule"]:
        if m["matchupPeriodId"] == mp and m.get("home", {}).get("pointsByScoringPeriod"):
            return sorted(int(k) for k in m["home"]["pointsByScoringPeriod"])
    return []


def _transactions(sps):
    seen, out = set(), []
    for sp in sps:
        try:
            tx = _get(["mTransactions2"], params={"scoringPeriodId": sp}).get("transactions", [])
        except Exception:
            continue
        for t in tx:
            if t.get("id") in seen or t.get("status") != "EXECUTED" or t.get("type") not in ("WAIVER", "FREEAGENT", "TRADE_ACCEPT"):
                continue
            seen.add(t.get("id"))
            out.append(t)
    return out


def _key_players(team_entries, top=8):
    ps = [e["playerPoolEntry"]["player"] for e in team_entries]
    ps.sort(key=lambda p: -_value(_rates(p)))
    return ps[:top]


@mcp.tool()
def daily_post_data() -> str:
    """Everything for the DAILY league post, pre-computed: every live matchup with score, win chance
    and bookmaker odds (10% margin), the closest matchups, odds movers and momentum swings since the
    last daily post, knife-edge categories, performance / worst performance / ice-cold shooter of the
    day, bench blunders, injury watch (key players newly out), the biggest FAAB splash and recent
    transactions. Saves a snapshot so tomorrow's movers and the weekly comeback/choke awards work."""
    upd = json.loads(league_update_data())
    sp, mp = upd["scoring_period"], upd["matchup_period"]
    d = _get(["mRoster", "mTeam", "mMatchup", "mMatchupScore", "mStatus", "mSettings"])
    names = {t["id"]: t.get("name") for t in d["teams"]}
    live = _live_odds(d)
    hist = _posts_load()
    today = datetime.now(MEL).strftime("%Y-%m-%d")
    prev_key = max((k for k, v in hist["daily"].items() if k < today and v.get("mp") == mp), default=None)
    prev = hist["daily"].get(prev_key) if prev_key else None

    matchups, snap_odds, snap_scores, knife = [], {}, {}, []
    for m in d["schedule"]:
        if m["matchupPeriodId"] != mp or "away" not in m:
            continue
        h, a = m["home"]["teamId"], m["away"]["teamId"]
        key = f"{h}-{a}"
        (hs, hc), (as_, ac) = _side_score(m["home"]), _side_score(m["away"])
        hw, aw = _cats_won(hc, ac)
        ties = 9 - len(hw) - len(aw)
        ph, pt = live.get((h, a), (None, 0.0))
        row = {"home": names[h], "away": names[a], "home_id": h, "away_id": a,
               "score_home_away_ties": [len(hw), len(aw), ties], "margin": abs(len(hw) - len(aw)),
               "leader": names[h] if len(hw) > len(aw) else names[a] if len(aw) > len(hw) else None}
        if ph is not None:
            mk = _h2h_market(ph, pt)
            pa = max(0.0, 1 - ph - pt)
            row.update({"win_pct_home": round(ph * 100), "win_pct_away": round(pa * 100), "tie_pct": round(pt * 100),
                        "favourite": names[h] if ph >= pa else names[a],
                        "favourite_win_pct": round(max(ph, pa) * 100),
                        "price_home": mk["home"], "price_away": mk["away"], "price_draw": mk["draw"]})
            snap_odds[key] = {"ph": ph, "pt": pt, "price_home": mk["home"], "price_away": mk["away"]}
        snap_scores[key] = [len(hw), len(aw), ties]
        for k in CATS:
            if hc[k] or ac[k]:
                knife.append({"matchup": f"{names[h]} v {names[a]}", "category": CN[k],
                              "home": _fmt_cat(k, hc[k]), "away": _fmt_cat(k, ac[k]),
                              "_c": _closeness(k, hc[k], ac[k]) + (0 if k in ("19", "20") else 1 / max(4, hc[k] + ac[k]))})
        matchups.append(row)
    matchups.sort(key=lambda r: -r["margin"])
    knife.sort(key=lambda x: x["_c"])
    for x in knife:
        x.pop("_c")
    closest = sorted([r for r in matchups if "favourite_win_pct" in r], key=lambda r: r["favourite_win_pct"])[:2]

    movers, swings = [], []
    prev_odds = (prev or {}).get("odds") or hist["opening"].get(str(mp), {})  # Tuesday: vs Monday's opening odds
    if prev or prev_odds:
        for r in matchups:
            key = f"{r['home_id']}-{r['away_id']}"
            po, ps = prev_odds.get(key), (prev or {}).get("scores", {}).get(key)
            if po and "price_home" in r:
                for side, team in (("home", r["home"]), ("away", r["away"])):
                    old, new = po[f"price_{side}"], r[f"price_{side}"]
                    if old != new:
                        movers.append({"team": team, "from": old, "to": new,
                                       "direction": "firmed" if new < old else "drifted",
                                       "_size": abs(1 / new - 1 / old)})
            if ps:
                old_lead = (ps[0] > ps[1]) - (ps[0] < ps[1])
                new_lead = (r["score_home_away_ties"][0] > r["score_home_away_ties"][1]) - \
                           (r["score_home_away_ties"][0] < r["score_home_away_ties"][1])
                if old_lead != new_lead:
                    swings.append({"matchup": f"{r['home']} v {r['away']}",
                                   "was": "-".join(map(str, ps)), "now": "-".join(map(str, r["score_home_away_ties"])),
                                   "now_leading": r["leader"] or "level"})
        movers.sort(key=lambda x: -x["_size"])
        for x in movers:
            x.pop("_size")

    # day lines: ice-cold shooter, injury watch (key players newly out / doubtful)
    dd = _get(["mRoster", "mTeam"], params={"scoringPeriodId": sp})
    cold, injuries, statuses = [], [], {}
    prev_status = (prev or {}).get("injuries", {})
    for t in dd["teams"]:
        for e in t["roster"]["entries"]:
            p = e["playerPoolEntry"]["player"]
            st = _day_line(p, sp)
            if st and e["lineupSlotId"] not in (12, 13) and st.get("14", 0) >= 10:
                cold.append({"player": p.get("fullName"), "team": t.get("name"),
                             "fg": f"{int(st.get('13', 0))}/{int(st.get('14', 0))}",
                             "fg_pct": round(st.get("13", 0) / st["14"], 3), "line": _fmt_line(st)})
        for p in _key_players(t["roster"]["entries"]):
            s = p.get("injuryStatus") or "ACTIVE"
            statuses[str(p["id"])] = s
            was = prev_status.get(str(p["id"]))
            if prev and s in ("OUT", "INJURY_RESERVE", "SUSPENSION", "DAY_TO_DAY") and was != s:
                injuries.append({"player": p.get("fullName"), "team": t.get("name"),
                                 "status": s, "was": was or "ACTIVE"})
    cold.sort(key=lambda x: x["fg_pct"])

    # FAAB: biggest winning bid processed since the last post
    pool_names = {x["player"]["id"]: x["player"].get("fullName") for x in _pool()}
    # Same-day re-runs start from the seen-set as it was before today's first run, so the
    # latest run of the day reports the same FAAB splashes (only one snapshot per day persists).
    if hist.get("tx_seen_day") != today:
        hist["tx_seen_base"] = list(hist.get("tx_seen", []))
        hist["tx_seen_day"] = today
    seen = set(hist.get("tx_seen_base", []))
    splashes = []
    tsp = (d["status"].get("transactionScoringPeriod") or sp)
    for t in _transactions(sorted({sp - 1, sp, tsp})):
        if t.get("id") in seen:
            continue
        seen.add(t.get("id"))
        if t.get("type") == "WAIVER" and (t.get("bidAmount") or 0) > 0:
            add = next((i for i in t.get("items", []) if i.get("type") == "ADD"), None)
            if add:
                splashes.append({"team": names.get(t.get("teamId")), "player": pool_names.get(add["playerId"], add["playerId"]),
                                 "bid": t["bidAmount"]})
    splashes.sort(key=lambda x: -x["bid"])
    first_run = prev is None
    hist["tx_seen"] = list(seen)
    hist["daily"][today] = {"mp": mp, "sp": sp, "saved": datetime.now(timezone.utc).isoformat(),
                            "odds": snap_odds, "scores": snap_scores, "injuries": statuses}
    hist["daily"] = dict(sorted(hist["daily"].items())[-60:])
    _posts_save(hist)

    since = (prev or {}).get("saved")
    tx = upd["recent_transactions"]
    if since:
        tx = [x for x in tx if x["date"] > since]
    return json.dumps({
        "date_melbourne": datetime.now(MEL).strftime("%A %-d %B %Y"),
        "matchup_period": mp, "scoring_period": sp,
        "matchups_most_lopsided_first": matchups,
        "closest_by_win_chance": closest,
        "odds_movers_since_last_post": movers[:3],
        "momentum_swings_since_last_post": swings,
        "knife_edge_categories": knife[:3],
        "performance_of_the_day": upd["top_performances"][:1],
        "worst_performance": upd["worst_performances"][:1],
        "ice_cold_shooter": cold[:1],
        "bench_blunders": upd["bench_blunders"],
        "injury_watch": injuries,
        "faab_splash": splashes[:1],
        "transactions_since_last_post": tx,
        "notes": {"first_snapshot": first_run,
                  "odds": "decimal, ~10% margin; price_draw only when the model gives a tie >= 2%",
                  "previous_post_snapshot": prev_key}}, indent=1)


def _finished_period(d):
    reg_and_po = sorted({m["matchupPeriodId"] for m in d["schedule"]})
    done = [p for p in reg_and_po
            if all(m.get("winner") not in (None, "UNDECIDED") for m in d["schedule"]
                   if m["matchupPeriodId"] == p and "away" in m)]
    return max(done) if done else None


@mcp.tool()
def weekly_post_data(matchup_period: int = 0) -> str:
    """Everything for the WEEKLY roundup post, pre-computed: final results of the finished week
    (default = latest finished), standings with the finals line, power rankings with movement,
    premiership odds and make-the-finals odds (season + finals simulated on the real fixture, 30%
    margin) with movers, next week's matchups with opening odds (10% margin), and awards: upset of
    the week, biggest comeback, biggest choke, bad beat, team of the week, MVP of the week, bench
    points of the week, best/worst acquisition, worst drop, streaks, luck ladder, toughest run home."""
    d = _get(["mMatchup", "mMatchupScore", "mTeam", "mStatus", "mStandings", "mRoster", "mSettings"])
    names = {t["id"]: t.get("name") for t in d["teams"]}
    ss = d["settings"]["scheduleSettings"]
    reg, po_n = ss["matchupPeriodCount"], ss.get("playoffTeamCount") or 6
    F = matchup_period or _finished_period(d)
    if not F:
        return json.dumps({"error": "No finished week yet."})
    cur = d["status"].get("currentMatchupPeriod")
    hist = _posts_load()
    P, power = _pairwise(d)

    # --- results
    results, bad_beats = [], []
    for m in d["schedule"]:
        if m["matchupPeriodId"] != F or "away" not in m:
            continue
        h, a = m["home"]["teamId"], m["away"]["teamId"]
        decided = m.get("winner") not in (None, "UNDECIDED")
        (_, hc), (_, ac) = _side_score(m["home"], live=not decided), _side_score(m["away"], live=not decided)
        hw, aw = _cats_won(hc, ac)
        win = h if len(hw) > len(aw) else a if len(aw) > len(hw) else None
        results.append({"winner": names.get(win), "loser": names.get(a if win == h else h) if win else None,
                        "home": names[h], "away": names[a], "home_id": h, "away_id": a, "winner_id": win,
                        "score": f"{max(len(hw), len(aw))}-{min(len(hw), len(aw))}-{9 - len(hw) - len(aw)}",
                        "margin": abs(len(hw) - len(aw)),
                        "winner_categories": hw if win == h else aw if win else [],
                        "categories_home_vs_away": {CN[k]: [_fmt_cat(k, hc[k]), _fmt_cat(k, ac[k])] for k in CATS}})
        for k in CATS:
            for (x, xv, yv, y) in ((h, hc[k], ac[k], a), (a, ac[k], hc[k], h)):
                if _beats(k, yv, xv):
                    bad_beats.append({"team": names[x], "lost_to": names[y], "category": CN[k],
                                      "score": f"{_fmt_cat(k, xv)} v {_fmt_cat(k, yv)}", "_c": _closeness(k, xv, yv)})
    results.sort(key=lambda r: -r["margin"])
    bad_beats.sort(key=lambda x: x["_c"])
    for x in bad_beats:
        x.pop("_c")

    # --- opening odds for F (stored last Monday), else earliest daily snapshot, else model now
    opening_F, opening_src = {}, None
    if str(F) in hist["opening"]:
        opening_F, opening_src = hist["opening"][str(F)], "stored opening odds"
    else:
        days_F = sorted(k for k, v in hist["daily"].items() if v.get("mp") == F)
        if days_F:
            opening_F, opening_src = hist["daily"][days_F[0]]["odds"], f"first daily post ({days_F[0]})"
    if not opening_F:
        opening_src = "season model now (no stored opening odds)"
        for r in results:
            ph = P[(r["home_id"], r["away_id"])]
            mk = _h2h_market(ph, 0.0)
            opening_F[f"{r['home_id']}-{r['away_id']}"] = {"ph": ph, "pt": 0.0, "price_home": mk["home"], "price_away": mk["away"]}

    upsets, comebacks, chokes = [], [], []
    for r in results:
        key = f"{r['home_id']}-{r['away_id']}"
        o = opening_F.get(key)
        if not r["winner_id"]:
            continue
        won_home = r["winner_id"] == r["home_id"]
        if o:
            price = o["price_home"] if won_home else o["price_away"]
            if price > 2.0:
                upsets.append({"winner": r["winner"], "beat": r["loser"], "opening_price": price, "score": r["score"]})
        track = []  # (label, winner's win prob)
        if o:
            track.append(("opening", o["ph"] if won_home else 1 - o["ph"] - o.get("pt", 0)))
        for day, v in sorted(hist["daily"].items()):
            if v.get("mp") == F and key in v.get("odds", {}):
                x = v["odds"][key]
                track.append((day, x["ph"] if won_home else 1 - x["ph"] - x.get("pt", 0)))
        if track:
            lo = min(track, key=lambda x: x[1])
            hi_l = max(track, key=lambda x: 1 - x[1])
            if lo[1] < 0.5:  # only a comeback if the winner was the underdog at some point
                comebacks.append({"winner": r["winner"], "beat": r["loser"], "lowest_win_pct": round(lo[1] * 100), "when": lo[0]})
            if 1 - hi_l[1] > 0.5:  # only a choke if the loser was favourite at some point
                chokes.append({"loser": r["loser"], "lost_to": r["winner"], "highest_win_pct": round((1 - hi_l[1]) * 100), "when": hi_l[0]})
    upsets.sort(key=lambda x: -x["opening_price"])
    comebacks.sort(key=lambda x: x["lowest_win_pct"])
    chokes.sort(key=lambda x: -x["highest_win_pct"])

    # --- day-by-day rosters for week F: MVP, team of the week tiebreak, bench points, acquisitions
    sps = _period_sps(d, F)
    started, benched, on_team = {}, {}, {}
    for sp in sps:
        try:
            dd = _get(["mRoster", "mTeam"], params={"scoringPeriodId": sp})
        except Exception:
            continue
        for t in dd["teams"]:
            for e in t["roster"]["entries"]:
                p = e["playerPoolEntry"]["player"]
                st = _day_line(p, sp)
                if not st:
                    continue
                key = (t["id"], p["id"])
                on_team.setdefault(key, {"name": p.get("fullName"), "games": 0, "score": 0.0, "stats": {}})
                on_team[key]["games"] += 1
                on_team[key]["score"] += _fs(st)
                for k2, v2 in st.items():
                    on_team[key]["stats"][k2] = on_team[key]["stats"].get(k2, 0) + v2
                bucket = benched if e["lineupSlotId"] in (12, 13) else started
                b = bucket.setdefault(key, {"name": p.get("fullName"), "games": 0, "score": 0.0, "stats": {}, "best": None})
                b["games"] += 1
                b["score"] += _fs(st)
                for k2, v2 in st.items():
                    b["stats"][k2] = b["stats"].get(k2, 0) + v2
                if not b["best"] or _fs(st) > b["best"][0]:
                    b["best"] = (_fs(st), _fmt_line(st))
    mvp = sorted(started.items(), key=lambda kv: -kv[1]["score"])[:3]
    mvp = [{"player": v["name"], "team": names[k[0]], "line": _fmt_line(v["stats"], v["games"]), "score": round(v["score"], 1)}
           for k, v in mvp]
    bench_team = {}
    for (tid, pid), v in benched.items():
        bench_team[tid] = bench_team.get(tid, 0) + v["score"]
    bench_big = sorted(benched.items(), key=lambda kv: -(kv[1]["best"][0] if kv[1]["best"] else 0))[:1]
    bench_week = {"most_bench_production": [{"team": names[t], "bench_score": round(s, 1)}
                                            for t, s in sorted(bench_team.items(), key=lambda x: -x[1])[:3]],
                  "biggest_single_bench_game": [{"player": v["name"], "team": names[k[0]], "line": v["best"][1],
                                                 "score": round(v["best"][0], 1)} for k, v in bench_big if v["best"]]}
    starter_total = {}
    for (tid, _), v in started.items():
        starter_total[tid] = starter_total.get(tid, 0) + v["score"]
    team_week = None
    best_cats = -1
    for r in results:
        for tid, cats in ((r["home_id"], r["winner_categories"] if r["winner_id"] == r["home_id"] else None),
                          (r["away_id"], r["winner_categories"] if r["winner_id"] == r["away_id"] else None)):
            if cats is None:
                continue
            cand = (len(cats), starter_total.get(tid, 0))
            if team_week is None or cand > best_cats:
                best_cats, team_week = cand, {"team": names[tid], "categories_won": len(cats), "score": r["score"],
                                              "categories": cats}

    # --- acquisitions and drops (this week and last)
    tx_sps = sorted(set(sps) | set(_period_sps(d, F - 1)) | ({d["status"].get("transactionScoringPeriod")} - {None}))
    pool = {x["player"]["id"]: x for x in _pool()}
    adds, drops = [], []
    for t in _transactions(tx_sps):
        if t.get("type") == "TRADE_ACCEPT":
            continue
        for it in t.get("items", []):
            if it.get("type") == "ADD":
                adds.append((it.get("toTeamId") or t.get("teamId"), it["playerId"], t.get("bidAmount") or 0))
            elif it.get("type") == "DROP":
                drops.append((it.get("fromTeamId") or t.get("teamId"), it["playerId"]))
    acq = []
    for tid, pid, bid in adds:
        v = on_team.get((tid, pid))
        if v and v["games"]:
            acq.append({"player": v["name"], "team": names.get(tid), "bid": bid, "games": v["games"],
                        "line": _fmt_line(v["stats"], v["games"]), "score": round(v["score"], 1),
                        "per_game": round(v["score"] / v["games"], 1)})
    best_acq = sorted(acq, key=lambda x: -x["score"])[:1]
    worst_acq = sorted([x for x in acq if x["games"] >= 2] or acq, key=lambda x: x["per_game"])[:1]
    worst_drop = []
    for tid, pid in drops:
        x = pool.get(pid)
        if not x:
            continue
        a7, gp = _avg(x["player"], f"01{SEASON}")
        if not a7 or not gp:
            continue
        now_team = names.get(x.get("onTeamId")) or "free agent"
        worst_drop.append({"player": x["player"].get("fullName"), "dropped_by": names.get(tid), "now": now_team,
                           "last7_per_game": {CN.get(k, k): round(v, 1) for k, v in a7.items() if k in CN and k not in ("19", "20")},
                           "games_last7": gp, "score_last7": round(_fs(a7) * gp, 1)})
    worst_drop.sort(key=lambda x: -x["score_last7"])

    # --- streaks, luck, toughest run home
    seq = {t: [] for t in names}
    for p in range(1, min(F, reg) + 1):
        for m in d["schedule"]:
            if m["matchupPeriodId"] != p or "away" not in m or m.get("winner") in (None, "UNDECIDED"):
                continue
            h, a = m["home"]["teamId"], m["away"]["teamId"]
            res = {"HOME": ("W", "L"), "AWAY": ("L", "W"), "TIE": ("T", "T")}[m["winner"]]
            seq[h].append((p, res[0], f"{h}-{a}", True))
            seq[a].append((p, res[1], f"{h}-{a}", False))
    streaks = []
    for t, s in seq.items():
        if not s:
            continue
        last, n = s[-1][1], 0
        for x in reversed(s):
            if x[1] != last:
                break
            n += 1
        streaks.append({"team": names[t], "streak": f"{last}{n}", "type": last, "length": n})
    win_streak = sorted([x for x in streaks if x["type"] == "W"], key=lambda x: -x["length"])[:1]
    loss_streak = sorted([x for x in streaks if x["type"] == "L"], key=lambda x: -x["length"])[:1]
    luck = []
    for t, s in seq.items():
        actual = sum(1 if r == "W" else 0.5 if r == "T" else 0 for _, r, _, _ in s)
        exp = 0.0
        for p, _, key, is_home in s:
            o = hist["opening"].get(str(p), {}).get(key)
            h, a = map(int, key.split("-"))
            ph = o["ph"] if o else P[(h, a)]
            pt = o.get("pt", 0) if o else 0
            exp += (ph + pt / 2) if is_home else (1 - ph - pt / 2)
        if s:
            luck.append({"team": names[t], "actual_wins": actual, "expected_wins": round(exp, 1), "luck": round(actual - exp, 1)})
    luck.sort(key=lambda x: -x["luck"])
    run_home = []
    for t in names:
        opps = [(m["away"]["teamId"] if m["home"]["teamId"] == t else m["home"]["teamId"])
                for m in d["schedule"] if F < m["matchupPeriodId"] <= reg and "away" in m
                and t in (m["home"]["teamId"], m["away"]["teamId"])]
        if opps:
            run_home.append({"team": names[t], "games_left": len(opps),
                             "avg_opponent_power_pct": round(100 * sum(power[o] for o in opps) / len(opps))})
    run_home.sort(key=lambda x: -x["avg_opponent_power_pct"])  # toughest (strongest opponents) first

    # --- next week opening odds (live sim if that week is the current one, else season model)
    nxt = F + 1
    live = _live_odds(d) if cur == nxt else {}
    next_week, opening_next = [], {}
    for m in d["schedule"]:
        if m["matchupPeriodId"] != nxt or "away" not in m:
            continue
        h, a = m["home"]["teamId"], m["away"]["teamId"]
        ph, pt = live.get((h, a), (P[(h, a)], 0.0))
        mk = _h2h_market(ph, pt)
        pa = max(0.0, 1 - ph - pt)
        next_week.append({"home": names[h], "away": names[a], "favourite": names[h] if ph >= pa else names[a],
                          "favourite_win_pct": round(max(ph, pa) * 100), "price_home": mk["home"],
                          "price_away": mk["away"], "price_draw": mk["draw"],
                          "playoff": m.get("playoffTierType") not in (None, "NONE")})
        opening_next[f"{h}-{a}"] = {"ph": ph, "pt": pt, "price_home": mk["home"], "price_away": mk["away"]}
    if opening_next:
        hist["opening"][str(nxt)] = opening_next

    # --- futures
    sim, po_n, in_playoffs = _season_sim(d, P, live=live)
    teams_by_title = sorted(sim, key=lambda t: -sim[t]["title"])
    title_prices = dict(zip(teams_by_title, _book([sim[t]["title"] for t in teams_by_title], FUTURES_MARGIN)))
    alive_finals = [t for t in teams_by_title if not in_playoffs]
    finals_prices = dict(zip(alive_finals, _book([sim[t]["finals"] for t in alive_finals], FUTURES_MARGIN, places=po_n))) if alive_finals else {}
    prev_w_key = max((int(k) for k in hist["weekly"] if int(k) < F), default=None)
    prev_w = hist["weekly"].get(str(prev_w_key), {}) if prev_w_key else {}
    pt_prev = prev_w.get("title_prices", {})
    premiership = []
    for t in teams_by_title:
        if in_playoffs and sim[t]["title"] == 0:
            continue
        old = pt_prev.get(str(t))
        premiership.append({"team": names[t], "price": title_prices[t], "title_pct": round(sim[t]["title"] * 100, 1),
                            "previous_price": old,
                            "move": None if old is None or old == title_prices[t] else ("shortened" if title_prices[t] < old else "drifted")})
    fp_prev = prev_w.get("finals_prices", {})
    finals = [{"team": names[t], "price": finals_prices[t], "finals_pct": round(sim[t]["finals"] * 100),
               "previous_price": fp_prev.get(str(t))}
              for t in sorted(alive_finals, key=lambda t: -sim[t]["finals"])]

    order = sorted(power, key=lambda t: -power[t])
    pr_prev = prev_w.get("power_ranks", {})
    pp_prev = prev_w.get("power_pct", {})
    power_rankings = []
    for i, t in enumerate(order, 1):
        pct = round(power[t] * 100)
        power_rankings.append({"rank": i, "team": names[t], "chance_vs_avg_team_pct": pct,
                               "rank_move": (pr_prev[str(t)] - i) if str(t) in pr_prev else None,
                               "pct_change": (pct - pp_prev[str(t)]) if str(t) in pp_prev else None})
    standings = []
    for t in sorted(d["teams"], key=lambda t: t.get("playoffSeed", 99)):
        r = t["record"]["overall"]
        standings.append({"seed": t.get("playoffSeed"), "team": t.get("name"),
                          "record": f"{r['wins']}-{r['losses']}-{r['ties']}"})

    hist["weekly"][str(F)] = {"saved": datetime.now(timezone.utc).isoformat(),
                              "title_prices": {str(t): title_prices[t] for t in title_prices},
                              "title_pct": {str(t): sim[t]["title"] for t in sim},
                              "finals_prices": {str(t): finals_prices[t] for t in finals_prices},
                              "power_ranks": {str(t): i for i, t in enumerate(order, 1)},
                              "power_pct": {str(t): round(power[t] * 100) for t in order}}
    _posts_save(hist)

    return json.dumps({
        "finished_week": F, "next_week": nxt, "regular_season_weeks": reg, "finals_teams": po_n,
        "finals_format": f"top {po_n}; {'reseeded' if ss.get('playoffReseed') else 'fixed bracket'}; "
                         f"seeded by {ss.get('playoffSeedingRule')}", "in_finals": in_playoffs,
        "results_most_lopsided_first": results,
        "standings": standings, "power_rankings": power_rankings,
        "premiership_odds": premiership,
        "make_finals_odds": finals,
        "next_week_opening_odds": next_week,
        "awards": {
            "upset_of_the_week": upsets[:1], "opening_odds_source": opening_src,
            "biggest_comeback": comebacks[:1], "biggest_choke": chokes[:1],
            "bad_beat": bad_beats[:1], "team_of_the_week": team_week, "mvp_of_the_week": mvp[:1],
            "bench_points_of_the_week": bench_week, "best_acquisition": best_acq, "worst_acquisition": worst_acq,
            "worst_drop": worst_drop[:1], "longest_win_streak": win_streak, "longest_losing_streak": loss_streak,
            "luck_ladder": luck, "toughest_run_home": run_home[:3], "easiest_run_home": run_home[::-1][:3]},
        "notes": {"h2h_margin": H2H_MARGIN, "futures_margin": FUTURES_MARGIN, "season_sims": SEASON_SIMS,
                  "comeback_choke_need": "daily_post_data snapshots during the week (plus Monday opening odds)"}},
        indent=1)


if __name__ == "__main__":
    mcp.run()
