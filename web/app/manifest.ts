import type { MetadataRoute } from "next";
import { COLOR_VOID, SITE_NAME, SITE_TAGLINE } from "@/lib/site";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: SITE_NAME,
    short_name: "Commander",
    description: SITE_TAGLINE,
    start_url: "/",
    display: "standalone",
    background_color: COLOR_VOID,
    theme_color: COLOR_VOID,
    icons: [
      { src: "/icon.svg", sizes: "any", type: "image/svg+xml" },
      { src: "/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/icon-512.png", sizes: "512x512", type: "image/png" },
    ],
  };
}
