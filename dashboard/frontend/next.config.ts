import type { NextConfig } from "next";

// Remote use (supervisor's laptop through the dashboard tunnel): the browser calls /api/* on this same
// origin and Next.js passes it to the backend. The backend treats those requests as remote (login needed).
const BACKEND = (process.env.DASHBOARD_BACKEND_URL || "http://127.0.0.1:8000").replace(/\/$/, "");

const nextConfig: NextConfig = {
  reactStrictMode: true,
  devIndicators: false,
  // NEXT_DIST_DIR lets a demo build sit next to the real one without overwriting it.
  distDir: process.env.NEXT_DIST_DIR || ".next",
  // The server's Docker image runs the small standalone build; the laptop keeps `next start`.
  output: process.env.NEXT_OUTPUT === "standalone" ? "standalone" : undefined,
  // No gzip here: it buffered the live stream (/api/stream) through the proxy. Cloudflare compresses at its edge.
  compress: false,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${BACKEND}/api/:path*` }];
  },
  experimental: {
    // The live stream (/api/stream) stays open; Next's proxy cuts requests after 30 s by default.
    proxyTimeout: 12 * 3600 * 1000,
  },
};

export default nextConfig;
