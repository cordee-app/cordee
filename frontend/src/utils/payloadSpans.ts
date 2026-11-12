// Mirrors agent_api.py `_extract_payload_blocks` / `_resolve_odd_markers`,
// prompt_builder.py `_resolve_referenced_files` (the @file mention rules) and
// agent_executor.py `_URL_RE` (web-link recognition). Keep in lockstep.
//
// Pure helpers for the live recognition overlay in the task description
// textarea: verbatim payload blocks (''' … ''' / ``` … ```), @file mentions and
// stray (unpaired) markers. No React imports — this must stay unit-testable.

export type PayloadSpan = { start: number; end: number };
export type MentionSpan = { start: number; end: number; known: boolean };
export type FileRef = { name: string; rel: string; hidden?: boolean; category?: string };

// Global regex: never share one instance across calls (lastIndex hazard), so
// scanPayloads() rebuilds it from this source+flags every time.
const BLOCK_RE = /'''(.*?)'''|```(.*?)```/gs;
const TOKEN_RE = /\[\[\s*PAYLOAD_/i;
// Start of a mention: an `@` at the string start or right after whitespace
// (so `a@b.com` is not a mention).
const MENTION_START_RE = /(^|\s)@/g;
// Same set the backend uses for the stem's "identifier-ish" test:
// `any(c in stem for c in '_-') or any(c.isdigit() for c in stem)
//  or any(c.isupper() for c in stem)` — so underscore/hyphen, a digit, or an
// UPPERCASE letter. A lowercase-only stem is NOT identifier-ish.
const IDENTIFIERISH_RE = /[_\-\p{N}\p{Lu}]/u;
// Fallback name capture (old behaviour): up to the first whitespace / `@`.
const FALLBACK_NAME_RE = /^[^\s@]+/;
// Python `\w` — a char that keeps a word-bounded match going. Used for the
// `(?!\w)` right-boundary of the stem and lead rules.
const WORD_CHAR_RE = /[\p{L}\p{N}_]/u;
// Trailing sentence punctuation is never part of a mention name.
const TRAILING_PUNCT_RE = /[.,;:!?]+$/;
// Web links — byte-for-byte mirror of agent_executor.py `_URL_RE`
// (r'https?://[^\s<>"\')\]]+', re.IGNORECASE). Any URL in the description
// makes the task need web access (the G1 guard holds routes without it, and
// web-capable routes are told to use web_search / web_fetch). NB: the match
// INCLUDES trailing punctuation like a final '.', exactly as Python sees it.
const URL_RE = /https?:\/\/[^\s<>"')\]]+/gi;
// Mirrors `_LEAD_TOKEN_DISTINCTIVE_MAX` in prompt_builder.py.
const LEAD_TOKEN_DISTINCTIVE_MAX = 2;
// The folders the backend's file auto-catch actually walks — agent_tools.py
// `_WORKING_DOC_FOLDERS` + `Artifacts/outputs` (prompt_builder.py adds the
// latter), max_depth=3, dot-entries skipped. Files elsewhere in the catalog
// (project root, data/, Artifacts/chats, …) are visible in the file browser
// but can NEVER be caught by the auto-catch, so they must not turn blue.
const SCAN_FOLDERS = [
  'My Docs',
  'Working Docs',
  'Working Documents',
  'working-docs',
  'docs',
  'Artifacts/outputs',
];
// list_files_under(max_depth=3): files up to 3 path segments below the scan
// folder are reachable (root/a/b/file.ext), a 4th level is never entered.
const SCAN_MAX_DEPTH = 3;

/** True when the backend's auto-catch scan can actually see this rel path. */
function isCatchable(rel: string): boolean {
  const norm = rel.replace(/\\/g, '/');
  for (const folder of SCAN_FOLDERS) {
    if (!norm.startsWith(folder + '/')) continue;
    const below = norm.slice(folder.length + 1).split('/');
    if (below.length > SCAN_MAX_DEPTH) continue;
    if (below.some((seg) => seg.startsWith('.'))) continue; // skip_hidden
    return true;
  }
  return false;
}

// Same-length sentinels: Python masks with 5-char sentinels and restores them
// before use, so pairing/scoring is unaffected by the width. Using 3 chars
// (exactly the delimiter width) keeps every index in the scanned text valid on
// the ORIGINAL string — which is what lets us highlight the untouched value.
const SENTINEL_Q = '\u0001Q\u0001'; // replaces a stray ''' occurrence
const SENTINEL_F = '\u0001F\u0001'; // replaces a stray ``` occurrence

function countOccurrences(text: string, delim: string): number {
  // Python str.count() semantics: non-overlapping occurrences.
  let n = 0;
  let pos = 0;
  while ((pos = text.indexOf(delim, pos)) >= 0) {
    n += 1;
    pos += delim.length;
  }
  return n;
}

function scanPayloads(text: string): Array<{ start: number; end: number; raw: string }> {
  const re = new RegExp(BLOCK_RE.source, BLOCK_RE.flags);
  const spans: Array<{ start: number; end: number; raw: string }> = [];
  for (const m of text.matchAll(re)) {
    const content = m[1] !== undefined ? m[1] : m[2];
    if (content === undefined || !content.trim()) continue;
    const start = m.index ?? 0;
    spans.push({ start, end: start + m[0].length, raw: m[0] });
  }
  return spans;
}

function isBlockStyle(raw: string): boolean {
  const body = raw.slice(3, -3);
  return body.startsWith('\n') && body.endsWith('\n');
}

function payloadScore(spans: Array<{ raw: string }>): [number, number] {
  return [
    spans.filter((s) => isBlockStyle(s.raw)).length,
    spans.reduce((sum, s) => sum + s.raw.length, 0),
  ];
}

/** Replace the k-th (1-based) occurrence of `delim` with `sentinel`; null if absent. */
function maskOccurrence(
  text: string,
  delim: string,
  k: number,
  sentinel: string,
): string | null {
  let pos = -1;
  for (let i = 0; i < k; i += 1) {
    pos = text.indexOf(delim, pos + 1);
    if (pos < 0) return null;
  }
  return text.slice(0, pos) + sentinel + text.slice(pos + delim.length);
}

function scoreGreater(a: [number, number], b: [number, number]): boolean {
  return a[0] > b[0] || (a[0] === b[0] && a[1] > b[1]);
}

/**
 * Mask one stray marker per delimiter type that has an odd count.
 *
 * A mask is adopted ONLY if it strictly increases the number of block-style
 * payloads (markers on their own lines) — same rule as the backend.
 */
function resolveOddMarkers(text: string): string {
  let out = text;
  const pairs: Array<[string, string]> = [
    ["'''", SENTINEL_Q],
    ['```', SENTINEL_F],
  ];
  for (const [delim, sentinel] of pairs) {
    const n = countOccurrences(out, delim);
    if (n < 3 || n % 2 === 0) continue;
    const baseScore = payloadScore(scanPayloads(out));
    let best: { score: [number, number]; text: string } | null = null;
    for (let k = 1; k <= n; k += 1) {
      const cand = maskOccurrence(out, delim, k, sentinel);
      if (cand === null) continue;
      const score = payloadScore(scanPayloads(cand));
      if (score[0] > baseScore[0] && (best === null || scoreGreater(score, best.score))) {
        best = { score, text: cand };
      }
    }
    if (best) out = best.text;
  }
  return out;
}

export function computePayloadSpans(text: string): {
  spans: PayloadSpan[];
  unpaired: number[];
} {
  if (!text) return { spans: [], unpaired: [] };
  // The backend skips extraction entirely when a payload token is already
  // present, so show no recognition at all in that case.
  if (TOKEN_RE.test(text)) return { spans: [], unpaired: [] };

  const scanText = resolveOddMarkers(text);
  const spans: PayloadSpan[] = scanPayloads(scanText).map(({ start, end }) => ({ start, end }));

  const unpaired: number[] = [];
  for (const delim of ["'''", '```']) {
    let pos = scanText.indexOf(delim);
    while (pos >= 0) {
      // Both delimiters of a matched block lie inside its span; masked
      // sentinels no longer match the delimiter pattern, so adopted masks are
      // naturally not flagged.
      const inside = spans.some((s) => pos >= s.start && pos + delim.length <= s.end);
      if (!inside) unpaired.push(pos);
      pos = scanText.indexOf(delim, pos + 1);
    }
  }
  unpaired.sort((a, b) => a - b);
  return { spans, unpaired };
}

function baseName(rel: string): string {
  const parts = rel.split('/');
  return parts[parts.length - 1] ?? rel;
}

/** Basename without its last extension — Python's `os.path.splitext()[0]`. */
function stemOf(name: string): string {
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(0, dot) : name;
}

/** Python's `re.split(r'[_\-\s]', stem, 1)[0]`. */
function leadTokenOf(stem: string): string {
  return stem.split(/[_\-\s]/, 1)[0] ?? stem;
}

/** Python's `_referenced()` "identifierish" test, on the ORIGINAL stem. */
function isIdentifierish(stem: string): boolean {
  return stem.length >= 6 && IDENTIFIERISH_RE.test(stem);
}

/** Python's `(?!\w)`: true when the char right after the match is not a word char. */
function isWordBoundary(text: string, end: number): boolean {
  if (end >= text.length) return true;
  return !WORD_CHAR_RE.test(text[end]);
}

/**
 * Longest case-SENSITIVE candidate key that matches `rest` at `skip` (0, or
 * just after a path-like prefix). Case-sensitivity mirrors the backend
 * (`fname in desc` and the stem/lead regexes; only the lead COUNTING key is
 * lowercased in Python).
 *
 * `requireWordBoundary` mirrors the backend's `(?!\w)` right-boundary (stem and
 * lead rules). The full-name rule passes `false`: the backend catches it with
 * a plain substring test (`fname in desc`), so ANY following character —
 * including a word char — still means the file is caught.
 *
 * `allowPathPrefix` (full-name rule only): the name may be preceded by a
 * path-like run (`data/README.md`, `sub/dir/file.pdf`) — characters that are
 * not whitespace and not `@`. The span then covers the whole path form, and
 * the backend still catches it because the basename is a substring.
 */
function longestPrefixMatch(
  rest: string,
  candidates: FileRef[],
  keyOf: (f: FileRef) => string,
  requireWordBoundary: boolean,
  allowPathPrefix = false,
): number {
  // Longest path-like prefix of `rest` (may be empty) that ends at a `/`.
  let skip = 0;
  if (allowPathPrefix) {
    const m = rest.match(/^[^\s@]*/)!;
    const slash = m[0].lastIndexOf('/');
    if (slash >= 0) skip = slash + 1;
  }
  let best = 0;
  for (const f of candidates) {
    const key = keyOf(f);
    if (!key || key.length <= best || key.length > rest.length - skip) continue;
    if (!rest.startsWith(key, skip)) continue;
    if (requireWordBoundary && !isWordBoundary(rest, skip + key.length)) continue;
    best = skip + key.length;
  }
  return best;
}

/**
 * @file mention spans for the live overlay.
 *
 * Mirrors `prompt_builder.py::_resolve_referenced_files._referenced`, which
 * catches a file when the description contains (in this order of precedence):
 *   R1 — the full file name (spaces allowed, case-SENSITIVE plain substring —
 *        no boundary requirement, like `fname in desc`);
 *   R2 — the full stem, when it is identifier-ish (>= 6 chars and containing
 *        `_`, `-`, a digit or an uppercase letter), word-bounded after;
 *   R3 — a distinctive leading token of the stem (>= 4 chars, shared by at
 *        most `_LEAD_TOKEN_DISTINCTIVE_MAX` = 2 files in the scan),
 *        word-bounded after.
 * Matching is case-sensitive throughout (only the lead COUNTING key is
 * lowercased, as in Python). The LONGEST match across all three wins so
 * precedence is deterministic. Anything else
 * falls back to the old whitespace-delimited capture and is flagged unknown
 * (gray). Keep both sides in lockstep when changing either.
 */
export function computeMentionSpans(
  text: string,
  files: FileRef[],
  payloadSpans: PayloadSpan[],
): MentionSpan[] {
  if (!text) return [];

  // Lead-token frequency over the files the backend's scan can actually see
  // (mirrors the backend, which counts its own scanned files only).
  const scannable = (f: FileRef) => isCatchable(f.rel || f.name);
  const leadCounts = new Map<string, number>();
  for (const f of files) {
    if (!scannable(f)) continue;
    const key = leadTokenOf(stemOf(f.name || baseName(f.rel))).toLowerCase();
    leadCounts.set(key, (leadCounts.get(key) ?? 0) + 1);
  }

  // Only files that are both displayable and reachable by the backend's scan
  // can be recognized (blue); anything else falls back to gray.
  const candidates = files.filter(
    (f) => !f.hidden && f.category !== 'system' && scannable(f),
  );

  const out: MentionSpan[] = [];
  const re = new RegExp(MENTION_START_RE.source, 'g');
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    const at = m.index + m[1].length;
    if (payloadSpans.some((s) => at >= s.start && at < s.end)) continue;
    const rest = text.slice(at + 1);

    // `@https://…` / `@http://…` is a web link, not a file mention — the file
    // rules can never match a URL (basenames contain no `://`), so leave it to
    // computeUrlSpans instead of emitting a misleading gray span.
    if (/^https?:\/\//i.test(rest)) continue;

    // R1 — full file name, spaces allowed. Plain substring on the backend
    // (`fname in desc`), so no boundary requirement. The name may be preceded
    // by a path-style prefix (`@data/README.md`): the backend matches the
    // BASENAME anywhere, so `dir/name` counts as long as only path-like
    // characters (no whitespace) sit between `@` and the name.
    const r1 = longestPrefixMatch(
      rest,
      candidates,
      (f) => f.name,
      false,
      true,
    );

    // R2 — full stem, identifier-ish only, word-bounded after (`(?!\w)`).
    const r2 = longestPrefixMatch(
      rest,
      candidates,
      (f) => {
        const stem = stemOf(f.name);
        return isIdentifierish(stem) ? stem : '';
      },
      true,
    );

    // R3 — distinctive lead token of the stem: >= 4 chars and shared by at most
    // LEAD_TOKEN_DISTINCTIVE_MAX files. Matched from the FILE side so the lead
    // keeps its dots (Python splits the stem only on `_`, `-`, whitespace) and
    // word-bounded after, exactly like the backend's `(?!\w)` search.
    const r3 = longestPrefixMatch(
      rest,
      candidates.filter((f) => {
        const lead = leadTokenOf(stemOf(f.name));
        return (
          lead.length >= 4 &&
          (leadCounts.get(lead.toLowerCase()) ?? 0) <= LEAD_TOKEN_DISTINCTIVE_MAX
        );
      }),
      (f) => leadTokenOf(stemOf(f.name)),
      true,
    );

    // Longest match wins — a full name always beats its own stem, which beats
    // its own lead, and cross-file the longest span is the most specific.
    const len = Math.max(r1, r2, r3);
    if (len > 0) {
      out.push({ start: at, end: at + 1 + len, known: true });
      // Skip the consumed name so a second `@` inside it is not re-scanned.
      re.lastIndex = at + 1 + len;
      continue;
    }

    // Fallback: old whitespace-delimited capture, unknown (gray).
    const name = (rest.match(FALLBACK_NAME_RE) ?? [''])[0].replace(TRAILING_PUNCT_RE, '');
    if (!name) continue;
    out.push({ start: at, end: at + 1 + name.length, known: false });
    re.lastIndex = at + 1 + name.length;
  }
  return out;
}

/**
 * Web-link spans for the live overlay.
 *
 * Byte-for-byte mirror of agent_executor.py `_URL_RE`
 * (r'https?://[^\s<>"\')\]]+', IGNORECASE): every match means the task needs
 * web access — the G1 guard holds routes without it, and web-capable routes
 * are instructed to use web_search / web_fetch. A URL inside a ''' payload
 * block is skipped here: the payload renders as one amber unit (the backend
 * still sees the URL in the raw description, but the block is the visual
 * unit, same convention as @mentions inside payloads).
 */
export function computeUrlSpans(text: string, payloadSpans: PayloadSpan[]): PayloadSpan[] {
  if (!text) return [];
  const out: PayloadSpan[] = [];
  const re = new RegExp(URL_RE.source, 'gi');
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    const start = m.index;
    if (payloadSpans.some((s) => start >= s.start && start < s.end)) continue;
    out.push({ start, end: start + m[0].length });
  }
  return out;
}
