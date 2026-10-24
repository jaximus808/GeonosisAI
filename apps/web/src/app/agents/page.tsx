import { AgentsBoard, RecordingLinks } from '@/components/agents/AgentsBoard';

// TDM-76: every agent in the factory, live. ?replay=1 plays back a recorded droid run (TDM-35 replay mode).
export default async function AgentsPage({ searchParams }: { searchParams: Promise<{ replay?: string }> }) {
  const { replay } = await searchParams;
  return (
    <>
      <header className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight">Agents</h1>
        <p className="mt-1 max-w-3xl text-[15px] text-zinc-600 dark:text-zinc-400">
          Everything on this page ran without a person at the keyboard. Factory Droids write the code that touches the robot and the data; the
          orchestrator&apos;s agents plan, review and check. Steps appear here as they happen.
        </p>
      </header>
      <AgentsBoard replay={!!replay} />
      {!replay && <RecordingLinks className="mt-10" lead="Nothing running right now? You can" />}
    </>
  );
}
