export function fmtTokens(n: number): string {
  const v = Math.floor(n || 0);
  if (v < 1000) return String(v);
  const m = Math.floor(v / 1_000_000);
  const k = Math.floor((v % 1_000_000) / 1_000);
  if (m > 0) return k > 0 ? `${m}M ${k}K` : `${m}M`;
  return `${k}K`;
}
