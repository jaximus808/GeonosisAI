import type { ReactNode } from 'react';
import { gxCx } from './cx';

/** TDM-109: a plate (panel). `surface` with a 1px `line` border, square corners, no shadow. */
export function Plate({
  title,
  aside,
  children,
  className,
  id,
  as: Tag = 'section',
  'aria-label': ariaLabel,
}: {
  title?: ReactNode;
  aside?: ReactNode;
  children?: ReactNode;
  className?: string;
  id?: string;
  as?: 'section' | 'div' | 'article' | 'aside';
  'aria-label'?: string;
}) {
  return (
    <Tag className={gxCx('gx-plate', className)} id={id} aria-label={ariaLabel}>
      {(title || aside) && (
        <div className="gx-plate-head">
          {title ? <h2 className="gx-heading">{title}</h2> : <span />}
          {aside}
        </div>
      )}
      {children}
    </Tag>
  );
}

/** A sunken well inside a plate: camera frames, thumbnails, code and log blocks. */
export function Well({ children, className }: { children?: ReactNode; className?: string }) {
  return <div className={gxCx('gx-well', className)}>{children}</div>;
}
