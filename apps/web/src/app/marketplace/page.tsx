import { Placeholder, Section } from '@/components/Section';
import { ContributorPresence } from '@/components/ContributorPresence';
import { ContributorsPanel } from '@/components/ContributorsPanel';

export default function MarketplacePage() {
  return (
    <>
      <h1 className="mb-6 text-2xl font-semibold tracking-tight">Tasks</h1>
      <ContributorPresence page="marketplace" />
      <Placeholder owner="E07 marketplace">Paid contributor tasks (takeover, teleop, reset).</Placeholder>
      <div className="mt-8">
        <Section title="Contributors (live)"><ContributorsPanel /></Section>
      </div>
    </>
  );
}
