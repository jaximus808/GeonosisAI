import { Section } from '@/components/Section';
import { ContributorPresence } from '@/components/ContributorPresence';
import { ContributorsPanel } from '@/components/ContributorsPanel';
import { MarketplaceView } from '@/components/marketplace/MarketplaceView';
import { PushOptIn } from '@/components/tdm65/PushOptIn';
import { TaskDeepLink } from '@/components/tdm65/TaskDeepLink';

export default function MarketplacePage() {
  return (
    <>
      <div className="mx-auto max-w-xl">
        <ContributorPresence page="marketplace" />
        {/* TDM-65: paid-task push alerts (opt-in) + landing from a notification tap. */}
        <PushOptIn className="mb-4" />
        <TaskDeepLink />
      </div>
      {/* TDM-61: open paid tasks, claim, held-claim lease, my tasks. */}
      <MarketplaceView />
      <div className="mx-auto mt-10 max-w-xl">
        <Section title="Contributors (live)"><ContributorsPanel /></Section>
      </div>
    </>
  );
}
