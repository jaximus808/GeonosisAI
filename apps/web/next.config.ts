import type { NextConfig } from 'next';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

const nextConfig: NextConfig = {
  // Lets a second build (e.g. pointed at another database) live next to `next dev`.
  distDir: process.env.NEXT_DIST_DIR || '.next',
  // pnpm workspace root, so Turbopack resolves hoisted deps from the monorepo.
  turbopack: { root: resolve(fileURLToPath(new URL('.', import.meta.url)), '../..') },
};

export default nextConfig;
