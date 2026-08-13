/** @type {import('next').NextConfig} */
const API = process.env.XNIOS_API ?? "http://127.0.0.1:8000";

const nextConfig = {
  reactStrictMode: true,
  // Proxy the FastAPI service so the browser talks to one origin (no CORS in dev,
  // and the WebSocket upgrade goes through the same host).
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API}/api/:path*` }];
  },
  env: { NEXT_PUBLIC_XNIOS_API: API },
};

export default nextConfig;
