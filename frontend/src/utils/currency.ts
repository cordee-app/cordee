/**
 * Shared currency formatting for cost display across the app.
 *
 * Rules:
 * - USD amounts < $1 use 4 decimal places (cheap Scaleway models can cost
 *   fractions of a cent per execution).
 * - USD amounts >= $1 use 2 decimal places (readability for larger spends).
 * - EUR amounts always use 2 decimal places (GPU hourly rates are >= €0.93).
 * - Zero and negative values render as $0.00 / €0.00.
 */

/** Format a USD amount with adaptive precision. */
export const fmtUsd = (n: number | null | undefined): string => {
  const v = n || 0;
  if (v > 0 && v < 1) return `$${v.toFixed(4)}`;
  return `$${v.toFixed(2)}`;
};

/** Format a EUR amount (always 2 decimals). */
export const fmtEur = (n: number | null | undefined): string => {
  const v = n || 0;
  return `€${v.toFixed(2)}`;
};

/** Format a USD amount without the currency symbol (for inline use). */
export const fmtUsdValue = (n: number | null | undefined): string => {
  const v = n || 0;
  if (v > 0 && v < 1) return v.toFixed(4);
  return v.toFixed(2);
};

/** Format a percentage (0-100) with 1 decimal. */
export const fmtPct = (n: number | null | undefined): string => {
  return `${(n || 0).toFixed(1)}%`;
};
