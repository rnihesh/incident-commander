import Link from "next/link";
import type { ReactNode } from "react";
import { Mark } from "./Mark";

/** Every page's top bar: brand / title on the left, page status beside it, page actions and
 *  navigation on the right. One row, one height, so the pages read as one product. */
export function PageHeader({
  title,
  status,
  right,
  sticky = false,
  titleAs: Title = "h1",
  children,
}: {
  title: ReactNode;
  /** "span" when the page body carries its own h1 (the proof page). */
  titleAs?: "h1" | "span";
  /** Badges or a one-line description that belongs with the title. */
  status?: ReactNode;
  right?: ReactNode;
  sticky?: boolean;
  /** Extra rows under the title row (the console's control row). */
  children?: ReactNode;
}) {
  return (
    <header className={`border-b border-rule bg-deck ${sticky ? "sticky top-0 z-10" : ""}`}>
      <div className="flex min-h-12 flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2 sm:px-5">
        <div className="flex min-w-0 items-baseline gap-2.5">
          <Link href="/" className="shrink-0 font-mono text-label font-medium text-dim hover:text-ink">
            <Mark size={16} className="mr-2 inline-block align-[-3px]" />
            incident-commander
          </Link>
          <span className="text-faint" aria-hidden>/</span>
          <Title className="min-w-0 break-words text-title font-semibold tracking-tight text-ink sm:truncate">{title}</Title>
        </div>
        {status && <div className="flex min-w-0 items-center gap-2">{status}</div>}
        {right && <div className="ml-auto flex max-w-full flex-wrap items-center justify-end gap-2">{right}</div>}
      </div>
      {children}
    </header>
  );
}

const NAV = [
  { href: "/", label: "Console", key: "console" },
  { href: "/judge", label: "Judge mode", key: "judge" },
  { href: "/benchmark", label: "Benchmark", key: "benchmark" },
] as const;

/** Links to the other pages; the current page is omitted. */
export function PageNav({ current }: { current?: (typeof NAV)[number]["key"] }) {
  return (
    <nav aria-label="Pages" className="flex items-center">
      {NAV.filter((n) => n.key !== current).map((n) => (
        <Link
          key={n.key}
          href={n.href}
          className="inline-flex h-7 items-center rounded-ctl px-2.5 text-label font-medium text-dim hover:bg-raise hover:text-ink"
        >
          {n.label}
        </Link>
      ))}
    </nav>
  );
}

/** Suspense / data-loading placeholder for a whole page. */
export function PageLoading({ label }: { label: string }) {
  return (
    <div className="grid h-screen place-items-center bg-void" role="status">
      <p className="flex items-center gap-2.5 text-label text-faint">
        <span className="size-1.5 animate-pulse rounded-full bg-symptom motion-reduce:animate-none" aria-hidden />
        {label}
      </p>
    </div>
  );
}

/* Run-page layout (console, storm, judge run). Below lg the page scrolls as one column: graph,
 * funnel, then the evidence column. From lg up the two columns sit side by side and each
 * scrolls on its own inside a full-height grid. */
export const RUN_MAIN = "flex flex-col lg:grid lg:min-h-0 lg:grid-cols-[minmax(0,1fr)_clamp(400px,36vw,640px)]";
export const RUN_LEFT = "flex flex-col gap-gutter p-gutter lg:min-h-0 lg:overflow-y-auto";
export const RUN_RIGHT = "flex flex-col border-t border-rule bg-deck lg:min-h-0 lg:overflow-y-auto lg:border-l lg:border-t-0";
/** The graph panel: canvas beside the causal-path list from sm, the list under it on phones. */
export const GRAPH_PANEL = "grid shrink-0 grid-cols-1 sm:min-h-[400px] sm:grid-cols-[minmax(0,1fr)_224px] lg:min-h-[380px] lg:flex-1 2xl:grid-cols-[minmax(0,1fr)_260px]";
/** The canvas box inside the graph panel: a fixed, usable height on phones, the panel's height above. */
export const GRAPH_CELL = "relative h-[min(60vh,420px)] min-h-[280px] min-w-0 sm:h-auto sm:min-h-0 sm:flex-1";
