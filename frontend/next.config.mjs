/** @type {import('next').NextConfig} */
const nextConfig = {
  // Expose the backend base URL to the browser; defaults to local API.
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000",
  },
};

export default nextConfig;
