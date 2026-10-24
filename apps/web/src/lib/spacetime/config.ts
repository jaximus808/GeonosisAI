// Public SpacetimeDB connection config for the browser. Only NEXT_PUBLIC_* values,
// never SPACETIME_TOKEN (the database owner token): the browser connects with its own
// anonymous identity, whose token the server issues on first connect.
export const SPACETIME_HOST =
  process.env.NEXT_PUBLIC_SPACETIME_HOST || 'https://maincloud.spacetimedb.com';
export const SPACETIME_DB_NAME =
  process.env.NEXT_PUBLIC_SPACETIME_DB_NAME || 'robo-factory-a706j';

// localStorage key for this browser's own identity token (not the owner token).
export const BROWSER_TOKEN_KEY = `rdf.spacetime.token.${SPACETIME_DB_NAME}`;
