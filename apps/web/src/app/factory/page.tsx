// TDM-115: /factory, the foundry floor as a physical place: one isometric cell per robot, each agent a
// droid at its machine saying what it is doing. TDM-112's lanes stay one click away ("List view").
// Live from SpacetimeDB; nothing here is mocked.

import type { Metadata } from 'next';
import { FactoryIsoPage } from '@/components/factory-iso/FactoryIsoPage';

export const metadata: Metadata = {
  title: 'Factory floor: GeonosisAI',
  description: 'Every robot cell, live: the arm, the machines around it and each agent saying what it is doing.',
};

export default function FactoryPage() {
  return <FactoryIsoPage />;
}
