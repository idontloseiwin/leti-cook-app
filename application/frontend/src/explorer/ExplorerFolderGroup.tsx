import { ChevronRight, Clock3, Folder } from 'lucide-react';
import { useId } from 'react';
import type { ReactNode } from 'react';

export function ExplorerFolderGroup({
  label,
  count,
  collapsed,
  toggleDisabled = false,
  recent = false,
  onToggle,
  children,
}: {
  label: string;
  count: number;
  collapsed: boolean;
  toggleDisabled?: boolean;
  recent?: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  const contentId = `explorer-v2-folder-${useId().replace(/:/g, '')}`;
  return (
    <section className="explorer-folder-group explorer-v2-folder-group">
      <button
        type="button"
        className="explorer-v2-folder-heading"
        aria-expanded={!collapsed}
        aria-controls={contentId}
        disabled={toggleDisabled}
        onClick={onToggle}
        title={recent ? 'All folders, ordered by the original date added' : toggleDisabled
          ? 'Folders remain expanded while searching'
          : `${collapsed ? 'Expand' : 'Collapse'} ${label}`}
      >
        {!recent && <ChevronRight className="explorer-v2-folder-chevron" size={14} aria-hidden="true" />}
        {recent ? <Clock3 size={13} aria-hidden="true" /> : <Folder size={13} aria-hidden="true" />}
        <span>{label}</span>
        <strong aria-label={`${count} channels`}>{count}</strong>
      </button>
      <div
        className="explorer-folder-channels explorer-v2-folder-channels"
        id={contentId}
        hidden={collapsed}
      >
        {children}
      </div>
    </section>
  );
}
