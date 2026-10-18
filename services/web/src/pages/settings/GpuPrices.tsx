// Settings › GPU prices: the account's own prices per GPU hour (first match wins), then the defaults.
import { FormEvent, useEffect, useState } from "react";
import { fmtUnitPrice } from "../../lib/format";
import { account } from "../../api";
import { Panel } from "../../components/Panel";
import { DEFAULT_GPU_PRICES, GpuPrice } from "../../infra/model";
import { SettingsPage, SettingsProps } from "./parts";

/** Prices per GPU hour behind the GPU pages' costs: the defaults (AWS list prices) and the
 *  account's own (first match wins), which owners change. */
export function GpuPrices({ owner }: SettingsProps) {
  const [own, setOwn] = useState<GpuPrice[] | null>(null);
  const [draft, setDraft] = useState({ pattern: "", label: "", usd: "" });
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  useEffect(() => { account.gpuPrices().then((r) => setOwn(r.prices ?? []), (e: Error) => setLoadError(e.message)); }, []);
  // Edits save the whole list, so only once the current one is known (else they would overwrite it).
  const canEdit = owner && own !== null;
  const save = (next: GpuPrice[]) => account.saveGpuPrices(next).then((r) => { setOwn(r.prices); setMsg({ ok: true, text: "Saved: the GPU pages use these prices now." }); },
    (e: Error) => setMsg({ ok: false, text: e.message }));
  return (
    <SettingsPage title="GPU prices" lead="Prices per GPU hour behind the costs on the GPUs page.">
      <Panel title="Prices" flush right={<span className="faint">US$ per GPU hour</span>}>
        <table className="dtable">
          <thead><tr><th style={{ width: "34%" }}>GPU model matches</th><th>Name</th><th className="num">US$ per GPU hour</th><th>Source</th>{owner && <th />}</tr></thead>
          <tbody>
            {(own ?? []).map((p, i) => (
              <tr key={"own" + i}><td className="mono">{p.pattern}</td><td>{p.label}</td><td className="num">{fmtUnitPrice(p.usd_per_hour)}</td><td>yours</td>
                {owner && <td className="num"><button className="linkbtn danger" onClick={() => save(own!.filter((_, j) => j !== i))}>Remove</button></td>}</tr>
            ))}
            {DEFAULT_GPU_PRICES.map(([pattern, label, usd]) => (
              <tr key={pattern} className="faint"><td className="mono">{pattern}</td><td>{label}</td><td className="num">{fmtUnitPrice(usd)}</td>
                <td>default (AWS on-demand)</td>{owner && <td />}</tr>
            ))}
          </tbody>
        </table>
        {loadError && <div className="panel-foot error">Couldn't read your GPU prices: {loadError}. The GPU pages use the defaults until they can.</div>}
        {canEdit ? (
          <form className="panel-foot" onSubmit={(e: FormEvent) => {
            e.preventDefault();
            save([...(own ?? []), { pattern: draft.pattern.trim(), label: draft.label.trim() || draft.pattern.trim(), usd_per_hour: Number(draft.usd) }])
              .then(() => setDraft({ pattern: "", label: "", usd: "" }));
          }}>
            <input className="input mono" required placeholder="A100.*80" value={draft.pattern} onChange={(e) => setDraft({ ...draft, pattern: e.target.value })} aria-label="GPU model pattern" />
            <input className="input grow" placeholder="Name, e.g. A100 80GB (reserved)" value={draft.label} onChange={(e) => setDraft({ ...draft, label: e.target.value })} aria-label="Name" />
            <input className="input" required type="number" min="0.01" step="0.01" placeholder="1.50" value={draft.usd} onChange={(e) => setDraft({ ...draft, usd: e.target.value })} aria-label="US$ per GPU hour" style={{ width: 110 }} />
            <button className="btn primary">Add price</button>
          </form>
        ) : !owner ? <div className="panel-foot faint">Only owners can change prices.</div> : null}
        {msg && <div className={`panel-foot ${msg.ok ? "ok" : "error"}`}>{msg.text}</div>}
        <div className="panel-foot faint">The pattern is matched against the GPU model dcgm-exporter reports (e.g. "NVIDIA A100-SXM4-80GB"); yours come first, then the defaults.</div>
      </Panel>
    </SettingsPage>
  );
}
