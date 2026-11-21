import type { MetadataRoute } from "next";

import { SITE_URL } from "@/lib/site";

// Written to a plain file at build time, as a static export needs.
export const dynamic = "force-static";

// Lets every crawler in, naming the AI ones too, since being quoted by answer engines is the point (ADR-023).
export default function robots(): MetadataRoute.Robots {
  return {
    rules: [
      { userAgent: "*", allow: "/" },
      { userAgent: ["GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-SearchBot", "PerplexityBot", "Google-Extended"], allow: "/" },
    ],
    sitemap: `${SITE_URL}/sitemap.xml`,
  };
}
