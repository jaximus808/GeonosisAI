import { gxCx } from './cx';

/**
 * TDM-109: the GeonosisAI wordmark. Source text keeps the real casing ("GeonosisAI") and CSS
 * uppercases it, so screen readers say the name. "AI" is the only melt in it.
 */
export function Wordmark({
  size = 'md',
  descriptor = false,
  className,
}: {
  size?: 'sm' | 'md' | 'hero';
  /** Show "Robot deployment foundry" to the right (never underneath). */
  descriptor?: boolean;
  className?: string;
}) {
  return (
    <span className={gxCx('inline-flex items-baseline gap-3', className)}>
      <span
        className={gxCx('gx-wordmark', size === 'sm' && 'gx-wordmark-sm', size === 'hero' && 'gx-wordmark-hero')}
        data-testid="gx-wordmark"
      >
        Geonosis<b>AI</b>
      </span>
      {descriptor && <span className="gx-descriptor hidden md:inline">Robot deployment foundry</span>}
    </span>
  );
}
