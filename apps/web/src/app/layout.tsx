import type { Metadata, Viewport } from 'next';
import type { ReactNode } from 'react';
import { Big_Shoulders, Big_Shoulders_Stencil, IBM_Plex_Mono, IBM_Plex_Sans } from 'next/font/google';
import './globals.css';
import { SpacetimeProvider } from '@/lib/spacetime/provider';
import { Header } from '@/components/Header';
import { GX_THEME_SCRIPT } from '@/components/brand/ThemeToggle';

// TDM-109: GeonosisAI type. Stencil for the wordmark only, Big Shoulders for titles and stations,
// Plex Sans for reading, Plex Mono for every number a robot or ledger produced.
const stencil = Big_Shoulders_Stencil({ subsets: ['latin'], weight: ['800'], variable: '--gx-font-stencil', display: 'swap' });
const display = Big_Shoulders({ subsets: ['latin'], weight: ['600', '700', '800'], variable: '--gx-font-display', display: 'swap' });
const sans = IBM_Plex_Sans({ subsets: ['latin'], weight: ['400', '500', '600'], variable: '--gx-font-sans', display: 'swap' });
const mono = IBM_Plex_Mono({ subsets: ['latin'], weight: ['400', '500'], variable: '--gx-font-mono', display: 'swap' });

export const metadata: Metadata = {
  title: 'GeonosisAI',
  description: 'The droid foundry for real robots. Ticket in. Deployed robot out.',
  applicationName: 'GeonosisAI',
};

export const viewport: Viewport = {
  themeColor: '#13110f',
  colorScheme: 'dark light',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${stencil.variable} ${display.variable} ${sans.variable} ${mono.variable}`} suppressHydrationWarning>
      <head>
        {/* Foundry is the default; apply a remembered Dune choice before first paint. */}
        <script dangerouslySetInnerHTML={{ __html: GX_THEME_SCRIPT }} />
      </head>
      <body>
        <SpacetimeProvider>
          <Header />
          <main className="mx-auto max-w-[1120px] px-4 py-6 sm:px-8">{children}</main>
        </SpacetimeProvider>
      </body>
    </html>
  );
}
