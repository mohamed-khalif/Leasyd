// The Settings menu: its groups and pages (#/settings/<id>). Kept apart from the pages so the app's
// header can name the page without loading them.
export const SETTINGS_MENU = [
  { group: "Account", pages: [{ id: "profile", label: "Profile" }, { id: "security", label: "Security" }] },
  { group: "Organization", pages: [
    { id: "organization", label: "Organization" }, { id: "team", label: "Team" }, { id: "usage", label: "Usage & cost" },
    { id: "billing", label: "Billing" }, { id: "api-keys", label: "API keys" }, { id: "gpu-prices", label: "GPU prices" },
    { id: "ai-tools", label: "AI tools" },
  ] },
] as const;
export type SettingsId = (typeof SETTINGS_MENU)[number]["pages"][number]["id"];
const PAGES = SETTINGS_MENU.flatMap((g) => g.pages as readonly { id: SettingsId; label: string }[]);

/** The page a path shows: #/settings/<id>; a return from Stripe to #/settings?billing=… is Billing. */
export function settingsPage(path: string, hash = window.location.hash): { id: SettingsId; label: string } {
  const id = path.split("/")[2] || (/[?&]billing=/.test(hash) ? "billing" : "profile");
  return PAGES.find((p) => p.id === id) ?? PAGES[0];
}
