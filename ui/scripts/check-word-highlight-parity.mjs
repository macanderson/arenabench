#!/usr/bin/env node
/**
 * The TypeScript half of a cross-language parity check.
 *
 * `crates/stella-transcript/src/word.rs::highlight` decides which spans of a
 * modified line pair get the stronger tint, and `file_diff.rs`'s change-block
 * pairing decides which removal is compared with which addition. This page
 * reimplements both in TypeScript (`lib/word-highlight.ts`) because it is a
 * Next.js client with no way to call into the workspace — and an arena
 * transcript is read *beside* the deck and the Observatory when someone is
 * arguing about what a run did, which is exactly when one edit must not look
 * like two.
 *
 * The two halves cannot share a test runner — or, since the ejection, a
 * repository — so they share a **file**. The Rust source of truth lives in
 * https://github.com/macanderson/stella:
 * `crates/stella-transcript/tests/fixtures/word-highlight-matrix.txt`,
 * generated from the Rust and pinned there by `tests/word_highlight_matrix.rs`.
 * A vendored copy is committed here as `ui/golden/word-highlight-matrix.txt`,
 * and this script runs the TypeScript over that matrix and asserts it
 * reproduces the file exactly. So a Rust change to the rules must re-bless the
 * golden there, and the re-blessed matrix must be synced here by copying it
 * over the vendored file — which fails this check until the port catches up.
 *
 *   node scripts/check-word-highlight-parity.mjs
 *
 * Zero dependencies on purpose, and the same shape as its sibling
 * `check-diff-view-parity.mjs`: Node strips the type annotations from the
 * imported `.ts` natively (22.6+), so this runs against a checkout with no
 * `npm install` at all.
 *
 * Filed as #3676: before this existed the port had no word highlighting at all
 * and no mechanism to notice — the fourth time a hand-port of a Rust renderer
 * silently fell behind.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { highlight, highlightChangeBlock } from "../lib/word-highlight.ts";

const here = dirname(fileURLToPath(import.meta.url));
const GOLDEN = resolve(here, "../golden/word-highlight-matrix.txt");

/** Mirrors `repeat_tokens` in the Rust. */
function repeatTokens(n, stem) {
  const out = [];
  for (let i = 0; i < n; i += 1) out.push(`${stem}${i}`);
  return out.join(" ");
}

/** Mirrors `cases()` in `crates/stella-transcript/tests/word_highlight_matrix.rs`. */
function cases() {
  const long = "x".repeat(600);
  return [
    ["identical", "let x = 1;", "let x = 1;"],
    ["both-empty", "", ""],
    ["latex-parindent", "\\setlength{\\parindent}{15pt}", "\\setlength{\\parindent}{12pt}"],
    ["one-word-of-many", "the quick brown fox", "the quick lazy fox"],
    ["two-separate-runs", "a b c d e f g", "a X c d e Y g"],
    ["json-value", '  "retries": 3,', '  "retries": 5,'],
    ["insertion-only", "fn main() {}", "pub fn main() {}"],
    ["deletion-only", "pub fn main() {}", "fn main() {}"],
    ["short-run-length-rule", "value = 1", "value = 12"],
    ["shares-affix-suffix", "--fast", "--fast2"],
    ["shares-affix-prefix", "prefix_old", "prefix_new"],
    ["affix-too-short", "ab", "cd"],
    ["reindent", "    body();", "        body();"],
    ["brace-run", '{"a":{"b":1}}', '{"a":{"b":2}}'],
    ["wholly-different", "alpha beta gamma", "one two three four"],
    ["empty-against-text", "", "something new"],
    ["text-against-empty", "something old", ""],
    ["cjk", "設定 = 15", "設定 = 12"],
    ["emoji-run", "status: 🟢 ok", "status: 🔴 ok"],
    ["combining", "café = 1", "café = 2"],
    ["token-cap-exceeded", repeatTokens(600, "tok"), `${repeatTokens(600, "tok")} tail`],
    ["token-cap-just-under", repeatTokens(170, "tok"), repeatTokens(170, "tok").replaceAll("tok0 ", "tokZ ")],
    ["refine-cap-exceeded", `${long}A`, `${long}B`],
  ];
}

