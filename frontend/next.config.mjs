/** @type {import('next').NextConfig} */
// v3.1 Lite: METASEARCH_UI_EXPORT=1 builds a static export to out/ that the API
// serves on the SAME port (no separate :3000 process). Docker keeps standalone.
const liteExport = process.env.METASEARCH_UI_EXPORT === "1";
const nextConfig = {
  output: liteExport ? "export" : "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
  env: {
    // Lite export is served by the API itself -> same-origin relative /v1/...
    // (empty string). Docker standalone on :3000 keeps the explicit origin.
    NEXT_PUBLIC_API_BASE_URL:
      process.env.NEXT_PUBLIC_API_BASE_URL ?? (liteExport ? "" : "http://localhost:8000"),
  },
};

export default nextConfig;
