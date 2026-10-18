// Settings › API keys (create with a name, list, revoke; a new key shown once) and AI tools (MCP).
import { useState } from "react";
import { account, KeyScope, rumSnippet } from "../../api";
import { getConfig } from "../../config";
import { Field, ownersOnly, Section, SettingsPage, SettingsProps } from "./parts";

const SCOPES = { ingest: "Send data", read: "Read data (API)", browser: "Web monitoring (browser)" } as const;
const DOCS = "https://www.leasyd.com/docs";
const endpoint = () => getConfig().ingestUrl || "https://ingest.leasyd.com";

const mcpCommand = (key: string) => `claude mcp add --transport http leasyd ${endpoint()}/v1/mcp \\
  --header "x-api-key: ${key}"`;

type NewKey = { key_id: string; name?: string; scope: string; api_key: string; origins?: string[] };

export function ApiKeys({ acc, owner, busy, act, setNote }: SettingsProps) {
  const [newKey, setNewKey] = useState<NewKey | null>(null);
  const [scope, setScope] = useState<KeyScope>("ingest");
  const [name, setName] = useState("");
  const [origins, setOrigins] = useState("");
  const create = () => act(async () => {
    setNewKey(await account.createKey(scope, scope === "browser" ? origins.split(/[\s,]+/).filter(Boolean) : undefined, name));
    setName("");
  });

  return (
    <SettingsPage title="API keys">
      <Section title="Create a key" about={<>
        Give each app or script its own key, so you can revoke one without touching the others.
        <ul className="set-links">
          <li><a href={`${DOCS}#otlp`} target="_blank" rel="noreferrer">Send data with OpenTelemetry</a></li>
          <li><a href={`${DOCS}#query-api`} target="_blank" rel="noreferrer">Query API</a></li>
          <li><a href={`${DOCS}#web`} target="_blank" rel="noreferrer">Web monitoring</a></li>
        </ul>
      </>}>
        {owner ? (
          <form className="set-form" onSubmit={(e) => { e.preventDefault(); create(); }}>
            <Field label="Name" hint="Optional. Where the key is used, e.g. prod ECS.">
              <input className="input" maxLength={60} value={name} onChange={(e) => setName(e.target.value)} placeholder="prod ECS" aria-label="Key name" />
            </Field>
            <Field label="What it can do">
              <select className="select" value={scope} onChange={(e) => setScope(e.target.value as KeyScope)} aria-label="Key can">
                <option value="ingest">Send data (for your apps)</option>
                <option value="read">Read data (for scripts using the query API, and AI tools)</option>
                <option value="browser">Web monitoring (a public key for your website's pages)</option>
              </select>
            </Field>
            {scope === "browser" && (
              <Field label="Your site" hint="The pages the script runs on. Separate several with commas.">
                <input className="input" required value={origins} onChange={(e) => setOrigins(e.target.value)}
                       placeholder="https://shop.example.com" aria-label="Allowed origins (comma-separated)" />
              </Field>
            )}
            <div><button className="btn primary" disabled={busy || (scope === "browser" && !origins.trim())}>Create key</button></div>
          </form>
        ) : ownersOnly("create and revoke API keys")}
        {newKey && <NewKeyBox k={newKey} onCopied={() => setNote("Copied.")} onDone={() => setNewKey(null)} />}
      </Section>

      <Section title="Your keys" about={`${acc.keys.length} of ${acc.limits.keys}. A revoked key stops working within a minute.`}>
        <table className="dtable">
          <thead><tr><th>Name</th><th>Key</th><th>Can</th><th>Created</th><th>Status</th>{owner && <th />}</tr></thead>
          <tbody>
            {acc.keys.length === 0 && <tr><td colSpan={6} className="faint">No keys yet{owner ? ": create one to send data." : "."}</td></tr>}
            {acc.keys.map((k) => (
              <tr key={k.key_id}>
                <td>{k.name ?? <span className="faint">–</span>}</td>
                <td className="mono">{k.key_id}</td>
                <td>{SCOPES[k.scope] ?? k.scope}{k.origins?.length ? <div className="faint">{k.origins.join(", ")}</div> : null}</td>
                <td className="faint">{k.created_at?.slice(0, 10)}</td>
                <td>{k.status === "expiring" ? `expires ${k.expires_at?.slice(0, 16).replace("T", " ")}` : "active"}</td>
                {owner && <td className="num"><button className="linkbtn danger" disabled={busy}
                  onClick={() => confirm(`Revoke key ${k.name ? `“${k.name}” (${k.key_id})` : k.key_id}? Apps using it stop sending within a minute.`)
                    && act(() => account.revokeKey(k.key_id), "Key revoked.")}>Revoke</button></td>}
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title="Connect your app" about="Send OpenTelemetry data over OTLP/HTTP with a key that sends data, from any OpenTelemetry SDK or the Collector. It shows up under Logs, Traces and Metrics within a minute.">
        <pre className="code">{`OTEL_EXPORTER_OTLP_ENDPOINT=${endpoint()}
OTEL_EXPORTER_OTLP_HEADERS=x-api-key=${newKey && newKey.scope === "ingest" ? newKey.api_key : "<your API key>"}
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_SERVICE_NAME=my-service`}</pre>
      </Section>
    </SettingsPage>
  );
}

function NewKeyBox({ k, onCopied, onDone }: { k: NewKey; onCopied: () => void; onDone: () => void }) {
  return (
    <div className="newkey">
      {k.scope === "browser" ? <>
        <b>New browser key{k.name ? ` “${k.name}”` : ""}: add this to every page of your site, before &lt;/head&gt;.</b>
        <span className="faint">It's public by design: it can only send web-monitoring events, and only from {k.origins?.join(", ")}. It works at once.</span>
        <pre className="code">{rumSnippet(k.api_key)}</pre>
      </> : <>
        <b>New key{k.name ? ` “${k.name}”` : ""}: copy it now, it won't be shown again.</b>
        <span className="faint">It starts working within about 2 minutes (until then, data sent with it is refused with 403).</span>
        {k.scope === "read" && <>
          <span className="faint">To use it from Claude Code (other AI tools: <a href="#/settings/ai-tools">AI tools</a>):</span>
          <pre className="code">{mcpCommand(k.api_key)}</pre>
        </>}
      </>}
      <div className="newkey-row">
        <code className="mono">{k.api_key}</code>
        <button type="button" className="btn" onClick={() => navigator.clipboard?.writeText(k.api_key).then(onCopied)}>Copy</button>
        <button type="button" className="btn ghost" onClick={onDone}>Done</button>
      </div>
    </div>
  );
}

export function AiTools() {
  return (
    <SettingsPage title="AI tools">
      <Section title="Connect over MCP" about={<>
        Let AI agents (Claude Code, Claude Desktop, Cursor, VS Code…) read your logs, traces, metrics, checks and alerts through
        Leasyd's MCP server. Use an API key that <b>reads data</b>: agents can only read, never change anything.
        <ul className="set-links">
          <li><a href="#/settings/api-keys">Create a read key</a></li>
          <li><a href={`${DOCS}#mcp`} target="_blank" rel="noreferrer">Setup for Cursor, VS Code and Claude Desktop</a></li>
        </ul>
      </>}>
        <p className="faint">For Claude Code:</p>
        <pre className="code">{mcpCommand("<your read key>")}</pre>
      </Section>
    </SettingsPage>
  );
}
