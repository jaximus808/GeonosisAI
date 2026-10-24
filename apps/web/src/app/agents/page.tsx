import { Section } from '@/components/Section';
import { LiveTable } from '@/components/LiveTable';

export default function AgentsPage() {
  return (
    <>
      <h1 className="mb-6 text-2xl font-semibold tracking-tight">Agents</h1>
      <Section title="Agent sessions (live)">
        <LiveTable name="agentSession" />
      </Section>
    </>
  );
}
