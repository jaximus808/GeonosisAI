import { Placeholder, Section } from '@/components/Section';
import { RobotSummary } from '@/components/RobotSummary';

export default async function RobotPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <>
      <h1 className="mb-6 text-2xl font-semibold tracking-tight">Robot #{id}</h1>
      <Section title="Robot (live)"><RobotSummary id={id} /></Section>
      <Placeholder owner="E11 robot page">Adapter, cameras, status and run settings.</Placeholder>
    </>
  );
}
