import type { Metadata } from 'next';
import { DeploymentView } from '@/components/deployment/DeploymentView';

// A shipped ticket's finished unit: the spec sheet (proof, policy, endpoint) and "Run my policy" on the sim robot.
export async function generateMetadata({ params }: { params: Promise<{ id: string }> }): Promise<Metadata> {
  const { id } = await params;
  return { title: `Deployment, TKT-${id.padStart(4, '0')}` };
}

export default async function DeploymentPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!/^\d{1,18}$/.test(id)) return <p className="gx-small">Not a ticket id: {id}</p>;
  return <DeploymentView id={id} />;
}
