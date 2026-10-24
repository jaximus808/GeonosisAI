import type { ReactNode } from 'react';
import { gxCx } from './cx';
import { gxDollars } from './Meter';
import { Tag } from './StatusTag';

/**
 * TDM-109: a paid task on a contributor's phone. Kind as a money tag, the price in ochre mono top
 * right, a verb-first title, one sentence of instructions, facts (duration, robot, slots left), and
 * one full-width action at the bottom (pass it as `action`). `taken` swaps the action for a steel
 * "Taken" tag.
 */
export function TaskCard({
  kind,
  priceCents,
  title,
  instructions,
  facts = [],
  action,
  taken,
  children,
  className,
  testId,
}: {
  kind: string;
  priceCents?: number | bigint;
  title: ReactNode;
  instructions?: ReactNode;
  facts?: ReactNode[];
  action?: ReactNode;
  taken?: boolean;
  children?: ReactNode;
  className?: string;
  testId?: string;
}) {
  return (
    <article className={gxCx('gx-task', className)} data-testid={testId}>
      <div className="flex items-center justify-between gap-3">
        <Tag tone="money">{kind}</Tag>
        {priceCents != null && <span className="gx-price">{gxDollars(priceCents)}</span>}
      </div>
      <h3 className="gx-task-title">{title}</h3>
      {instructions && <p className="m-0">{instructions}</p>}
      {facts.length > 0 && (
        <div className="gx-facts">
          {facts.map((f, i) => (
            <span key={i}>{f}</span>
          ))}
        </div>
      )}
      {children}
      {taken ? <Tag tone="steel">Taken</Tag> : action}
    </article>
  );
}
