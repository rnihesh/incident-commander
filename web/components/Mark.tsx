/** The Incident Commander mark: a small service graph whose highlighted edge runs from the
 *  alerting service (white) to the root-cause change (green); the other two nodes are the
 *  eliminated rivals. Same drawing as app/icon.svg, also used by the Open Graph image. */
export function Mark({ size = 16, className }: { size?: number; className?: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 32 32"
      width={size}
      height={size}
      className={className}
      aria-hidden
    >
      <rect width="32" height="32" rx="7" fill="#0e141b" />
      <path d="M9 10 L23 9 L22 22 L9 23 Z" fill="none" stroke="#33465c" strokeWidth="2.2" strokeLinejoin="round" />
      <path d="M9 10 L22 22" stroke="#3ee59b" strokeWidth="2.6" strokeLinecap="round" />
      <circle cx="23" cy="9" r="2.6" fill="#526379" />
      <circle cx="9" cy="23" r="2.6" fill="#526379" />
      <circle cx="9" cy="10" r="3.2" fill="#f4f7fb" />
      <circle cx="22" cy="22" r="7" fill="none" stroke="#3ee59b" strokeOpacity="0.35" strokeWidth="1.4" />
      <circle cx="22" cy="22" r="4.6" fill="#3ee59b" />
    </svg>
  );
}
