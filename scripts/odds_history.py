"""Win-probability history for every matchup, for the live charts on the odds page.
Appends the current data/odds.json to data/odds_history.json. Run after live.py.

Format: {"<period>": {"<home>|<away>": [[iso_utc, home_pct, tie_pct, cats_home, cats_away], ...]}}
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PATH = os.path.join(ROOT, "data", "odds_history.json")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def add(hist, o):
    """Add one odds.json snapshot. Returns True if anything new was stored."""
    if not o or not o.get("matchups") or not o.get("updated_utc"):
        return False
    wk = hist.setdefault(str(o["matchup_period"]), {})
    ts = o["updated_utc"][:19] + "Z"
    new = False
    for m in o["matchups"]:
        pts = wk.setdefault(f'{m["home"]}|{m["away"]}', [])
        if any(p[0] == ts for p in pts):
            continue
        s = m.get("score_now") or [0, 0, 9]
        pts.append([ts, m["home_win_pct"], m.get("tie_pct", 0), s[0], s[1]])
        pts.sort(key=lambda p: p[0])
        new = True
    return new


def save(hist):
    with open(PATH, "w") as f:
        json.dump(hist, f, separators=(",", ":"))


def main():
    hist = load(PATH, {})
    if add(hist, load(os.path.join(ROOT, "data", "odds.json"), None)):
        save(hist)
        print("odds history updated")
    else:
        print("odds history unchanged")


if __name__ == "__main__":
    main()
