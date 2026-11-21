import { cookies } from "next/headers";
import { type NextRequest, NextResponse } from "next/server";

import { safeNext } from "@/lib/auth/config";
import { serverSupabase } from "@/lib/auth/server";

// Google sends the user back here with a one-time code; swap it for a session cookie, then open the app.
export async function GET(request: NextRequest) {
  const { searchParams } = request.nextUrl;
  const code = searchParams.get("code");
  const next = safeNext(searchParams.get("next"));
  // Google or Supabase can send back an error instead of a code (e.g. the user pressed Cancel).
  let error = searchParams.get("error_description") ?? searchParams.get("error");

  if (code) {
    const jar = await cookies();
    const supabase = serverSupabase({
      getAll: () => jar.getAll(),
      setAll: (toSet) => toSet.forEach(({ name, value, options }) => jar.set(name, value, options)),
    });
    const result = await supabase.auth.exchangeCodeForSession(code);
    if (!result.error) return NextResponse.redirect(new URL(next, request.url));
    error = result.error.message;
  }

  const url = new URL("/sign-in", request.url);
  url.searchParams.set("error", error ?? "No sign-in code came back.");
  if (next !== "/") url.searchParams.set("next", next);
  return NextResponse.redirect(url);
}
