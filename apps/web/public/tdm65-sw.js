/* TDM-65: service worker for paid-task push notifications.
 *
 * The orchestrator (services/orchestrator/src/rdf_orchestrator/paging.py) sends a Web Push with a
 * JSON payload {title, body, url, tag, task_ids, ...} when the factory creates a paid task. This
 * worker shows it even when the marketplace page is closed or in the background, and a tap opens
 * (or focuses) the marketplace at /marketplace?task=<id>, which scrolls to the task to claim it.
 *
 * No caching / offline logic here on purpose: the app needs a live SpacetimeDB connection anyway.
 */
'use strict';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: 'New paid robot task', body: event.data ? event.data.text() : '' };
  }
  const title = data.title || 'New paid robot task';
  const options = {
    body: data.body || 'Open the marketplace to claim it.',
    tag: data.tag || 'rdf-task',
    renotify: true,
    requireInteraction: !data.test,
    icon: '/tdm65/icon-192.png',
    badge: '/tdm65/badge-96.png',
    vibrate: [120, 60, 120, 60, 240],
    timestamp: Date.now(),
    data: { url: data.url || '/marketplace', taskIds: data.task_ids || [] },
    actions: data.test ? [] : [{ action: 'claim', title: 'Open to claim' }],
  };
  event.waitUntil(
    (async () => {
      await self.registration.showNotification(title, options);
      // Tell any open marketplace tab too (it already highlights new tasks live; this is a nudge).
      const tabs = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
      for (const c of tabs) c.postMessage({ type: 'tdm65-push', payload: data });
    })(),
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = new URL((event.notification.data && event.notification.data.url) || '/marketplace', self.location.origin);
  event.waitUntil(
    (async () => {
      const tabs = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
      // Prefer a tab of this app that is already open: focus it and navigate to the task.
      for (const c of tabs) {
        if (new URL(c.url).origin !== target.origin) continue;
        try {
          await c.focus();
          if ('navigate' in c) return await c.navigate(target.href);
          c.postMessage({ type: 'tdm65-open', url: target.href });
          return;
        } catch (e) {
          // fall through to openWindow
        }
      }
      await self.clients.openWindow(target.href);
    })(),
  );
});

// The browser rotated the subscription (rare): the page re-subscribes on its next open
// (PushOptIn compares the live subscription with the server's state), so nothing to do here.
self.addEventListener('pushsubscriptionchange', () => {});
