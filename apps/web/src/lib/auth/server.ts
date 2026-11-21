import { type CookieOptions, createServerClient } from "@supabase/ssr";

import { SUPABASE_KEY, SUPABASE_URL } from "./config";

type CookieToSet = { name: string; value: string; options: CookieOptions };

// Where a server-side Supabase client reads and writes the session cookies.
export type CookieJar = {
  getAll: () => { name: string; value: string }[];
  setAll: (cookies: CookieToSet[]) => void;
};

// A Supabase client for the proxy and route handlers, on that request's cookies.
export function serverSupabase(jar: CookieJar) {
  return createServerClient(SUPABASE_URL, SUPABASE_KEY, { cookies: jar });
}
