import Link from "next/link";
import type { LimitNotice as Notice } from "../lib/limits";
import { buttonClass } from "./ui";

/** Where "Watch a sample run" goes: the console, playing the recorded run on request. */
export const SAMPLE_RUN_HREF = "/?sample=1";

/** One friendly message in place of a page the API refused to start (session or run limits). */
export function LimitNotice({ notice, onRetry }: { notice: Notice; onRetry?: () => void }) {
  return (
    <div role="status" className="mx-auto w-full max-w-xl px-6 py-16 sm:py-24">
      <h2 className="text-title font-semibold text-ink">{notice.title}</h2>
      <p className="mt-2 text-body leading-relaxed text-dim">{notice.body}</p>
      <div className="mt-5 flex flex-wrap items-center gap-2">
        <Link href={SAMPLE_RUN_HREF} className={buttonClass("primary")}>
          Watch a sample run
        </Link>
        {onRetry && (
          <button type="button" onClick={onRetry} className={buttonClass("secondary")}>
            Try again
          </button>
        )}
      </div>
    </div>
  );
}
