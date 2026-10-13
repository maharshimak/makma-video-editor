/** @type {import('next').NextConfig} */
const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

const nextConfig = {
  reactStrictMode: true,
  // Proxy the API through Next so the browser sees one origin: signed media
  // URLs are same-origin, cookies stay simple, and CORS never enters the
  // picture during development.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API}/api/:path*` }];
  },
};

export default nextConfig;
