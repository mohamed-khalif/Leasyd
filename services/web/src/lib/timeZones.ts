// Time-zone arithmetic (not display; see lib/format.ts for that).

/** The viewer's IANA time zone, e.g. "Europe/London". */
export const LOCAL_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

/** The time zones the browser knows (empty where it can't list them). */
export const timeZones = (): string[] =>
  (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.("timeZone") ?? [];

/** The weekday ("mon"…"sun") and minute of the day of moment `t` in time zone `tz`. */
export function zonedDayMinute(t: Date, tz: string): { day: string; minute: number } {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-GB", { timeZone: tz, weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
    .formatToParts(t).map((x) => [x.type, x.value]));
  return { day: String(p.weekday).toLowerCase().slice(0, 3), minute: Number(p.hour) * 60 + Number(p.minute) };
}
