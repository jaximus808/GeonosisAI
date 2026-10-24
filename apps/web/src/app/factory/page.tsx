// TDM-112: /factory, the whole foundry floor on one screen. Every active ticket is its own lane of
// agent stations (Eval -> Diagnose -> Plan data -> Collect -> Review -> Train -> Eval -> Ship), with
// the shared Factory Droids on an overhead rail. Live from SpacetimeDB; nothing here is mocked.

import type { Metadata } from 'next';
import { FactoryLanes } from '@/components/factory/FactoryLanes';

export const metadata: Metadata = {
  title: 'Factory floor: GeonosisAI',
  description: 'Every agent in the pipeline, live: each active ticket on its own line, the Factory Droids overhead.',
};

export default function FactoryPage() {
  return <FactoryLanes />;
}