/** Mirrors `blocks()` in the Rust. */
function blocks() {
  return [
    ["balanced-1", ["let a = 1;"], ["let a = 2;"]],
    ["balanced-3", ["a = 1;", "b = 2;", "c = 3;"], ["a = 9;", "b = 2;", "c = 8;"]],
    ["more-removals", ["a = 1;", "b = 2;", "c = 3;"], ["a = 9;"]],
    ["more-additions", ["a = 1;"], ["a = 9;", "b = 2;", "c = 3;"]],
    ["removals-only", ["gone();"], []],
    ["additions-only", [], ["added();"]],
  ];
}

/**
 * Mirrors `escape` in the Rust. Five rules rather than `JSON.stringify`,
 * because `Debug for str` and `JSON.stringify` disagree on apostrophes, on
 * `\0`, and on how they spell an escaped code point.
 */
function escape(s) {
  let out = "";
  for (const ch of s) {
    if (ch === "\\") out += "\\\\";
    else if (ch === '"') out += '\\"';
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else out += ch;
  }
  return out;
}

function renderSpans(spans) {
  return spans.map((s) => `${s.changed ? "changed" : "plain"}("${escape(s.text)}")`).join(",");
}

function render() {
  // The header is the Rust's, verbatim: the file is compared byte for byte,
  // and a header drifting is itself a signal that one side was edited without
  // the other.
  const lines = [
    "# stella_transcript::word::highlight over a fixed matrix.",
    "# Generated — re-bless with:",
    "#   BLESS=1 cargo test -p stella-transcript --test word_highlight_matrix",
    "# The TypeScript port must reproduce this byte for byte. It lives in",
    "# github.com/macanderson/arenabench, which vendors this file as",
    "# `ui/golden/word-highlight-matrix.txt`; from that repo's `ui/`:",
    "#   node scripts/check-word-highlight-parity.mjs",
    "# Inputs are not echoed; `cases()`/`blocks()` are mirrored in the port.",
  ];
  for (const [label, oldLine, newLine] of cases()) {
    const [oldSpans, newSpans] = highlight(oldLine, newLine);
    lines.push(`pair=${label} old=${renderSpans(oldSpans)}`);
    lines.push(`pair=${label} new=${renderSpans(newSpans)}`);
  }
  for (const [label, removals, additions] of blocks()) {
    const { removed, added } = highlightChangeBlock(removals, additions);
    removed.forEach((spans, i) => lines.push(`block=${label} removed[${i}]=${renderSpans(spans)}`));
    added.forEach((spans, i) => lines.push(`block=${label} added[${i}]=${renderSpans(spans)}`));
  }
  return `${lines.join("\n")}\n`;
}

let golden;
try {
  golden = readFileSync(GOLDEN, "utf8");
} catch (err) {
  console.error(`word-highlight-parity: cannot read ${GOLDEN}\n  ${err.message}`);
  console.error(
    "Re-vendor it from the stella repo (github.com/macanderson/stella):",
  );
  console.error(
    "  BLESS=1 cargo test -p stella-transcript --test word_highlight_matrix",
  );
  console.error(
    "  cp crates/stella-transcript/tests/fixtures/word-highlight-matrix.txt <here>/ui/golden/",
  );
  process.exit(2);
}

const rendered = render();
if (golden === rendered) {
  const rows = rendered.split("\n").filter((l) => l.startsWith("pair=") || l.startsWith("block=")).length;
  console.log(`word-highlight-parity: OK — the TypeScript port matches the Rust on ${rows} rows.`);
  process.exit(0);
}

// Name the first differing line rather than dumping two blobs.
const g = golden.split("\n");
const r = rendered.split("\n");
const at = g.findIndex((line, i) => line !== r[i]);
console.error("word-highlight-parity: the TypeScript port no longer matches the Rust rules.");
if (at !== -1) {
  console.error(`  line ${at + 1}`);
  console.error(`  rust: ${g[at] ?? "(end of file)"}`);
  console.error(`  ts:   ${r[at] ?? "(end of file)"}`);
} else {
  console.error(`  lengths differ: ${g.length} rust vs ${r.length} ts`);
}
console.error("");
console.error("Fix whichever side is wrong — the Rust is not automatically the");
console.error("right one. Port to lib/word-highlight.ts, or change");
console.error("crates/stella-transcript/src/word.rs in github.com/macanderson/stella,");
console.error("re-bless the golden there, and copy it over ui/golden/:");
console.error("  BLESS=1 cargo test -p stella-transcript --test word_highlight_matrix");
process.exit(1);
