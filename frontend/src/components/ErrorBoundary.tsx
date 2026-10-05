import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
  fallback?: ReactNode | ((error: Error, reset: () => void) => ReactNode);
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('ErrorBoundary caught:', error, info);
  }

  reset = () => this.setState({ error: null });

  render() {
    if (this.state.error) {
      const fb = this.props.fallback;
      if (typeof fb === 'function') return fb(this.state.error, this.reset);
      if (fb != null) return fb;
      return (
        <div className="p-4 m-2 rounded border border-border-default bg-surface-muted text-sm text-text-soft">
          <div className="font-medium text-text-strong mb-1">Something broke here.</div>
          <div className="mb-2 text-xs text-text-faint">{this.state.error.message}</div>
          <button
            data-tip="Retry rendering this section"
            onClick={this.reset}
            className="px-2 py-1 border border-default rounded text-xs hover:bg-surface-strong"
          >
            Retry
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

export default ErrorBoundary;