// Public SpacetimeDB connection config for the browser. Only NEXT_PUBLIC_* values,
// never SPACETIME_TOKEN (the database owner token): the browser connects with its own
// anonymous identity, whose token the server issues on first connect.
//
// NEXT_PUBLIC_SPACETIME_HOST=same-origin (make demo-public): the browser talks to SpacetimeDB on the page's
// own origin (the public edge routes /v1/* to it, scripts/demo/public_guard.py), so one tunnel serves both
// and the build does not depend on the tunnel URL. Server code then uses RDF_SPACETIME_SERVER_HOST.
const RAW_HOST = process.env.NEXT_PUBLIC_SPACETIME_HOST || 'https://maincloud.spacetimedb.com';
export const SPACETIME_SAME_ORIGIN = RAW_HOST === 'same-origin';
export const SPACETIME_HOST = !SPACETIME_SAME_ORIGIN
  ? RAW_HOST
  : typeof window !== 'undefined'
    ? window.location.origin
    : process.env.RDF_SPACETIME_SERVER_HOST || process.env.SPACETIME_HOST || 'http://127.0.0.1:3000';
export const SPACETIME_DB_NAME =
  process.env.NEXT_PUBLIC_SPACETIME_DB_NAME || 'robo-factory-a706j';

// localStorage key for this browser's own identity token (not the owner token).
export const BROWSER_TOKEN_KEY = `rdf.spacetime.token.${SPACETIME_DB_NAME}`;
