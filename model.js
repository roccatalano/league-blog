// Port of the expected-wins model in scripts/server.py (_weekly_dist, _cat_probs, _match_win, _fixture_value).
(function (root) {
  function erf(x) { // Abramowitz-Stegun 7.1.26
    const s = x < 0 ? -1 : 1; x = Math.abs(x);
    const t = 1 / (1 + 0.3275911 * x);
    const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x);
    return s * y;
  }
  function make(M) {
    const C = M.consts, KEYS = C.KEYS, CATS = C.CATS, PP = C.PLAY_PROB;
    const pp = s => (s in PP ? PP[s] : 0.8);
    const value = r => r ? (r["0"] + 1.2 * r["6"] + 1.5 * r["3"] + 3 * r["2"] + 3 * r["1"] + r["17"] - r["11"]) : 0;
    function weeklyDist(players, gpp = 3.4, cap = 38) {
      const ps = players.filter(p => p.rates && p.slot !== 13 && pp(p.status) > 0)
        .sort((a, b) => value(b.rates) - value(a.rates));
      const mean = {}, vr = {}; KEYS.forEach(k => { mean[k] = 0; vr[k] = 0; });
      let used = 0;
      for (const p of ps) {
        const g = Math.min(gpp * pp(p.status), Math.max(0, cap - used));
        if (g <= 0) break;
        used += g;
        for (const k of KEYS) { const m = p.rates[k] || 0; mean[k] += g * m; vr[k] += g * (0.8 * m + 0.07 * m * m) + 0.5 * m * m; }
      }
      return [mean, vr];
    }
    function catProbs(da, db) {
      const [ma, va] = da, [mb, vb] = db, out = {}, VS = C.VAR_SCALE;
      for (const k of CATS.slice(0, 7)) {
        const sd = Math.sqrt(VS * (va[k] + vb[k])) || 1, z = (ma[k] - mb[k]) / sd;
        out[k] = 0.5 * (1 + erf((k === "11" ? -z : z) / Math.SQRT2));
      }
      for (const [k, mk, ak] of [["19", "13", "14"], ["20", "15", "16"]]) {
        if (!ma[ak] || !mb[ak]) { out[k] = 0.5; continue; }
        const pa = ma[mk] / ma[ak], pb = mb[mk] / mb[ak];
        const sd = Math.sqrt(pa * (1 - pa) / ma[ak] * 3 * VS + pb * (1 - pb) / mb[ak] * 3 * VS) || 1;
        out[k] = 0.5 * (1 + erf((pa - pb) / sd / Math.SQRT2));
      }
      return out;
    }
    function matchWin(probs) {
      let dist = [1];
      for (const p of Object.values(probs)) {
        const nd = new Array(dist.length + 1).fill(0);
        dist.forEach((x, i) => { nd[i] += x * (1 - p); nd[i + 1] += x * p; });
        dist = nd;
      }
      return dist.slice(5).reduce((a, b) => a + b, 0);
    }
    const rosterOf = id => M.teams.find(t => t.id === id).players;
    function fixtureValue(tid, rosters) {
      const mine = weeklyDist(rosters[tid] || rosterOf(tid));
      let exp = 0;
      for (const f of M.fixtures) {
        if (f.home !== tid && f.away !== tid) continue;
        const o = f.home === tid ? f.away : f.home;
        exp += matchWin(catProbs(mine, weeklyDist(rosters[o] || rosterOf(o))));
      }
      return exp;
    }
    function profile(tid, players, rosters) {
      const md = weeklyDist(players), acc = {}; CATS.forEach(k => acc[k] = 0);
      const others = M.teams.filter(t => t.id !== tid);
      for (const t of others) { const pr = catProbs(md, weeklyDist(rosters[t.id] || t.players)); CATS.forEach(k => acc[k] += pr[k] / others.length); }
      return acc;
    }
    // give = names leaving team a, get = names leaving team b
    function evaluate(a, b, giveNames, getNames, opts = {}) {
      const ra = rosterOf(a), rb = rosterOf(b);
      const give = ra.filter(p => giveNames.includes(p.name)), get = rb.filter(p => getNames.includes(p.name));
      const na = ra.filter(p => !give.includes(p)).concat(get), nb = rb.filter(p => !get.includes(p)).concat(give);
      const after = opts.originalOpponents ? {} : { [a]: na, [b]: nb };
      const side = (tid, before, aft) => {
        const bv = fixtureValue(tid, { [tid]: before });
        const av = fixtureValue(tid, Object.assign({}, after, { [tid]: aft }));
        const pb = profile(tid, before, {}), pa = profile(tid, aft, after);
        const cats = {}; CATS.forEach(k => cats[C.CN[k]] = pa[k] - pb[k]);
        return { change: (av - bv) * C.GAIN_CALIBRATION, cats };
      };
      return { a: side(a, ra, na), b: side(b, rb, nb) };
    }
    return { evaluate, weeklyDist, catProbs, matchWin, fixtureValue };
  }
  root.LeagueModel = { make };
  if (typeof module !== "undefined") module.exports = { make };
})(typeof window !== "undefined" ? window : globalThis);
