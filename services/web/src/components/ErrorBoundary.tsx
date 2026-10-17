import { Component, ErrorInfo, ReactNode } from "react";

type Props = { resetKey: string; children: ReactNode };
type State = { error: Error | null; key: string };

/** A page that throws while drawing shows the error here, inside the app's frame, instead of
 *  blanking everything. Moving to another page (a new resetKey) or Try again draws it afresh. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, key: this.props.resetKey };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    return props.resetKey !== state.key ? { error: null, key: props.resetKey } : null;
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("page failed to draw", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="state error" role="alert">
        Something went wrong on this page: {this.state.error.message}.{" "}
        <button className="btn small" onClick={() => this.setState({ error: null })}>Try again</button>
      </div>
    );
  }
}
