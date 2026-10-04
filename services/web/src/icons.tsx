// Small line icons (16 px), drawn to match the UI's thin strokes.
const s = { width: 16, height: 16, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", strokeWidth: 1.4, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
export const IconLogo = () => (
  <svg width="22" height="22" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="var(--surface-3)" /><path d="M9 7v18h14" stroke="var(--accent)" strokeWidth="3.2" fill="none" strokeLinecap="round" strokeLinejoin="round" /></svg>
);
export const IconDash = () => <svg {...s}><rect x="2" y="2" width="5" height="5" rx="1" /><rect x="9" y="2" width="5" height="3" rx="1" /><rect x="9" y="7" width="5" height="7" rx="1" /><rect x="2" y="9" width="5" height="5" rx="1" /></svg>;
export const IconLogs = () => <svg {...s}><path d="M3 4h10M3 8h10M3 12h6" /></svg>;
export const IconMetrics = () => <svg {...s}><path d="M2 13l3.5-4.5 3 2.5L14 4" /><path d="M2 2v12h12" /></svg>;
export const IconTarget = () => <svg {...s}><circle cx="8" cy="8" r="6" /><circle cx="8" cy="8" r="3" /><circle cx="8" cy="8" r=".6" /></svg>;
export const IconBell = () => <svg {...s}><path d="M4 11V7a4 4 0 0 1 8 0v4l1.5 1.5h-11L4 11zM6.5 14h3" /></svg>;
export const IconPulse = () => <svg {...s}><path d="M1.5 8h3l1.5-4 3 8 1.5-4h4" /></svg>;
export const IconTraces =() => <svg {...s}><path d="M2 3h6M5 7h7M8 11h6" /></svg>;
export const IconCost = () => <svg {...s}><circle cx="8" cy="8" r="6" /><path d="M10 5.8c-.4-.6-1.1-.9-2-.9-1.2 0-2 .6-2 1.5 0 2 4 1 4 3.1 0 .9-.9 1.6-2 1.6-1 0-1.7-.4-2.1-1M8 3.6v1.3M8 11.1v1.3" /></svg>;
export const IconClock = () => <svg {...s}><circle cx="8" cy="8" r="6" /><path d="M8 4.5V8l2.4 1.4" /></svg>;
export const IconRefresh = () => <svg {...s}><path d="M13 7.5A5 5 0 1 0 12 11M13 3.5v4h-4" /></svg>;
export const IconMoon = () => <svg {...s}><path d="M13 9.5A5.5 5.5 0 0 1 6.5 3 5.5 5.5 0 1 0 13 9.5Z" /></svg>;
export const IconSun = () => <svg {...s}><circle cx="8" cy="8" r="3" /><path d="M8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1 1M11.6 11.6l1 1M3.4 12.6l1-1M11.6 4.4l1-1" /></svg>;
export const IconOut = () => <svg {...s}><path d="M6 3H3v10h3M10 5l3 3-3 3M13 8H6" /></svg>;
export const IconFlask = () => <svg {...s}><path d="M6 2h4M6.5 2v4L3 13a1 1 0 0 0 .9 1.4h8.2A1 1 0 0 0 13 13L9.5 6V2M4.7 10h6.6" /></svg>;
export const IconDb = () => <svg {...s}><ellipse cx="8" cy="4" rx="5" ry="2" /><path d="M3 4v8c0 1.1 2.2 2 5 2s5-.9 5-2V4M3 8c0 1.1 2.2 2 5 2s5-.9 5-2" /></svg>;
export const IconGear = () => <svg {...s}><circle cx="8" cy="8" r="2.2" /><path d="M8 1.8v1.6M8 12.6v1.6M1.8 8h1.6M12.6 8h1.6M3.6 3.6l1.1 1.1M11.3 11.3l1.1 1.1M3.6 12.4l1.1-1.1M11.3 4.7l1.1-1.1" /></svg>;
export const IconSpark = () => <svg {...s}><path d="M8 1.8l1.5 4.2 4.2 1.5-4.2 1.5L8 13.2 6.5 9 2.3 7.5 6.5 6z" /><path d="M13 11.5l.6 1.4 1.4.6-1.4.6-.6 1.4-.6-1.4-1.4-.6 1.4-.6z" /></svg>;
export const IconMap = () => <svg {...s}><circle cx="3.5" cy="8" r="1.8" /><circle cx="12.5" cy="3.5" r="1.8" /><circle cx="12.5" cy="12.5" r="1.8" /><path d="M5.2 7.2 10.8 4.3M5.2 8.8l5.6 2.9" /></svg>;
export const IconLambda = () => <svg {...s}><path d="M3.5 2.5h2.6l6.4 11h-2.6L7.6 9.1 5.2 13.5H2.8l3.5-6.4L4.6 4.1H3.5z" /></svg>;
