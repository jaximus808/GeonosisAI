import type { Metadata } from 'next';
import type { ReactNode } from 'react';
import './globals.css';
import { SpacetimeProvider } from '@/lib/spacetime/provider';
import { Header } from '@/components/Header';

export const metadata: Metadata = {
  title: 'Robot Deployment Factory',
  description: 'Plug in your robot, tell it the job, and the factory gets it deployed.',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <SpacetimeProvider>
          <Header />
          <main className="mx-auto max-w-6xl px-4 py-6">{children}</main>
        </SpacetimeProvider>
      </body>
    </html>
  );
}
