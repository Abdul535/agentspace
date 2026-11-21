import type { MetadataRoute } from "next";

import { SITE_URL } from "@/lib/site";

// Written to a plain file at build time, as a static export needs.
export const dynamic = "force-static";

// The list of pages for search engines; one page today, the topic subdomains get their own later.
export default function sitemap(): MetadataRoute.Sitemap {
  return [{ url: SITE_URL, lastModified: new Date(), changeFrequency: "weekly", priority: 1 }];
}
