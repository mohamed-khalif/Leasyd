import { ReactNode } from "react";
import { Load } from "../useQuery";

export function Panel(props: { title: string; span?: number; right?: ReactNode; flush?: boolean; children: ReactNode; height?: number }) {
  return (
    <section className={`panel span-${props.span ?? 12}`}>
      <header className="panel-head">
        <span>{props.title}</span>
        <span className="spacer" />
        {props.right}
      </header>
      <div className={`panel-body${props.flush ? " flush" : ""}`} style={props.height ? { minHeight: props.height } : undefined}>
        {props.children}
      </div>
    </section>
  );
}

/** Loading / error / empty states around anything loaded, and a notice when a result was cut at the row cap. */
export function Loads(props: { q: Load<{ truncated?: boolean } | object>; empty?: boolean; height?: number; children: () => ReactNode }) {
  const h = props.height ?? 140;
  if (props.q.error) return <div className="state error" style={{ minHeight: h }}>{props.q.error}</div>;
  if (!props.q.data) return <div className="skeleton" style={{ height: h }} />;
  if (props.empty) return <div className="state" style={{ minHeight: h }}>No data in this time range</div>;
  return <>{props.children()}{"truncated" in props.q.data && props.q.data.truncated && <Truncated />}</>;
}

export function Truncated() {
  return <div className="panel-foot faint">Not everything is shown: the result reached its row limit. Narrow the time range or filters to see all of it.</div>;
}
