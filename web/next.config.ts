import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* config options here */
  // No floating "N" dev badge: it sits over the console in screenshots and captures.
  devIndicators: false,
  cacheComponents: true,
  partialPrefetching: true,
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
