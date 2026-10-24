'use client';

// TDM-78: keeps a crash in one live panel from taking the rest of the robot page (and above all the
// soft e-stop, which sits outside every boundary) down with it.

import { Component, type ReactNode } from 'react';

export class Tdm78Boundary extends Component<{ label: string; children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <div role="alert" className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-200">
          {this.props.label} stopped rendering: {this.state.error.message}.{' '}
          <button type="button" className="underline" onClick={() => this.setState({ error: null })}>
            Try again
          </button>
          . The soft e-stop still works.
        </div>
      );
    }
    return this.props.children;
  }
}
