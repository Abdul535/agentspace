import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { Suspense } from "react";

import { SessionSidebar } from "@/components/session-sidebar";

import "./globals.css";
import { Providers } from "./providers";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "agentspace",
  description: "Run agents, each with its own computer",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="flex h-dvh overflow-hidden">
        <Providers>
          {/* The sidebar reads the active session from the URL, which is only known per request. */}
          <Suspense fallback={<aside className="w-64 shrink-0 border-r bg-muted/30" />}>
            <SessionSidebar />
          </Suspense>
          <main className="min-w-0 flex-1">{children}</main>
        </Providers>
      </body>
    </html>
  );
}
