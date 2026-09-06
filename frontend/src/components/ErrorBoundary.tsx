import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/**
 * Keeps one broken screen from blanking the whole app. The sidebar and
 * navigation stay usable so a member can move somewhere that still works.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('screen crashed', error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <main className="error-screen" style={{ flex: 1, overflowY: 'auto', padding: '40px 32px' }}>
        <div className="micro-label">A room interruption</div>
        <div className="display" style={{ fontSize: 38, marginTop: 12 }}>
          This screen hit an error.
        </div>
        <div style={{ fontSize: 13, color: 'var(--muted)', marginTop: 10, lineHeight: 1.6 }}>
          Nothing was lost — your data is safe on the server. Try another screen from the sidebar,
          or reload.
        </div>
        <pre
          className="mono"
          style={{
            marginTop: 20,
            background: 'var(--ink)',
            color: '#C6C2CE',
            borderRadius: 3,
            padding: '13px 15px',
            fontSize: 11,
            lineHeight: 1.6,
            overflowX: 'auto',
            maxWidth: 720,
            whiteSpace: 'pre-wrap',
          }}
        >
          {error.message}
          {'\n\n'}
          {error.stack?.split('\n').slice(0, 6).join('\n')}
        </pre>
        <button
          className="btn-primary"
          style={{ marginTop: 18 }}
          onClick={() => this.setState({ error: null })}
        >
          TRY AGAIN
        </button>
      </main>
    );
  }
}
