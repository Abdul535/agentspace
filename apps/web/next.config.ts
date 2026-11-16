import type { NextConfig } from "next";

// The browser calls the FastAPI backend at /api/* on this origin, so no CORS is needed.
const apiUrl = process.env.API_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  rewrites: async () => [{ source: "/api/:path*", destination: `${apiUrl}/:path*` }],
  cacheComponents: true,
  partialPrefetching: true,
  reactCompiler: true,
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
