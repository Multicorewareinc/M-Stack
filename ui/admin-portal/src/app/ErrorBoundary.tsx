import { Alert, Button } from '@multistack/ui';
import * as React from 'react';

interface State {
  hasError: boolean;
}

/** Top-level error boundary. Never surfaces the raw error/stack to the user (§190). */
export class ErrorBoundary extends React.Component<{ children: React.ReactNode }, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(error: unknown) {
    // Log a diagnostic without sensitive data (§191); never a token/credential.
    console.error('Unhandled application error', error);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex h-screen items-center justify-center p-6">
          <Alert variant="error" title="Something went wrong">
            The application encountered an unexpected error.
          </Alert>
          <Button className="ml-3" onClick={() => window.location.reload()}>
            Reload
          </Button>
        </div>
      );
    }
    return this.props.children;
  }
}
