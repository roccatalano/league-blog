"""Injury report: every injured NBA player, how long they're projected out, and which fantasy
team rosters them. Writes data/injuries.json.

Sources (public, no login): ESPN's NBA injuries feed (status, injury, expected return date,
latest note) and each NBA team's schedule (to count games missed). Fantasy owners come from
data/model.json, which scripts/live.py writes earlier in the same run.
"""
import json, os, unicodedata
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
API = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ET = ZoneInfo("America/New_York")


def norm(name):
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    for suf in (" jr.", " jr", " sr.", " sr", " iii", " ii", " iv"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return "".join(c for c in s if c.isalnum())


def load_owners():
    try:
        with open(os.path.join(ROOT, "data", "model.json")) as f:
            m = json.load(f)
    except Exception:
        return {}
    owners = {}
    for t in m.get("teams", []):
        for p in t.get("players", []):
            r = p.get("rates") or {}
            line = None
            if r:
                line = {"PTS": round(r.get("0", 0), 1), "REB": round(r.get("6", 0), 1),
                        "AST": round(r.get("3", 0), 1)}
            owners[norm(p["name"])] = {"team": t["name"], "fantasy_status": p.get("status"),
                                       "per_game": line, "ir": p.get("slot") == 13}
    return owners


def team_dates(client, abbr, cache):
    """US Eastern game dates for an NBA team (whole season)."""
    if abbr in cache:
        return cache[abbr]
    days = []
    try:
        r = client.get(f"{API}/teams/{abbr.lower()}/schedule")
        r.raise_for_status()
        for e in r.json().get("events", []):
            dt = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
            days.append(dt.astimezone(ET).date())
    except Exception:
        days = None
    cache[abbr] = days
    return days


def bucket(days_out, season_end, ret):
    if ret is None:
        return "Unknown"
    if ret > season_end:
        return "Season"
    if days_out <= 2:
        return "Days"
    if days_out <= 7:
        return "1 week"
    if days_out <= 14:
        return "1-2 weeks"
    if days_out <= 28:
        return "2-4 weeks"
    if days_out <= 60:
        return "1-2 months"
    return "2+ months"


def main():
    today = datetime.now(ET).date()
    owners = load_owners()
    out, cache = [], {}
    with httpx.Client(timeout=20, headers={"User-Agent": "league-blog"}) as client:
        r = client.get(f"{API}/injuries")
        r.raise_for_status()
        feed = r.json()
        season_end = date(int(feed.get("season", {}).get("year", today.year + 1)), 4, 15)
        for team in feed.get("injuries", []):
            for i in team.get("injuries", []):
                a = i.get("athlete") or {}
                det = i.get("details") or {}
                abbr = (a.get("team") or {}).get("abbreviation") or ""
                ret = None
                if det.get("returnDate"):
                    try:
                        ret = date.fromisoformat(det["returnDate"][:10])
                    except ValueError:
                        pass
                days_out = max(0, (ret - today).days) if ret else None
                games = None
                sched = team_dates(client, abbr, cache) if abbr and ret else None
                if sched is not None and ret:
                    games = sum(1 for g in sched if today <= g < ret)
                    season_end = max(sched) if sched else season_end
                injury = " ".join(x for x in (det.get("side") if det.get("side") not in (None, "Not Specified") else None,
                                               det.get("type")) if x) or None
                own = owners.get(norm(a.get("displayName")))
                out.append({
                    "name": a.get("displayName"),
                    "nba_team": abbr,
                    "pos": (a.get("position") or {}).get("abbreviation"),
                    "status": i.get("status"),
                    "injury": injury,
                    "return_date": ret.isoformat() if ret else None,
                    "days_out": days_out,
                    "games_missed": games,
                    "timeframe": bucket(days_out or 0, season_end, ret),
                    "note": i.get("shortComment"),
                    "note_date": (i.get("date") or "")[:10] or None,
                    "fantasy_team": own["team"] if own else None,
                    "on_ir": bool(own and own["ir"]),
                    "per_game": own["per_game"] if own else None,
                })
    out.sort(key=lambda p: (p["return_date"] is None, p["return_date"] or "", p["name"] or ""))
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    with open(os.path.join(ROOT, "data", "injuries.json"), "w") as f:
        json.dump({"updated_utc": datetime.now(timezone.utc).isoformat(), "today_et": today.isoformat(),
                   "players": out}, f, indent=1)
    print(f"wrote data/injuries.json: {len(out)} injured, {sum(1 for p in out if p['fantasy_team'])} rostered")


if __name__ == "__main__":
    main()
