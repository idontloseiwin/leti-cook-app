import {
  AlertTriangle,
  RefreshCw,
  ServerOff,
} from 'lucide-react';
import type { ReactNode } from 'react';

export function LoadingState({
  label,
  compact = false,
}: {
  label: string;
  compact?: boolean;
}) {
  return (
    <div
      className={`explorer-module-state explorer-v2-status-row is-loading ${compact ? 'is-compact' : ''}`}
      role="status"
    >
      <RefreshCw className="explorer-spin explorer-module-state-icon" size={20} aria-hidden="true" />
      <span className="explorer-module-state-copy">{label}</span>
    </div>
  );
}

export function ErrorState({
  title,
  message,
  onRetry,
  children,
}: {
  title: string;
  message: string;
  onRetry?: () => void;
  children?: ReactNode;
}) {
  return (
    <div className="explorer-module-state explorer-v2-status-row is-error" role="alert">
      <AlertTriangle className="explorer-module-state-icon" size={20} aria-hidden="true" />
      <div className="explorer-module-state-copy">
        <strong>{title}</strong>
        <span>{message}</span>
        {children}
      </div>
      {onRetry && (
        <button className="explorer-module-state-action" type="button" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

export function UnavailableState({
  title,
  message,
  onRetryDiscovery,
}: {
  title: string;
  message: string;
  onRetryDiscovery?: () => void;
}) {
  return (
    <div
      className="explorer-module-state explorer-v2-status-row is-unavailable"
      role="status"
    >
      <ServerOff className="explorer-module-state-icon" size={20} aria-hidden="true" />
      <div className="explorer-module-state-copy">
        <strong>{title}</strong>
        <span>{message}</span>
      </div>
      {onRetryDiscovery && (
        <button className="explorer-module-state-action" type="button" onClick={onRetryDiscovery}>
          Check availability again
        </button>
      )}
    </div>
  );
}
