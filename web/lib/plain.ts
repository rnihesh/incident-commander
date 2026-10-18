// Display-only rewording of internal design codes in text that comes from the API or a
// recording ("passes every KTD23 clause"). Claims are hashed into the proof chain, so the
// stored text never changes: only what the page shows does.

const RULES: [RegExp, string][] = [
  [/\s*\(KTD\d+\)/g, ""],
  [/\bKTD\d+ (clauses?)\b/g, "$1 of the explanation rule"],
  [/\bthe KTD\d+ (predicate|rule)\b/g, "the explanation rule"],
  [/\bKTD\d+ (predicate|rule)\b/g, "the explanation rule"],
  [/\bKTD\d+ funnel\b/g, "elimination funnel"],
  [/\bKTD\d+\b/g, "the explanation rule"],
];

/** `text` with internal codes such as KTD23 replaced by plain words. */
export function plainText(text: string): string;
export function plainText(text: string | null | undefined): string | null | undefined;
export function plainText(text: string | null | undefined) {
  if (!text || !text.includes("KTD")) return text;
  return RULES.reduce((t, [re, to]) => t.replace(re, to), text);
}
