import type { Metadata } from "next";
import { Suspense } from "react";

import { SignInPanel } from "./sign-in-panel";

export const metadata: Metadata = { title: "Sign in · agentspace" };

// The only page a signed-out visitor sees (ADR-024); the panel reads the reason from the URL.
export default function SignInPage() {
  return (
    <main className="flex flex-1 items-center justify-center p-4">
      <Suspense fallback={<div className="h-56 w-full max-w-sm border bg-card" />}>
        <SignInPanel />
      </Suspense>
    </main>
  );
}
