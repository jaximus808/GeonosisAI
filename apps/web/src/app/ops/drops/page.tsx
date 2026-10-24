import { DropsConsole } from '@/components/tdm108/DropsConsole';
import { TextOffersPanel } from '@/components/tdm113/TextOffersPanel';

export const dynamic = 'force-dynamic';

// TDM-108: operator view of job drops (social posts of new paid tasks). The data routes need an
// operator login; the page itself says so when the session is missing.
export default function OpsDropsPage() {
  return (
    <div className="mx-auto max-w-5xl space-y-4">
      <div>
        <h1 className="mb-1 text-2xl font-semibold tracking-tight">Job drops</h1>
        <p className="text-sm text-zinc-500">
          Each new paid task gets a share card and a post per channel. Dry run writes them here and posts nothing.
        </p>
      </div>
      <DropsConsole />
      {/* TDM-113: text offers (dry-run outbox, opt-in counts) */}
      <TextOffersPanel />
    </div>
  );
}
