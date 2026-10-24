import type { MetadataRoute } from 'next';

// TDM-65: installable web app, so contributors get paid-task push notifications.
// iOS Safari only delivers Web Push to a home-screen app (iOS 16.4+): Share -> Add to Home Screen,
// then open it from the home screen and turn notifications on. Android Chrome and desktop browsers
// get pushes from the normal tab. Start URL is the marketplace (the claim page).
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: 'GeonosisAI: paid tasks',
    short_name: 'GeonosisAI',
    description: 'Paid robot resets, takeovers and teleop demos. Get notified when the foundry posts one.',
    id: '/marketplace',
    start_url: '/marketplace?src=pwa',
    scope: '/',
    display: 'standalone',
    orientation: 'portrait',
    background_color: '#13110f',
    theme_color: '#13110f',
    icons: [
      { src: '/tdm65/icon-192.png', sizes: '192x192', type: 'image/png', purpose: 'any' },
      { src: '/tdm65/icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'any' },
      { src: '/tdm65/icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
    ],
  };
}
