// The command palette's fuzzy matching (androiddesktop/ui/fuzzy.js). Run: node tests/js/fuzzy.test.js
"use strict";
const path = require("path");
const fuzzy = require(path.join(__dirname, "..", "..", "androiddesktop", "ui", "fuzzy.js"));

let failures = 0;
function check(name, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) { failures++; console.log(`FAIL ${name}: got ${g}, want ${w}`); }
}

const titles = ["Settings", "Set up fingerprint", "Pair an iPhone or iPad", "Connect Claude", "Run checks",
  "Shut down Android", "Restart Android", "Install the virtual camera", "Camera", "Quit Android Desktop",
  "Chrome", "Clock", "Google Play Store"];
const items = titles.map((title) => ({ title }));
const top = (q) => (fuzzy.rank(q, items)[0] || { item: {} }).item.title;

check("empty query matches everything", fuzzy.rank("", items).length, items.length);
check("prefix", top("fing"), "Set up fingerprint");
check("words in any order", top("claude con"), "Connect Claude");
check("word starts", top("pair ip"), "Pair an iPhone or iPad");
check("letters in order", top("rstrt"), "Restart Android");
check("case doesn't matter", top("SHUT"), "Shut down Android");
check("a whole word beats letters spread out", top("camera"), "Camera");
check("no match", fuzzy.rank("zzz", items).length, 0);
check("every word must match", fuzzy.rank("camera zzz", items).length, 0);
check("letters out of order don't match", fuzzy.match("ab", "ba"), null);
check("letters scattered over words don't match", fuzzy.match("claude", "Close the launcher hide"), null);
check("a word is entered at its start", fuzzy.match("hrom", "Chrome Canary"), { score: fuzzy.match("hrom", "Chrome Canary").score, hits: [1, 2, 3, 4] });
check("...or not at all", fuzzy.match("hrm", "Chrome"), null);
check("initials", top("sda"), "Shut down Android");
check("letters within one word", top("chk"), "Run checks");

const m = fuzzy.match("fing", "Set up fingerprint");
check("hits mark the matched letters", m.hits, [7, 8, 9, 10]);
check("hits for letters in order", fuzzy.match("pip", "Pair an iPhone").hits, [0, 8, 9]);

// Keywords find an item its title doesn't mention, below title matches.
const withKeys = [{ title: "Run checks", keywords: "doctor diagnose" }, { title: "Doctor Who", keywords: "" }];
const ranked = fuzzy.rank("doctor", withKeys, (x) => x.title, (x) => x.keywords);
check("keywords match", ranked.map((r) => r.item.title), ["Doctor Who", "Run checks"]);
check("no hits shown for a keyword match", ranked[1].hits, []);

// Ties keep the order they came in.
check("stable", fuzzy.rank("x", [{ title: "x" }, { title: "x" }]).map((r) => r.order), [0, 1]);

if (failures) { console.log(`${failures} failed`); process.exit(1); }
console.log("ok");
