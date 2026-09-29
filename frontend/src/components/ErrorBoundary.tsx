import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
  info: string | null;
}

/**
 * A page-level error boundary. During an incident demo a blank screen is worse than an honest
 * error: if one page trips over an unexpected payload shape the shell, the navigation and the
 * live status bar all stay usable, and the failure is visible and reportable.
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, info: null };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    this.setState({ info: info.componentStack ?? null });
    // Kept deliberately visible in the console: the trace is the useful part when something
    // in this console renders incorrectly.
    console.error("OpsMemory console render error:", error, info);
  }

  render(): ReactNode {
    const { error, info } = this.state;
    if (error === null) return this.props.children;

    return (
      <div className="card">
        <div className="card-head">
          <h3>This view failed to render</h3>
        </div>
        <div className="card-body">
          <div className="banner bad">
            <span>⚠</span>
            <div>
              <div className="strong">{error.name}</div>
              <div>{error.message}</div>
            </div>
          </div>
          <p className="small muted">
            The rest of the console still works — use the navigation to move on. If this keeps
            happening, the API returned a shape this view did not expect.
          </p>
          {info && (
            <details>
              <summary className="faint small" style={{ cursor: "pointer" }}>
                Component stack
              </summary>
              <pre className="code scroll">{info}</pre>
            </details>
          )}
          <div className="btn-row" style={{ marginTop: 12 }}>
            <button className="btn" onClick={() => this.setState({ error: null, info: null })}>
              Try again
            </button>
            <button className="btn ghost" onClick={() => window.location.reload()}>
              Reload console
            </button>
          </div>
        </div>
      </div>
    );
  }
}
