"use client";

import { CircleAlert, LoaderCircle } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState } from "react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { supabase } from "@/lib/auth/browser";
import { authEnabled, safeNext, type SignInReason } from "@/lib/auth/config";

// What the panel says under the title, by why the user is here.
const MESSAGES: Record<SignInReason | "default", string> = {
  default: "Sign in to run agents, each with its own computer.",
  expired: "Your session ended. Sign in again to pick up where you left off.",
  "signed-out": "You're signed out. Sign in again whenever you like.",
};

// The sign-in box: why you're here, any error from the last try, and the Google button.
export function SignInPanel() {
  const params = useSearchParams();
  const reason = params.get("reason") as SignInReason | null;
  const next = safeNext(params.get("next"));
  const [redirecting, setRedirecting] = useState(false);
  const [error, setError] = useState(params.get("error"));

  async function signInWithGoogle() {
    if (!supabase) return;
    setRedirecting(true);
    setError(null);
    const callback = new URL("/auth/callback", window.location.origin);
    if (next !== "/") callback.searchParams.set("next", next);
    // On success the browser leaves for Google, so `redirecting` stays on until the page is gone.
    const { error } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: callback.toString() },
    });
    if (error) {
      setError(error.message);
      setRedirecting(false);
    }
  }

  return (
    <section className="flex w-full max-w-sm flex-col gap-4 border bg-card p-6">
      <div className="flex flex-col gap-1.5">
        <p className="font-mono text-[10.5px] tracking-[0.06em] text-muted-foreground uppercase">agentspace</p>
        <h1 className="text-lg font-bold tracking-tight">Sign in</h1>
        <p className="text-sm text-muted-foreground">
          {authEnabled ? MESSAGES[reason ?? "default"] ?? MESSAGES.default : "Sign-in is off: this app runs in local mode."}
        </p>
      </div>

      {error && (
        <Alert variant="destructive">
          <CircleAlert />
          <AlertDescription>Google sign-in didn&apos;t finish: {error} Try again.</AlertDescription>
        </Alert>
      )}

      {authEnabled ? (
        <Button variant="outline" size="lg" onClick={signInWithGoogle} disabled={redirecting} className="w-full">
          {redirecting ? <LoaderCircle className="animate-spin" strokeWidth={1.75} /> : <GoogleMark />}
          {redirecting ? "Opening Google…" : "Continue with Google"}
        </Button>
      ) : (
        <>
          <p className="text-sm text-muted-foreground">
            Set <code className="font-mono text-xs">NEXT_PUBLIC_SUPABASE_URL</code> and{" "}
            <code className="font-mono text-xs">NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY</code> to turn it on.
          </p>
          <Button asChild size="lg" className="w-full">
            <Link href="/">Open the app</Link>
          </Button>
        </>
      )}
    </section>
  );
}

// Google's "G" in its own colors, on a neutral button, as Google's brand rules ask.
function GoogleMark() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.27-4.74 3.27-8.1z" />
      <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84A11 11 0 0 0 12 23z" />
      <path fill="#FBBC05" d="M5.84 14.1a6.6 6.6 0 0 1 0-4.2V7.06H2.18a11 11 0 0 0 0 9.88l3.66-2.84z" />
      <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15A10.96 10.96 0 0 0 12 1 11 11 0 0 0 2.18 7.06l3.66 2.84C6.71 7.31 9.14 5.38 12 5.38z" />
    </svg>
  );
}
