// Fuzzy matching for the command palette: "fing" finds "Set up fingerprint", "pair ip" finds "Pair an
// iPhone or iPad", "claude con" finds "Connect Claude Code". Each word of the query has to turn up in
// the text, its letters in order; word starts and letters in a row count most. Also loaded by Node for
// tests/js/fuzzy.test.js.
(function (root) {
  "use strict";

  function wordStart(t, i) {
    return i === 0 || /[\s\-_./()&,:"'+]/.test(t[i - 1]);
  }

  // One query word in the text: {score, hits} or null.
  function matchWord(w, t) {
    // As a whole, preferably where a word starts.
    let at = -1;
    for (let i = t.indexOf(w); i >= 0; i = t.indexOf(w, i + 1)) {
      if (at < 0) at = i;
      if (wordStart(t, i)) { at = i; break; }
    }
    if (at >= 0) {
      const score = 100 + w.length * 6 + (wordStart(t, at) ? 60 : 0) + (at === 0 ? 30 : 0) - Math.min(at, 40) * 0.5;
      return { score, hits: Array.from(w, (_, k) => at + k) };
    }
    // Letter by letter: the next letter right after the last one, else where a later word starts, else
    // further on in the same word. A word is only ever entered at its start ("pip": Pair iPhone,
    // "rstrt": Restart; not "claude": CLose the lAUncher hiDE).
    const hits = [];
    let score = 0;
    let from = 0;
    for (const c of w) {
      let i = -1;
      if (hits.length && t[from] === c) i = from;
      if (i < 0) {
        for (let j = t.indexOf(c, from); j >= 0; j = t.indexOf(c, j + 1)) {
          if (wordStart(t, j)) { i = j; break; }
        }
      }
      if (i < 0 && hits.length) {
        const j = t.indexOf(c, from);
        const gap = t.slice(from, j);
        if (j >= 0 && !/[\s\-_./()&,:"'+]/.test(gap)) i = j; // still in the same word
      }
      if (i < 0) return null;
      const together = hits.length && i === hits[hits.length - 1] + 1;
      score += 10 + (wordStart(t, i) ? 18 : 0) + (together ? 12 : 0) - Math.min(i - from, 8);
      hits.push(i);
      from = i + 1;
    }
    return { score, hits };
  }

  // The query against a text: {score, hits} (hits: indexes of the matched letters) or null.
  function match(query, text) {
    const words = String(query || "").toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length) return { score: 0, hits: [] };
    const t = String(text || "").toLowerCase();
    let score = 0;
    const hits = new Set();
    for (const w of words) {
      const m = matchWord(w, t);
      if (!m) return null;
      score += m.score;
      m.hits.forEach((h) => hits.add(h));
    }
    return { score: score - t.length * 0.2, hits: [...hits].sort((a, b) => a - b) };
  }

  // Items that match, best first (ties keep their order). Each item's title counts fully, its keywords
  // (other words for the same thing) a little less.
  function rank(query, items, title = (x) => x.title, keywords = (x) => x.keywords || "") {
    const out = [];
    items.forEach((item, order) => {
      const m = match(query, title(item));
      if (m) { out.push({ item, score: m.score, hits: m.hits, order }); return; }
      const k = match(query, `${title(item)} ${keywords(item)}`);
      if (k) out.push({ item, score: k.score * 0.6, hits: [], order });
    });
    return out.sort((a, b) => b.score - a.score || a.order - b.order);
  }

  const api = { match, rank };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.fuzzy = api;
})(this);
