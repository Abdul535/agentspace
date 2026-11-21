import { Suspense } from "react";

import { SessionSidebar } from "@/components/session-sidebar";

// The signed-in app: the session sidebar beside each page (the sign-in page has neither).
export default function AppLayout({ children }: LayoutProps<"/">) {
  return (
    <>
      {/* The sidebar reads the active session from the URL, which is only known per request. */}
      <Suspense fallback={<aside className="w-64 shrink-0 border-r bg-sidebar" />}>
        <SessionSidebar />
      </Suspense>
      <main className="min-w-0 flex-1">{children}</main>
    </>
  );
}
