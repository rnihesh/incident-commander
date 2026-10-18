// Plain-language text for the API's refusals (session caps, run caps, the spend breaker), so
// pages never show a raw reason string such as "at most 2 concurrent sessions per client".

export interface LimitNotice {
  title: string;
  /** What happened and what to do, without the sample-run suggestion. */
  detail: string;
  /** The full message for a page that offers the sample run instead. */
  body: string;
}

const SAMPLE = "Meanwhile you can watch a sample run.";

function notice(title: string, detail: string, body = `${detail} ${SAMPLE}`): LimitNotice {
  return { title, detail, body };
}

/** The notice for a refusal reason, or null when the reason is not a known limit. */
export function limitNotice(reason: string | null | undefined): LimitNotice | null {
  if (!reason) return null;
  const r = reason.toLowerCase();
  const n = /at most (\d+)/.exec(r)?.[1] ?? "the maximum number of";
  if (r.includes("concurrent sessions per client")) {
    const detail = `You already have ${n} live sandboxes open in other tabs or browsers. They expire after 30 minutes idle.`;
    return notice(
      "Your live sandboxes are all in use",
      detail,
      `You already have ${n} live sandboxes open in other tabs or browsers. They expire after 30 minutes idle; meanwhile you can watch a sample run.`,
    );
  }
  if (r.includes("new sessions per hour")) {
    return notice("Too many new sandboxes this hour", `You have opened ${n} new sandboxes in the last hour. Try again later.`);
  }
  if (r.includes("server is at capacity")) {
    return notice("The demo is busy", "Every live sandbox on the server is in use right now. Try again in a few minutes.");
  }
  if (r.includes("spend cap")) {
    return notice("Live runs are paused for today", "Today's budget for live investigations is used up.");
  }
  if (r.includes("already in progress")) {
    const detail = "Your sandbox is already running an investigation, perhaps in another tab. Wait for it to finish, then start a new one.";
    return notice("An investigation is already running", detail, detail);
  }
  const runs = /limit of (\d+) runs per hour/.exec(r)?.[1];
  if (runs) {
    return notice("Hourly limit reached", `You have started ${runs} live investigations in the last hour, the most allowed. Try again later.`);
  }
  if (r.includes("global hourly run ceiling")) {
    return notice("The demo is busy", "The demo has reached its hourly limit of live investigations. Try again later.");
  }
  if (/per (hour|day|session)/.test(r)) {
    return notice("Limit reached", `You have reached a limit on live investigations (${reason}). Try again later.`);
  }
  return null;
}
