import { type NextRequest, NextResponse } from "next/server";

import { authEnabled } from "@/lib/auth/config";
import { serverSupabase } from "@/lib/auth/server";

// Pages a signed-out visitor may open.
const PUBLIC_PATHS = ["/sign-in", "/auth/callback"];

// Before each page: refresh the Supabase session cookies, and send signed-out visitors to /sign-in (ADR-024).
export async function proxy(request: NextRequest) {
  if (!authEnabled) return NextResponse.next();

  let response = NextResponse.next({ request });
  const supabase = serverSupabase({
    getAll: () => request.cookies.getAll(),
    // A refreshed session goes on both the request (for this render) and the response (for the browser).
    setAll: (cookies) => {
      for (const { name, value } of cookies) request.cookies.set(name, value);
      response = NextResponse.next({ request });
      for (const { name, value, options } of cookies) response.cookies.set(name, value, options);
    },
  });
  // Checks the token against the project's public keys and refreshes it if it has expired.
  const { data } = await supabase.auth.getClaims();
  const signedIn = Boolean(data?.claims);
  const { pathname, search } = request.nextUrl;

  if (!signedIn && !PUBLIC_PATHS.includes(pathname)) {
    const url = new URL("/sign-in", request.url);
    if (pathname !== "/") url.searchParams.set("next", pathname + search);
    return NextResponse.redirect(url);
  }
  if (signedIn && pathname === "/sign-in") return NextResponse.redirect(new URL("/", request.url));
  return response;
}

export const config = {
  // Not the API (it checks its own token), Next's assets or the favicon.
  matcher: ["/((?!api|_next/static|_next/image|favicon.ico).*)"],
};
