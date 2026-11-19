/**
 * Shared hex/HSL color math behind every runtime theme override (accentColor.ts, themeSurface.ts)
 * — kept in one place so all of them parse/derive colors identically. No component imports this
 * directly; it's an internal helper for the two `apply*Color` entry points.
 */

export type Rgb = readonly [number, number, number];

export function clamp(n: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, n));
}

export function hexToRgb(hex: string): Rgb | null {
  const cleaned = hex.trim().replace(/^#/, '');
  const expanded =
    cleaned.length === 3
      ? cleaned
          .split('')
          .map((c) => c + c)
          .join('')
      : cleaned;
  if (!/^[0-9a-fA-F]{6}$/.test(expanded)) return null;
  const r = parseInt(expanded.slice(0, 2), 16);
  const g = parseInt(expanded.slice(2, 4), 16);
  const b = parseInt(expanded.slice(4, 6), 16);
  return [r, g, b];
}

export function rgbToHsl([r, g, b]: Rgb): readonly [number, number, number] {
  const rN = r / 255;
  const gN = g / 255;
  const bN = b / 255;
  const max = Math.max(rN, gN, bN);
  const min = Math.min(rN, gN, bN);
  const l = (max + min) / 2;
  if (max === min) return [0, 0, l * 100];
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h: number;
  switch (max) {
    case rN:
      h = (gN - bN) / d + (gN < bN ? 6 : 0);
      break;
    case gN:
      h = (bN - rN) / d + 2;
      break;
    default:
      h = (rN - gN) / d + 4;
  }
  return [h * 60, s * 100, l * 100];
}

export function hslToRgb(h: number, s: number, l: number): Rgb {
  const sN = clamp(s, 0, 100) / 100;
  const lN = clamp(l, 0, 100) / 100;
  if (sN === 0) {
    const v = Math.round(lN * 255);
    return [v, v, v];
  }
  const q = lN < 0.5 ? lN * (1 + sN) : lN + sN - lN * sN;
  const p = 2 * lN - q;
  const hue2rgb = (t: number): number => {
    let tt = t;
    if (tt < 0) tt += 1;
    if (tt > 1) tt -= 1;
    if (tt < 1 / 6) return p + (q - p) * 6 * tt;
    if (tt < 1 / 2) return q;
    if (tt < 2 / 3) return p + (q - p) * (2 / 3 - tt) * 6;
    return p;
  };
  const hN = h / 360;
  return [
    Math.round(hue2rgb(hN + 1 / 3) * 255),
    Math.round(hue2rgb(hN) * 255),
    Math.round(hue2rgb(hN - 1 / 3) * 255),
  ];
}

/** "R G B" (space-separated, no commas) — the format globals.css stores every `--color-*`
 * variable in, so Tailwind's `rgb(var(--color-x) / <alpha-value>)` pattern can apply alpha. */
export function rgbTriplet(rgb: Rgb): string {
  return rgb.join(' ');
}
