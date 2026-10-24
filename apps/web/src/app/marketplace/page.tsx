import { headers } from 'next/headers';
import { Section } from '@/components/Section';
import { ContributorPresence } from '@/components/ContributorPresence';
import { ContributorsPanel } from '@/components/ContributorsPanel';
import { MarketplaceView } from '@/components/marketplace/MarketplaceView';
import { PushOptIn } from '@/components/tdm65/PushOptIn';
import { TaskDeepLink } from '@/components/tdm65/TaskDeepLink';
import { Tdm108ClaimSource } from '@/components/tdm108/ClaimSource';

export default async function MarketplacePage() {
  // TDM-108: the access gate says `task:<ids>` when this visitor came in on a job-drop invite (that job only)
  const access = (await headers()).get('x-rdf-access') ?? '';
  const onlyTasks = access.startsWith('task:') ? access.slice(5).split('-').filter((x) => /^\d{1,19}$/.test(x)) : undefined;
  return (
    <>
      <div className="mx-auto max-w-xl">
        <ContributorPresence page="marketplace" />
        {/* TDM-65: paid-task push alerts (opt-in) + landing from a notification tap. */}
        <PushOptIn className="mb-4" />
        <TaskDeepLink />
        {/* TDM-108: job-drop attribution (?src= from a social post -> logged with the claim). */}
        <Tdm108ClaimSource />
      </div>
      {/* TDM-61: open paid tasks, claim, held-claim lease, my tasks. */}
      {onlyTasks && (
        <p className="mx-auto mb-3 max-w-xl text-sm text-zinc-500">
          This link opens task #{onlyTasks.join(', #')} only. The site-wide code opens every task.
        </p>
      )}
      <MarketplaceView onlyTasks={onlyTasks} />
      <div className="mx-auto mt-10 max-w-xl">
        <Section title="Contributors (live)"><ContributorsPanel /></Section>
      </div>
    </>
  );
}
