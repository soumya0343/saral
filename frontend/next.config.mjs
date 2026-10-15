/** @type {import('next').NextConfig} */
const nextConfig = {
  // STATIC_EXPORT=1 builds plain files into .next-export/ (the Hugging Face Space serves them next
  // to the API, which it mounts at /api). Every page is a client component, so nothing is lost.
  // A separate dir also keeps a static build from clobbering a running `next dev`.
  ...(process.env.STATIC_EXPORT ? { output: "export", distDir: ".next-export" } : {}),
  // Expose the backend base URL to the browser; defaults to local API.
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000",
  },
};

export default nextConfig;
