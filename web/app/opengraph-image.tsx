import { OG_SIZE, ogImage } from "@/components/OgCard";
import { OG_ALT, SITE_NAME, SITE_TAGLINE } from "@/lib/site";

export const alt = OG_ALT;
export const size = OG_SIZE;
export const contentType = "image/png";

export default function Image() {
  return ogImage({ title: SITE_NAME, tagline: SITE_TAGLINE });
}
