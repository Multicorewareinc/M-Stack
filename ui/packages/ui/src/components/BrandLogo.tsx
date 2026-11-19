import { cn } from '../styles/cn';

export interface BrandLogoProps {
  /** Product/organization name — always rendered as text, so a fork with no logo image still
   * gets a readable wordmark. */
  name: string;
  /** Optional logo image URL/path (e.g. `/logo.svg` served from the app's own `public/`). Omit
   * for a text-only wordmark. */
  logoUrl?: string;
  /** Pixel height of the logo image (width follows automatically). Ignored without `logoUrl`. */
  logoHeight?: number;
  className?: string;
  /** Overrides the name text's classes — callers place this in visually different spots (a
   * header's primary wordmark vs. a sidebar's small uppercase caption), so the text styling
   * can't be a single fixed default. */
  textClassName?: string;
}

/**
 * White-label brand mark — presentation only, no env/config coupling (this package stays
 * business-agnostic; each portal's own `branding.ts` resolves `name`/`logoUrl` from its env vars
 * and passes them in here). Used in both portals' Header and Sidebar so a fork's branding is
 * swapped by editing one config in one place, never by touching this component.
 */
export function BrandLogo({
  name,
  logoUrl,
  logoHeight = 20,
  className,
  textClassName = 'text-body font-semibold text-neutral-900',
}: BrandLogoProps) {
  return (
    <span className={cn('inline-flex items-center gap-2', className)}>
      {logoUrl && <img src={logoUrl} alt="" height={logoHeight} style={{ height: logoHeight }} className="w-auto" />}
      <span className={textClassName}>{name}</span>
    </span>
  );
}
