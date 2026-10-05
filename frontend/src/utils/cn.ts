export type ClassValue =
  | string
  | number
  | null
  | false
  | undefined
  | ClassValue[]
  | { [key: string]: unknown };

export function cn(...inputs: ClassValue[]): string {
  const out: string[] = [];

  const walk = (val: ClassValue): void => {
    if (!val) return;
    if (typeof val === "string" || typeof val === "number") {
      out.push(String(val));
    } else if (Array.isArray(val)) {
      val.forEach(walk);
    } else if (typeof val === "object") {
      for (const k of Object.keys(val)) {
        if (val[k]) out.push(k);
      }
    }
  };

  inputs.forEach(walk);
  return out.join(" ");
}