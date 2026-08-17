/** Strips likely secrets (API keys, bearer tokens, provider key prefixes) from free text. */
export function redactSecrets(value: string): string {
  return value
    .replace(/((?:api|access|refresh)[-_ ]?(?:key|token)\s*[:=]\s*)[^\s,;)"']+/gi, "$1[redacted]")
    .replace(/(\bBearer\s+)[^\s,;)"']+/gi, "$1[redacted]")
    .replace(/\b(?:sk(?:-or-v1)?-|gh[pousr]_|github_pat_|xai-)[A-Za-z0-9_-]{12,}\b/gi, "[redacted]");
}

const maxRedactDepth = 12;
const maxRedactArrayItems = 200;
const maxRedactStringChars = 20_000;

/** Recursively redacts string leaves in an arbitrary JSON-shaped value, bounding depth/size. */
export function redactJsonDeep(value: unknown, depth = 0): unknown {
  if (depth >= maxRedactDepth) return "[truncated: too deep]";
  if (typeof value === "string") {
    return redactSecrets(value.length > maxRedactStringChars ? `${value.slice(0, maxRedactStringChars)}…[truncated]` : value);
  }
  if (Array.isArray(value)) {
    const items = value.slice(0, maxRedactArrayItems).map((item) => redactJsonDeep(item, depth + 1));
    if (value.length > maxRedactArrayItems) items.push(`…[${value.length - maxRedactArrayItems} more items truncated]`);
    return items;
  }
  if (value && typeof value === "object") {
    const result: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      result[key] = redactJsonDeep(item, depth + 1);
    }
    return result;
  }
  return value;
}
