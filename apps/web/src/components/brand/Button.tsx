import Link from 'next/link';
import type { AnchorHTMLAttributes, ButtonHTMLAttributes, ReactNode } from 'react';
import { gxCx } from './cx';

export type GxButtonVariant = 'default' | 'primary' | 'danger' | 'ghost';

function gxButtonClass(variant: GxButtonVariant, block?: boolean, small?: boolean, className?: string) {
  return gxCx(
    'gx-btn',
    variant === 'primary' && 'gx-btn-primary',
    variant === 'danger' && 'gx-btn-danger',
    variant === 'ghost' && 'gx-btn-ghost',
    block && 'gx-btn-block',
    small && 'gx-btn-sm',
    className,
  );
}

/**
 * TDM-109: square-cornered button. At most one `primary` (melt) per view, for the step that moves the
 * ticket down the line. Labels are verb first, sentence case, at most three words. A disabled button
 * should come with a caption saying what it waits for (`disabledReason` renders it as the title).
 */
export function Button({
  variant = 'default',
  block,
  small,
  className,
  disabledReason,
  type = 'button',
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: GxButtonVariant;
  block?: boolean;
  small?: boolean;
  disabledReason?: string;
}) {
  return (
    <button
      type={type}
      title={rest.disabled && disabledReason ? disabledReason : rest.title}
      className={gxButtonClass(variant, block, small, className)}
      {...rest}
    />
  );
}

/** A link styled as a button (navigation, not an action). */
export function ButtonLink({
  href,
  variant = 'default',
  block,
  small,
  className,
  children,
  ...rest
}: Omit<AnchorHTMLAttributes<HTMLAnchorElement>, 'href'> & {
  href: string;
  variant?: GxButtonVariant;
  block?: boolean;
  small?: boolean;
  children?: ReactNode;
}) {
  return (
    <Link href={href} className={gxButtonClass(variant, block, small, className)} {...rest}>
      {children}
    </Link>
  );
}
