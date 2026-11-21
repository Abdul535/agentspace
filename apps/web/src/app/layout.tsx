import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";

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

// Runs in <head> before first paint: dark is the default, so only a saved "light" choice removes the class.
const THEME_SCRIPT = `(function(){try{if(localStorage.getItem("theme")==="light")document.documentElement.classList.remove("dark")}catch(e){}})()`;

export const metadata: Metadata = {
  title: "agentspace",
  description: "Run agents, each with its own computer",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} dark h-full antialiased`}
      // The theme script may remove "dark" before React hydrates; keep what it set.
      suppressHydrationWarning
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      {/* The 3px yellow stripe across the top is the brand mark (DESIGN.md). */}
      <body className="flex h-dvh overflow-hidden border-t-3 border-t-primary">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
