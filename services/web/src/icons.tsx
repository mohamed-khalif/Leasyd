// Small line icons (16 px), drawn to match the UI's thin strokes.
const s = { width: 16, height: 16, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", strokeWidth: 1.4, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
export const IconLogo = () => (
  <svg width="22" height="22" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="var(--surface-3)" /><path d="M9 7v18h14" stroke="var(--accent)" strokeWidth="3.2" fill="none" strokeLinecap="round" strokeLinejoin="round" /></svg>
);
export const IconDash = () => <svg {...s}><rect x="2" y="2" width="5" height="5" rx="1" /><rect x="9" y="2" width="5" height="3" rx="1" /><rect x="9" y="7" width="5" height="7" rx="1" /><rect x="2" y="9" width="5" height="5" rx="1" /></svg>;
export const IconLogs = () => <svg {...s}><path d="M3 4h10M3 8h10M3 12h6" /></svg>;
export const IconTraces = () => <svg {...s}><path d="M2 3h6M5 7h7M8 11h6" /></svg>;
export const IconCost = () => <svg {...s}><circle cx="8" cy="8" r="6" /><path d="M10 5.8c-.4-.6-1.1-.9-2-.9-1.2 0-2 .6-2 1.5 0 2 4 1 4 3.1 0 .9-.9 1.6-2 1.6-1 0-1.7-.4-2.1-1M8 3.6v1.3M8 11.1v1.3" /></svg>;
export const IconClock = () => <svg {...s}><circle cx="8" cy="8" r="6" /><path d="M8 4.5V8l2.4 1.4" /></svg>;
export const IconRefresh = () => <svg {...s}><path d="M13 7.5A5 5 0 1 0 12 11M13 3.5v4h-4" /></svg>;
export const IconMoon = () => <svg {...s}><path d="M13 9.5A5.5 5.5 0 0 1 6.5 3 5.5 5.5 0 1 0 13 9.5Z" /></svg>;
export const IconSun = () => <svg {...s}><circle cx="8" cy="8" r="3" /><path d="M8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1 1M11.6 11.6l1 1M3.4 12.6l1-1M11.6 4.4l1-1" /></svg>;
export const IconOut = () => <svg {...s}><path d="M6 3H3v10h3M10 5l3 3-3 3M13 8H6" /></svg>;
