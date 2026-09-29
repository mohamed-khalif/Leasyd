// Settings come from /config.json at runtime (written by the deploy script),
// so one build works in any environment.
export type Config = { region: string; userPoolId: string; clientId: string; apiBase: string; mock?: boolean };

let config: Config | null = null;

export async function loadConfig(): Promise<Config> {
  if (config) return config;
  const res = await fetch("/config.json", { cache: "no-store" });
  config = (await res.json()) as Config;
  if (import.meta.env.MODE === "mock") config.mock = true;
  return config;
}

export function getConfig(): Config {
  if (!config) throw new Error("config not loaded");
  return config;
}
