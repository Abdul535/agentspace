// The Supabase project the web app signs in with (ADR-024). Both values are public; unset = local mode, no login.
export const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL ?? "";
export const SUPABASE_KEY = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY ?? "";
export const authEnabled = Boolean(SUPABASE_URL && SUPABASE_KEY);

// Why the user landed on /sign-in, shown as the page's message.
export type SignInReason = "expired" | "signed-out";

// The /sign-in URL for a reason, coming back to `next` after signing in.
export function signInUrl(reason?: SignInReason, next?: string): string {
  const params = new URLSearchParams();
  if (reason) params.set("reason", reason);
  if (next && next !== "/") params.set("next", next);
  const query = params.toString();
  return query ? `/sign-in?${query}` : "/sign-in";
}

// `next` only if it is a path on this site, so a crafted link can't send the user elsewhere after sign-in.
export function safeNext(next: string | null): string {
  if (!next?.startsWith("/")) return "/";
  // Resolve it like a browser would ("//x" and "/\x" both mean another host), then keep it only if it stayed here.
  const base = "http://same.invalid";
  try {
    const url = new URL(next, base);
    const out = url.pathname + url.search + url.hash;
    // Check what we return too: "/.//evil.com" resolves here but comes out as "//evil.com", another host.
    return url.origin === base && new URL(out, base).origin === base ? out : "/";
  } catch {
    return "/";
  }
}
