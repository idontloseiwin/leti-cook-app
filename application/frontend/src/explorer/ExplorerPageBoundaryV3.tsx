import { Component, createRef, type ReactNode } from 'react';
import { ErrorState } from './ModuleState';

// Scoped to the lazy page, so a failed import cannot remove shell navigation.
// A document reload also clears React.lazy's cached rejected promise.
export class ExplorerPageBoundaryV3 extends Component<{ name: string; children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  private reloadButton = createRef<HTMLButtonElement>();

  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch() { this.reloadButton.current?.focus(); }

  render() {
    if (!this.state.failed) return this.props.children;
    return <main aria-label={`${this.props.name} unavailable`}>
      <ErrorState title={`${this.props.name} could not be opened`}
        message="Reload this page to try again. You can still use the sidebar to open another page.">
        <button ref={this.reloadButton} className="explorer-module-state-action" type="button" onClick={() => window.location.reload()}>Reload page</button>
      </ErrorState>
    </main>;
  }
}

// Keeps the channel library usable when one of its lazy tools fails to load or render.
export class ExplorerToolBoundary extends Component<{ name: string; children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() { return { failed: true }; }

  render() {
    if (!this.state.failed) return this.props.children;
    return <p className="explorer-empty-copy" role="alert">
      {this.props.name} could not load.{' '}
      <button className="explorer-secondary-button" type="button" onClick={() => window.location.reload()}>Reload page</button>
    </p>;
  }
}
