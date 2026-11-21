import type { NextConfig } from "next";

// The landing page is plain files (ADR-023): `next build` renders every route to HTML in `out/`, so any static host can serve it.
const nextConfig: NextConfig = {
  output: "export",
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
