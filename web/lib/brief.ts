// A one-shot Brief is at most 500 characters (T20 B2), counted as the server counts them:
// Unicode code points of the raw text, before trimming (Python len, Array.from here). The
// textarea's native maxLength counts UTF-16 code units, so it is not used.
export const BRIEF_MAX_CHARS = 500;

export const briefLength = (text: string): number => Array.from(text).length;

export type FittedBrief = { value: string; caret: number; truncated: boolean };

// The value to keep after an edit produced `next`, with the caret where the browser put it
// (`selectionEnd`, UTF-16). An insertion, typed or pasted, and a paste over a selection all
// leave the caret at the end of the inserted text, so the text after the caret is existing
// text: it is always kept. Over the limit only the text before the caret is cut, from its
// end, which removes inserted text only (before the edit the Brief fitted). Cuts fall
// between code points, never inside a surrogate pair; `caret` is the end of what was kept.
export function fitBrief(next: string, caret: number = next.length): FittedBrief {
  if (briefLength(next) <= BRIEF_MAX_CHARS) return { value: next, caret, truncated: false };
  const head = Array.from(next.slice(0, caret));
  const tail = Array.from(next.slice(caret));
  const kept = head.slice(0, Math.max(0, BRIEF_MAX_CHARS - tail.length)).join("");
  // `tail` alone over the limit is only possible if the Brief was already over it.
  const value = Array.from(kept + tail.join("")).slice(0, BRIEF_MAX_CHARS).join("");
  return { value, caret: Math.min(kept.length, value.length), truncated: true };
}
