// A one-shot Brief is at most 500 characters (T20 B2), counted as the server counts them:
// Unicode code points of the raw text, before trimming (Python len, Array.from here). The
// textarea's native maxLength counts UTF-16 code units, so it is not used.
export const BRIEF_MAX_CHARS = 500;

export const briefLength = (text: string): number => Array.from(text).length;

export type FittedBrief = { value: string; caret: number; truncated: boolean };

// The next value of the Brief after an edit from `previous` to `next`. Within the limit it is
// `next` as typed. Over it, only the inserted text is cut (at a code point, never inside a
// surrogate pair), so a paste in the middle keeps the text after it; `caret` (UTF-16, for
// setSelectionRange) is the end of what was kept.
export function fitBrief(previous: string, next: string): FittedBrief {
  const after = Array.from(next);
  if (after.length <= BRIEF_MAX_CHARS) return { value: next, caret: next.length, truncated: false };
  const before = Array.from(previous);
  let prefix = 0;
  while (prefix < before.length && prefix < after.length && before[prefix] === after[prefix]) prefix++;
  let suffix = 0;
  while (suffix < before.length - prefix && suffix < after.length - prefix
    && before[before.length - 1 - suffix] === after[after.length - 1 - suffix]) suffix++;
  const head = after.slice(0, prefix);
  const tail = after.slice(after.length - suffix);
  const room = Math.max(0, BRIEF_MAX_CHARS - head.length - tail.length);
  const kept = head.concat(after.slice(prefix, after.length - suffix).slice(0, room));
  // Only reachable if `previous` was itself over the limit: cut the end as well.
  const value = kept.concat(tail).slice(0, BRIEF_MAX_CHARS).join("");
  return { value, caret: Math.min(kept.join("").length, value.length), truncated: true };
}
