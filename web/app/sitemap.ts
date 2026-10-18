import type { MetadataRoute } from "next";
import { SITE_URL } from "@/lib/site";

export default function sitemap(): MetadataRoute.Sitemap {
  return [
    { url: `${SITE_URL}/`, changeFrequency: "weekly", priority: 1 },
    { url: `${SITE_URL}/benchmark`, changeFrequency: "weekly", priority: 0.8 },
    { url: `${SITE_URL}/judge`, changeFrequency: "weekly", priority: 0.8 },
  ];
}
