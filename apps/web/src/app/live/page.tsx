// TDM-101: /live, the projector page for the pitch. Full-screen kiosk over the normal page chrome:
// the robot (RobotView with its Sim | Live switch), the ticket on the line, the agents at work, and
// the join QR for judges' phones. /live?ticket=N pins a ticket; otherwise it follows the newest
// active one. Keys: S / L sim or live, F full screen, 1-3 the 3D camera presets.
//
// GeonosisAI brand (docs/brand/project), Foundry theme. The fonts are exposed under the brand's
// variable names (--font-stencil / -display / -sans / -mono), so this page keeps working unchanged
// when TDM-109 declares them app-wide.

import type { Metadata } from 'next';
import { Big_Shoulders, Big_Shoulders_Stencil, IBM_Plex_Mono, IBM_Plex_Sans } from 'next/font/google';
import { LiveKiosk } from './LiveKiosk';

const stencil = Big_Shoulders_Stencil({ subsets: ['latin'], weight: ['800'], variable: '--font-stencil' });
const display = Big_Shoulders({ subsets: ['latin'], weight: ['600', '700', '800'], variable: '--font-display' });
const sans = IBM_Plex_Sans({ subsets: ['latin'], weight: ['400', '500', '600'], variable: '--font-sans' });
const mono = IBM_Plex_Mono({ subsets: ['latin'], weight: ['400', '500'], variable: '--font-mono' });

export const metadata: Metadata = {
  title: 'Live: GeonosisAI',
  description: 'The foundry floor, live: the robot, the ticket on the line and the Factory Droids.',
};

export default async function LivePage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const sp = await searchParams;
  const ticket = Array.isArray(sp.ticket) ? sp.ticket[0] : sp.ticket;
  return (
    <div className={`${stencil.variable} ${display.variable} ${sans.variable} ${mono.variable}`}>
      <LiveKiosk ticketParam={ticket ?? null} />
    </div>
  );
}
