import { createBrowserClient } from "@supabase/ssr";

import { authEnabled, signInUrl, SUPABASE_KEY, SUPABASE_URL } from "./config";

// The browser's Supabase client: keeps the session in cookies and refreshes the access token before it expires.
export const supabase = authEnabled ? createBrowserClient(SUPABASE_URL, SUPABASE_KEY) : null;

// The `Authorization` header for API calls; empty in local mode or when signed out (the API then answers 401).
export async function authHeaders(): Promise<Record<string, string>> {
  if (!supabase) return {};
  const { data } = await supabase.auth.getSession();
  return data.session ? { Authorization: `Bearer ${data.session.access_token}` } : {};
}

let leaving = false;

// Sends the user to sign in again, once, when the API refuses their token or the session ends.
export function onSessionEnded(reason: "expired" | "signed-out" = "expired"): void {
  if (leaving || window.location.pathname === "/sign-in") return;
  leaving = true;
  // A full page load, so no data from the old session stays in the query cache.
  window.location.assign(signInUrl(reason, window.location.pathname + window.location.search));
}

// Signs out here and in every tab, then shows the signed-out page.
export async function signOut(): Promise<void> {
  if (!supabase) return;
  leaving = true;
  await supabase.auth.signOut();
  window.location.assign(signInUrl("signed-out"));
}
