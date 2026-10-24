import type { NextConfig } from 'next';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

const nextConfig: NextConfig = {
  // Lets a second build (e.g. pointed at another database) live next to `next dev`.
  distDir: process.env.NEXT_DIST_DIR || '.next',
  // pnpm workspace root, so Turbopack resolves hoisted deps from the monorepo.
  turbopack: { root: resolve(fileURLToPath(new URL('.', import.meta.url)), '../..') },
  // TDM-60: phones open the dev server by LAN IP or a tunnel (the /join QR), and
  // Next blocks dev assets for any other origin (the page renders but never
  // hydrates, so the SpacetimeDB client never connects; 127.0.0.1 included).
  // Loopback, private LAN ranges plus
  // RDF_DEV_ORIGINS (comma-separated hostnames, e.g. "*.ngrok-free.app").
  allowedDevOrigins: [
    '127.0.0.1',
    '192.168.*.*',
    '10.*.*.*',
    '172.*.*.*',
    '*.local',
    ...(process.env.RDF_DEV_ORIGINS ?? '').split(',').map((s) => s.trim()).filter(Boolean),
  ],
};

export default nextConfig;
