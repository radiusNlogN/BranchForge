/**
 * A tiny hash router.
 *
 * Two routes: the run list at `#/` and one run at `#/runs/:runId`.
 *
 * Hash rather than the History API, deliberately. A path like `/runs/abc` has to
 * be served `index.html` by whatever is hosting the build, and nothing here does
 * that: the browser tier serves `dist/` with `python -m http.server`, which
 * returns 404 for any path that is not a real file, and there is no deployment
 * config in the repository at all. A hash never reaches the server, so deep
 * links, reloads, and Back/Forward work under the dev server, `vite preview`,
 * the test fixture, and any static host — with no rewrite rule and no new
 * dependency.
 *
 * Back/Forward come free: assigning `location.hash` pushes a history entry, and
 * the browser fires `hashchange` when the user navigates between them.
 */

import { useEffect, useState } from "react";

export type Route = { name: "home" } | { name: "run"; runId: string };

const RUN_PREFIX = "#/runs/";

/** The route a hash denotes. Anything unrecognised is the home route. */
export function parseHash(hash: string): Route {
  if (hash.startsWith(RUN_PREFIX)) {
    // A run id is a server-generated UUID; it needs decoding because it went
    // through `encodeURIComponent` on the way out.
    const raw = hash.slice(RUN_PREFIX.length);
    const runId = decodeURIComponent(raw).trim();
    if (runId !== "") return { name: "run", runId };
  }
  return { name: "home" };
}

export function hrefForRun(runId: string): string {
  return `${RUN_PREFIX}${encodeURIComponent(runId)}`;
}

export const HOME_HREF = "#/";

/**
 * Navigate, pushing a history entry.
 *
 * Assigning the same hash twice is a no-op in every browser, so a repeated click
 * cannot stack duplicate entries and make Back appear broken.
 */
export function navigate(href: string): void {
  if (window.location.hash !== href) {
    window.location.hash = href;
  }
}

/** The current route, re-rendering on Back/Forward and on any hash change. */
export function useHashRoute(): Route {
  const [hash, setHash] = useState(() => window.location.hash);

  useEffect(() => {
    const onChange = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onChange);
    // The hash can change between first render and subscribing — read it again
    // so a deep link opened directly is never missed.
    onChange();
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  return parseHash(hash);
}
