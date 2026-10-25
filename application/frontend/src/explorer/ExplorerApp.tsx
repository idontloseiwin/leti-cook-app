import { useSourceExpiry } from '../sourceLifetimeReact';
import { sourceExpired, transferSourceLifetime } from '../sourceLifetime.ts';
import { retainSidebarUserState, retainFreshSidebarSources } from '../sidebarUserState';
import { ownerAccessMode } from '../ownerSession';
import {
  Bookmark,
  ChevronRight,
  Clock3,
  Folder,
  Home,
  Library,
  LoaderCircle,
  Plus,
  RadioTower,
  Search,
  ShieldCheck,
  Video,
  X,
} from 'lucide-react';
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import type { ComponentType, FormEvent, ReactNode, RefObject } from 'react';
import type {
  Channel,
  CreatedFolder,
  CreatedLabel,
  ExplorerLabel,
  Folder as ChannelFolder,
  SidebarData,
  SystemSummary,
} from '../types';
import { YouTubeAttribution } from '../components/YouTubeAttribution';
import { FolderLabelManager } from './FolderLabelManager';
import {
  addExplorerChannel,
  assignExplorerChannelFolder,
  assignExplorerChannelLabel,
  createExplorerFolder,
  createExplorerLabel,
  lookupExplorerChannelByVideo,
  isAbortError,
  isContractError,
  loadExplorerCapabilities,
  loadExplorerHomeSummary,
  loadExplorerSidebar,
  loadExplorerUserSidebar,
  loadExplorerSystemSummary,
} from './api';
import { formatCount, formatTimestamp, initials } from './format';
import type {
  CapabilityState,
  ExplorerAddChannelResult,
  ExplorerHomeSummary,
} from './model';
import { ExplorerRecentVideos } from './ExplorerRecentVideos';
import { useVisibleSummary, type SummaryState } from './useVisibleSummary';
import { homeSummaryIncomplete, systemSummaryIncomplete } from './summaryRecovery';
import {
  loadExplorerBookmarks,
  saveExplorerBookmarks,
  toggleExplorerBookmark,
} from './bookmarks';
import {
  assignExplorerSidebarChannelFolder,
  DEFAULT_EXPLORER_SIDEBAR_SORT,
  groupExplorerSidebarChannels,
  mergeExplorerSidebarChannel,
  mergeExplorerSidebarFolder,
  nextExplorerSidebarSort,
  safeExplorerAvatarUrl,
} from './sidebar';
import { parseExplorerYouTubeSearchTarget } from './youtubeSearch';
import type {
  ExplorerSidebarSort,
  ExplorerSidebarSortMetric,
} from './sidebar';
import { ErrorState, LoadingState } from './ModuleState';
import { ProviderKeyBanner, providerKeyRefusal } from '../components/ProviderKeyBanner';
import { RuntimeNotices } from '../components/RuntimeNotices';
import { apiReadAllowed, type RuntimeState } from '../runtimeNotices';
import { ExplorerPageBoundaryV3, ExplorerToolBoundary } from './ExplorerPageBoundaryV3';
import { ExplorerFolderGroup } from './ExplorerFolderGroup';
import {
  expandFolder,
  loadCollapsedFolders,
  saveCollapsedFolders,
  toggleCollapsedFolder,
} from './folderCollapse';
import { watchSharedPreferences } from './preferenceSync';
import {
  EXPLORER_V3_ROOT,
  explorerChannelPath,
  explorerOperationalPath,
  type ExplorerOperationalPage,
} from '../explorerRoutes';
import { readExplorerDestination, savedViewUrl, SAVED_VIEWS_PATH, type SavedView } from './savedViews';

const ChannelTrashControls = lazy(() => import('./ChannelTrashControls').then((module) => ({ default: module.ChannelTrashControls })));
const UserWorkBackupEntry = lazy(() => import('./UserWorkBackupDialog').then((module) => ({ default: module.UserWorkBackupEntry })));
const SavedPage = lazy(() => import('./SavedPage').then((module) => ({ default: module.SavedPage })));
const HealthPage = lazy(() => import('./HealthPage').then((module) => ({ default: module.HealthPage })));
const TelemetryPage = lazy(() => import('./TelemetryPage').then((module) => ({ default: module.TelemetryPage })));
const ResearchCatalogue = lazy(() => import('./ResearchCatalogue').then((module) => ({ default: module.ResearchCatalogue })));

type SidebarState =
  | { kind: 'loading' }
  | { kind: 'ready'; payload: SidebarData }
  | { kind: 'error'; message: string };

type ChannelLinkLookupState =
  | { kind: 'idle' }
  | { kind: 'loading'; videoId: string }
  | { kind: 'ready'; videoId: string; channelId: string | null }
  | { kind: 'error'; videoId: string; message: string };

const BOOKMARK_ADDED_ANIMATION_MS = 460;
const INLINE_LIBRARY_MEDIA_QUERY = '(min-width: 1041px)';
const SIDEBAR_SORT_OPTIONS: Array<{ key: ExplorerSidebarSortMetric; label: string }> = [
  { key: 'added_at', label: 'Added' },
  { key: 'name', label: 'Name' },
  { key: 'video_count', label: 'Videos' },
  { key: 'subscribers', label: 'Subscribers' },
];

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => (
    typeof window.matchMedia === 'function' && window.matchMedia(query).matches
  ));

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return undefined;
    const mediaQuery = window.matchMedia(query);
    const updateMatch = (event?: MediaQueryListEvent) => {
      setMatches(event ? event.matches : mediaQuery.matches);
    };
    updateMatch();
    mediaQuery.addEventListener('change', updateMatch);
    return () => mediaQuery.removeEventListener('change', updateMatch);
  }, [query]);

  return matches;
}

type ExplorerWorkspaceProps = {
  channelId: string;
  channel?: Channel;
  folders?: ChannelFolder[];
  labels?: ExplorerLabel[];
  onAssignFolder?: (channelId: string, folderId: string) => Promise<void>;
  onCreateFolder?: (name: string) => Promise<CreatedFolder>;
  onCreateLabel?: (name: string) => Promise<CreatedLabel>;
  onAssignLabel?: (channelId: string, labelKey: string, enabled: boolean) => Promise<void>;
  capabilities: CapabilityState;
  onRetryCapabilities: () => void;
  onBack: () => void;
};

function ExplorerLibraryContainer({
  open,
  inline,
  panelRef,
  children,
}: {
  open: boolean;
  inline: boolean;
  panelRef: RefObject<HTMLDivElement | null>;
  children: ReactNode;
}) {
  return (
    <div
      id="explorer-v2-channel-library"
      ref={panelRef}
      className="explorer-v2-library-drawer"
      role={inline ? 'complementary' : 'dialog'}
      aria-labelledby="explorer-v2-library-title"
      aria-modal={inline ? undefined : true}
      data-library-presentation={inline ? 'inline' : 'modal'}
      hidden={!open}
      tabIndex={inline ? undefined : -1}
    >
      {children}
    </div>
  );
}

function ExplorerLibraryTools({ children }: { children: ReactNode }) {
  return <div className="explorer-v2-library-tools">{children}</div>;
}

export function ExplorerApp({
  WorkspaceComponent,
  userWorkEnabled = false,
}: {
  WorkspaceComponent: ComponentType<ExplorerWorkspaceProps>;
  userWorkEnabled?: boolean;
}) {
  const rootPath = EXPLORER_V3_ROOT;
  const pageBoundary = (name: string, page: ReactNode) => (
    <ExplorerPageBoundaryV3 key={name} name={name}>{page}</ExplorerPageBoundaryV3>
  );
  const [sidebarState, setSidebarState] = useState<SidebarState>({ kind: 'loading' });
  const [capabilityState, setCapabilityState] = useState<CapabilityState>({ kind: 'loading' });
  useSourceExpiry(sidebarState, () => setSidebarState((current) => current.kind === 'ready'
    ? { kind: 'ready', payload: retainFreshSidebarSources(current.payload) } : current), () => { void loadSidebar(); });
  const [destination, setDestination] = useState(
    () => readExplorerDestination(window.location.pathname, window.location.search),
  );
  const selectedChannelId = destination.kind === 'channel' ? destination.channelId : undefined;
  const operationalPage = destination.kind === 'health' || destination.kind === 'telemetry' || destination.kind === 'research';
  const homeVisible = destination.kind === 'home';
  const { state: homeState, refresh: refreshHomeSummary } = useVisibleSummary(
    loadExplorerHomeSummary, homeVisible, homeSummaryIncomplete,
  );
  const { state: systemState, refresh: refreshSystemSummary } = useVisibleSummary(
    loadExplorerSystemSummary, homeVisible, systemSummaryIncomplete,
  );
  const [search, setSearch] = useState('');
  const [channelLinkLookup, setChannelLinkLookup] = useState<ChannelLinkLookupState>({
    kind: 'idle',
  });
  const [sidebarSort, setSidebarSort] = useState<ExplorerSidebarSort>(
    DEFAULT_EXPLORER_SIDEBAR_SORT,
  );
  const [mobileFolderKey, setMobileFolderKey] = useState('all');
  const [selectedLabelKeys, setSelectedLabelKeys] = useState<Set<string>>(new Set());
  const [labelMatch, setLabelMatch] = useState<'all' | 'any'>('all');
  const [onlyUnlabeled, setOnlyUnlabeled] = useState(false);
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [bookmarkedChannelIds, setBookmarkedChannelIds] = useState<Set<string>>(
    () => loadExplorerBookmarks(),
  );
  const [collapsedFolderKeys, setCollapsedFolderKeys] = useState<ReadonlySet<string>>(
    () => loadCollapsedFolders(),
  );
  const refreshUserWorkPreferences = useCallback(() => {
    setBookmarkedChannelIds(loadExplorerBookmarks());
    setCollapsedFolderKeys(loadCollapsedFolders());
  }, []);
  const libraryTriggerRef = useRef<HTMLButtonElement | null>(null);
  const libraryPanelRef = useRef<HTMLDivElement | null>(null);
  const librarySearchRef = useRef<HTMLInputElement | null>(null);
  const mainContentRef = useRef<HTMLDivElement | null>(null);
  const inlineLibrary = useMediaQuery(INLINE_LIBRARY_MEDIA_QUERY);

  const youtubeSearchTarget = useMemo(
    () => parseExplorerYouTubeSearchTarget(search),
    [search],
  );
  const searchVideoId = youtubeSearchTarget?.kind === 'video'
    ? youtubeSearchTarget.videoId
    : undefined;

  useEffect(() => {
    saveExplorerBookmarks(bookmarkedChannelIds);
  }, [bookmarkedChannelIds]);

  useEffect(() => {
    saveCollapsedFolders(collapsedFolderKeys);
  }, [collapsedFolderKeys]);

  useEffect(
    () => watchSharedPreferences(window, refreshUserWorkPreferences),
    [refreshUserWorkPreferences],
  );

  useEffect(() => {
    if (!searchVideoId) return undefined;
    const controller = new AbortController();
    let active = true;
    setChannelLinkLookup({ kind: 'loading', videoId: searchVideoId });
    void lookupExplorerChannelByVideo(searchVideoId, controller.signal)
      .then((result) => {
        if (!active) return;
        setChannelLinkLookup({
          kind: 'ready',
          videoId: searchVideoId,
          channelId: result.channel_id,
        });
      })
      .catch((error) => {
        if (!active || isAbortError(error)) return;
        setChannelLinkLookup({
          kind: 'error',
          videoId: searchVideoId,
          message: error instanceof Error ? error.message : String(error),
        });
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [searchVideoId]);
  useEffect(() => {
    if (!libraryOpen || inlineLibrary) return undefined;
    const bodyStyle = document.body.style;
    const previousBodyOverflow = bodyStyle.overflow;
    const previousBodyPaddingRight = bodyStyle.paddingRight;
    const scrollbarWidth = Math.max(0, window.innerWidth - document.documentElement.clientWidth);
    if (scrollbarWidth > 0) {
      const currentPaddingRight = Number.parseFloat(window.getComputedStyle(document.body).paddingRight) || 0;
      bodyStyle.paddingRight = `${currentPaddingRight + scrollbarWidth}px`;
    }
    bodyStyle.overflow = 'hidden';
    const focusFrame = window.requestAnimationFrame(() => librarySearchRef.current?.focus());
    const restoreTriggerFocus = () => {
      window.requestAnimationFrame(() => libraryTriggerRef.current?.focus());
    };
    const closeOnPointerDown = (event: PointerEvent) => {
      if (!(event.target instanceof Node)) return;
      if (
        libraryPanelRef.current?.contains(event.target)
        || libraryTriggerRef.current?.contains(event.target)
      ) return;
      setLibraryOpen(false);
      restoreTriggerFocus();
    };
    const keepFocusInDrawer = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        setLibraryOpen(false);
        restoreTriggerFocus();
        return;
      }
      if (event.key !== 'Tab') return;
      const panel = libraryPanelRef.current;
      if (!panel) return;
      const focusableElements = Array.from(panel.querySelectorAll<HTMLElement>([
        'a[href]',
        'button:not([disabled])',
        'input:not([disabled])',
        'select:not([disabled])',
        'textarea:not([disabled])',
        'summary',
        '[tabindex]:not([tabindex="-1"])',
      ].join(','))).filter((element) => (
        element.getAttribute('aria-hidden') !== 'true'
        && element.getClientRects().length > 0
      ));
      const first = focusableElements[0];
      const last = focusableElements[focusableElements.length - 1];
      if (!first || !last) {
        event.preventDefault();
        panel.focus();
        return;
      }
      const activeElement = document.activeElement;
      if (event.shiftKey && (activeElement === first || !panel.contains(activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (activeElement === last || !panel.contains(activeElement))) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('pointerdown', closeOnPointerDown);
    document.addEventListener('keydown', keepFocusInDrawer);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener('pointerdown', closeOnPointerDown);
      document.removeEventListener('keydown', keepFocusInDrawer);
      bodyStyle.overflow = previousBodyOverflow;
      bodyStyle.paddingRight = previousBodyPaddingRight;
    };
  }, [inlineLibrary, libraryOpen]);

  useEffect(() => {
    if (!libraryOpen || !inlineLibrary) return undefined;
    const closeInlineOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape' || !libraryPanelRef.current?.contains(document.activeElement)) return;
      event.preventDefault();
      setLibraryOpen(false);
      libraryTriggerRef.current?.focus();
    };
    document.addEventListener('keydown', closeInlineOnEscape);
    return () => document.removeEventListener('keydown', closeInlineOnEscape);
  }, [inlineLibrary, libraryOpen]);

  const toggleChannelBookmark = useCallback((channelId: string) => {
    setBookmarkedChannelIds((current) => toggleExplorerBookmark(current, channelId));
  }, []);

  const assignFolder = useCallback(async (channelId: string, folderId: string) => {
    await assignExplorerChannelFolder(channelId, folderId);
    setSidebarState((current) => {
      if (current.kind !== 'ready') return current;
      return {
        kind: 'ready',
        payload: {
          ...current.payload,
          channels: assignExplorerSidebarChannelFolder(
            current.payload.channels ?? [],
            channelId,
            folderId,
          ),
        },
      };
    });
  }, []);

  const createFolder = useCallback(async (name: string) => {
    const folder = await createExplorerFolder(name);
    if (!folder.id) throw new Error('The created folder did not return an id.');
    setSidebarState((current) => {
      if (current.kind !== 'ready') return current;
      return {
        kind: 'ready',
        payload: {
          ...current.payload,
          folders: mergeExplorerSidebarFolder(current.payload.folders ?? [], { id: folder.id, name: folder.name }),
        },
      };
    });
    return folder;
  }, []);

  const createLabel = useCallback(async (name: string) => {
    const label = await createExplorerLabel(name);
    setSidebarState((current) => {
      if (current.kind !== 'ready') return current;
      const labels = current.payload.labels ?? [];
      return {
        kind: 'ready',
        payload: {
          ...current.payload,
          labels: labels.some((item) => item.label_key === label.label_key)
            ? labels
            : [...labels, { label_key: label.label_key, name: label.name, label_type: label.label_type }],
        },
      };
    });
    return label;
  }, []);

  const assignLabel = useCallback(async (
    channelId: string,
    labelKey: string,
    enabled: boolean,
  ) => {
    await assignExplorerChannelLabel(channelId, labelKey, enabled);
    setSidebarState((current) => {
      if (current.kind !== 'ready') return current;
      return {
        kind: 'ready',
        payload: {
          ...current.payload,
          channels: (current.payload.channels ?? []).map((channel) => {
            if (channel.channel_id !== channelId) return channel;
            const keys = new Set(channel.label_keys ?? []);
            if (enabled) keys.add(labelKey);
            else keys.delete(labelKey);
            return { ...channel, label_keys: [...keys].sort() };
          }),
        },
      };
    });
  }, []);

  // Only the latest request may update its state, so a slower, older response
  // cannot replace newer data such as a channel added in the meantime.
  const sidebarRequestRef = useRef(0);
  const capabilityRequestRef = useRef(0);
  const runtimeApiAllowedRef = useRef<boolean | null>(null);
  const [runtimeApiAvailable, setRuntimeApiAvailable] = useState(false);
  const sidebarReading = useRef(false);

  const loadSidebar = useCallback(async () => {
    if (sidebarReading.current) return;
    sidebarReading.current = true;
    const request = ++sidebarRequestRef.current;
    try {
      const payload = await (runtimeApiAllowedRef.current === false ? loadExplorerUserSidebar() : loadExplorerSidebar());
      if (request === sidebarRequestRef.current) setSidebarState({ kind: 'ready', payload: retainFreshSidebarSources(payload) });
    } catch (error) {
      if (request !== sidebarRequestRef.current) return;
      try {
        const payload = await loadExplorerUserSidebar();
        if (request === sidebarRequestRef.current) setSidebarState({ kind: 'ready', payload });
        return;
      } catch { /* Keep the original read error if local work is also unavailable. */ }
      if (request !== sidebarRequestRef.current) return;
      setSidebarState({
        kind: 'error',
        message: error instanceof Error ? error.message : String(error),
      });
    } finally { sidebarReading.current = false; }
  }, []);

  const runtimeChanged = useCallback((runtime: RuntimeState) => {
    const allowed = apiReadAllowed(runtime);
    const previous = runtimeApiAllowedRef.current;
    runtimeApiAllowedRef.current = allowed;
    setRuntimeApiAvailable(allowed);
    if (!allowed && previous !== false) void loadSidebar();
  }, [loadSidebar]);

  const handleAddChannel = useCallback(async (
    value: string,
    signal?: AbortSignal,
  ): Promise<ExplorerAddChannelResult> => {
    const result = await addExplorerChannel(value, signal);
    if (result.channel?.channel_id) {
      if (sidebarState.kind !== 'ready') {
        void loadSidebar();
      } else {
        setSidebarState((current) => {
          if (current.kind !== 'ready') return current;
          return {
            kind: 'ready',
            payload: {
              ...current.payload,
              channels: mergeExplorerSidebarChannel(
                current.payload.channels ?? [],
                result.channel,
              ),
            },
          };
        });
      }
    }
    return result;
  }, [loadSidebar, sidebarState.kind]);

  const loadCapabilities = useCallback(async () => {
    const request = ++capabilityRequestRef.current;
    setCapabilityState({ kind: 'loading' });
    try {
      const payload = await loadExplorerCapabilities();
      if (request === capabilityRequestRef.current) setCapabilityState({ kind: 'ready', payload });
    } catch (error) {
      if (request !== capabilityRequestRef.current) return;
      const prefix = isContractError(error)
        ? 'The server returned an incompatible capability contract. '
        : '';
      setCapabilityState({
        kind: 'error',
        message: `${prefix}${error instanceof Error ? error.message : String(error)}`,
      });
    }
  }, []);

  useEffect(() => {
    void loadSidebar();
  }, [loadSidebar]);

  useEffect(() => {
    if (!libraryOpen) return undefined;
    // Extension/other-tab additions use the same server list. Refresh when
    // returning to the library, without a separate client-side history.
    const refreshVisibleLibrary = () => {
      if (document.visibilityState === 'visible') void loadSidebar();
    };
    refreshVisibleLibrary();
    window.addEventListener('focus', refreshVisibleLibrary);
    document.addEventListener('visibilitychange', refreshVisibleLibrary);
    return () => {
      window.removeEventListener('focus', refreshVisibleLibrary);
      document.removeEventListener('visibilitychange', refreshVisibleLibrary);
    };
  }, [libraryOpen, loadSidebar]);

  useEffect(() => {
    // Operational pages own independent reads. Do not start channel analytics or
    // Home aggregates merely because the shared rail is visible.
    if (operationalPage) return undefined;
    void loadCapabilities();
  }, [loadCapabilities, operationalPage]);

  useEffect(() => {
    const onPopState = () => setDestination(
      readExplorerDestination(window.location.pathname, window.location.search),
    );
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, []);

  const channels = sidebarState.kind === 'ready'
    ? retainFreshSidebarSources(sidebarState.payload).channels ?? []
    : [];
  const folders = sidebarState.kind === 'ready'
    ? sidebarState.payload.folders ?? []
    : [];
  const labels = sidebarState.kind === 'ready'
    ? sidebarState.payload.labels ?? []
    : [];
  const activeVideoLookup = (
    youtubeSearchTarget?.kind === 'video'
    && channelLinkLookup.kind !== 'idle'
    && channelLinkLookup.videoId === youtubeSearchTarget.videoId
  ) ? channelLinkLookup : undefined;
  const isResolvingChannelLink = activeVideoLookup?.kind === 'loading';
  const resolvedLinkChannelId = youtubeSearchTarget?.kind === 'channel'
    ? youtubeSearchTarget.channelId
    : activeVideoLookup?.kind === 'ready'
      ? activeVideoLookup.channelId ?? undefined
      : undefined;
  const channelSearchQuery = resolvedLinkChannelId || (
    isResolvingChannelLink ? '' : search
  );
  const normalizedSearch = channelSearchQuery.trim().toLocaleLowerCase();
  const filteredChannels = useMemo(() => {
    if (isResolvingChannelLink) return [];
    return channels.filter((channel) => {
      if (!normalizedSearch) return true;
      return `${channel.title || ''} ${channel.channel_id || ''}`
        .toLocaleLowerCase()
        .includes(normalizedSearch);
    });
  }, [channels, isResolvingChannelLink, normalizedSearch]);
  const labelFilteredChannels = useMemo(() => {
    return channels.filter((channel) => {
      const keys = channel.label_keys ?? [];
      if (onlyUnlabeled) return keys.length === 0;
      if (selectedLabelKeys.size === 0) return true;
      return labelMatch === 'all'
        ? [...selectedLabelKeys].every((key) => keys.includes(key))
        : [...selectedLabelKeys].some((key) => keys.includes(key));
    });
  }, [channels, labelMatch, onlyUnlabeled, selectedLabelKeys]);
  const resolvedChannelFilteredOut = Boolean(
    resolvedLinkChannelId
    && channels.some((channel) => channel.channel_id === resolvedLinkChannelId)
    && !labelFilteredChannels.some((channel) => channel.channel_id === resolvedLinkChannelId),
  );
  const linkLookupNotTracked = activeVideoLookup?.kind === 'ready'
    && activeVideoLookup.channelId === null;
  const linkLookupFailed = activeVideoLookup?.kind === 'error';
  const unsupportedYouTubeLink = youtubeSearchTarget?.kind === 'unsupported';
  const channelSearchStatus = isResolvingChannelLink
    ? 'Finding the channel from stored videos.'
    : linkLookupFailed
      ? 'Channel link lookup is temporarily unavailable.'
      : youtubeSearchTarget?.kind === 'unsupported' && youtubeSearchTarget.reason === 'handle'
        ? 'Handle links are not stored yet. Paste a video link or a /channel/ link.'
        : unsupportedYouTubeLink
          ? 'This YouTube link type is not supported.'
          : undefined;
  const channelSearchEmptyMessage = resolvedChannelFilteredOut
    ? 'This tracked channel is hidden by the current label filters.'
    : linkLookupNotTracked
      ? 'This video is not linked to a tracked channel in stored inventory.'
      : youtubeSearchTarget
        ? 'No tracked channel matches this YouTube link.'
        : normalizedSearch
          ? 'No tracked channel matches this search.'
          : 'No tracked channels yet.';
  const sidebarGroups = useMemo(
    () => groupExplorerSidebarChannels(
      labelFilteredChannels,
      folders,
      sidebarSort,
      channelSearchQuery,
    ),
    [channelSearchQuery, folders, labelFilteredChannels, sidebarSort],
  );
  const visibleChannelCount = isResolvingChannelLink
    ? 0
    : sidebarGroups.reduce(
      (total, group) => total + group.channels.length,
      0,
    );
  const mobileChannels = useMemo(() => {
    if (normalizedSearch || mobileFolderKey === 'all') {
      return sidebarGroups.flatMap((group) => group.channels);
    }
    return sidebarGroups.find((group) => group.key === mobileFolderKey)?.channels ?? [];
  }, [mobileFolderKey, normalizedSearch, sidebarGroups]);
  const selectedChannel = channels.find(
    (channel) => channel.channel_id === selectedChannelId,
  );
  const selectedSidebarGroupKey = useMemo(() => sidebarGroups.find((group) => (
    group.channels.some((channel) => channel.channel_id === selectedChannelId)
  ))?.key, [selectedChannelId, sidebarGroups]);

  useEffect(() => {
    setCollapsedFolderKeys((current) => (
      expandFolder(current, selectedSidebarGroupKey)
    ));
  }, [selectedSidebarGroupKey]);

  useEffect(() => {
    if (
      mobileFolderKey !== 'all'
      && !sidebarGroups.some((group) => group.key === mobileFolderKey)
    ) {
      setMobileFolderKey('all');
    }
  }, [mobileFolderKey, sidebarGroups]);

  const selectChannel = (channelId: string, keepLibraryOpen = false) => {
    if (channelId !== selectedChannelId) {
      window.history.pushState({}, '', explorerChannelPath(channelId));
    }
    setDestination({ kind: 'channel', channelId });
    if (!inlineLibrary && !keepLibraryOpen) {
      setLibraryOpen(false);
      window.requestAnimationFrame(() => mainContentRef.current?.focus());
    }
  };

  const showChannelList = () => {
    window.history.pushState({}, '', rootPath);
    setDestination({ kind: 'home' });
    if (!inlineLibrary) {
      setLibraryOpen(false);
      window.requestAnimationFrame(() => mainContentRef.current?.focus());
    }
  };

  const showSaved = () => {
    if (destination.kind === 'saved') return;
    window.history.pushState({}, '', SAVED_VIEWS_PATH);
    setDestination({ kind: 'saved' });
    if (!inlineLibrary) setLibraryOpen(false);
    window.requestAnimationFrame(() => mainContentRef.current?.focus());
  };
  const showOperationalPage = (page: ExplorerOperationalPage) => {
    if (destination.kind === page) return;
    window.history.pushState({}, '', explorerOperationalPath(page));
    setDestination({ kind: page });
    if (!inlineLibrary) setLibraryOpen(false);
    window.requestAnimationFrame(() => mainContentRef.current?.focus());
  };
  const navigateSaved = (path: string) => {
    const target = new URL(path, window.location.origin);
    if (target.href !== window.location.href) window.history.pushState({}, '', target);
    setDestination(readExplorerDestination(target.pathname, target.search));
    if (!inlineLibrary) setLibraryOpen(false);
    window.requestAnimationFrame(() => mainContentRef.current?.focus());
  };
  const openSavedView = (view: SavedView) => {
    const target = savedViewUrl(view, window.location.origin);
    window.history.pushState({}, '', target);
    setDestination({ kind: 'channel', channelId: view.channel_id });
    if (!inlineLibrary) setLibraryOpen(false);
    window.requestAnimationFrame(() => mainContentRef.current?.focus());
  };

  return (
    <div className={[
      'explorer-app',
      'is-updated',
      inlineLibrary ? 'explorer-v2-library-inline' : '',
      libraryOpen ? 'explorer-v2-library-open' : '',
    ].filter(Boolean).join(' ')}>
      <a className="explorer-skip-link" href="#explorer-main">Skip to Explorer content</a>
      <aside
        className="explorer-sidebar explorer-v2-rail"
        role="presentation"
      >
        <div className="explorer-brand">
          <span
            className="explorer-brand-mark explorer-v2-brand-letter"
            aria-hidden="true"
          >
            L
          </span>
          <div>
            <strong>Leti Cook</strong>
            <span>Channel Explorer</span>
          </div>
        </div>

        <nav className="explorer-product-links" aria-label="Product navigation">
          <a
            href={rootPath}
            aria-current={destination.kind === 'home' ? 'page' : undefined}
            title="Home"
            data-label="Home"
          >
            <Home size={17} aria-hidden="true" />
            <span>Home</span>
          </a>
          <button
            ref={libraryTriggerRef}
            type="button"
            className={`explorer-v2-library-trigger${libraryOpen || selectedChannelId ? ' is-active' : ''}`}
            aria-expanded={libraryOpen}
            aria-controls="explorer-v2-channel-library"
            aria-current={selectedChannelId ? 'page' : undefined}
            aria-label={libraryOpen ? 'Close channel library' : 'Open channel library'}
            onClick={() => setLibraryOpen((open) => !open)}
            title="Channel library"
            data-label="Library"
          >
            <Library size={17} aria-hidden="true" />
            <span>Library</span>
          </button>
          <a href={SAVED_VIEWS_PATH}
            aria-current={destination.kind === 'saved' ? 'page' : undefined} title="Saved" data-label="Saved"
            onClick={(event) => {
              if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
              event.preventDefault(); showSaved();
            }}><Bookmark size={17} aria-hidden="true" /><span>Saved</span></a>
          <a
            href={explorerOperationalPath('health')}
            aria-current={destination.kind === 'health' ? 'page' : undefined}
            onClick={(event) => {
              if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
              event.preventDefault(); showOperationalPage('health');
            }}
            title="Health"
            data-label="Health"
          >
            <ShieldCheck size={17} aria-hidden="true" />
            <span>Health</span>
          </a>
          <a
            href={explorerOperationalPath('telemetry')}
            aria-current={destination.kind === 'telemetry' ? 'page' : undefined}
            onClick={(event) => {
              if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
              event.preventDefault(); showOperationalPage('telemetry');
            }}
            title="Telemetry"
            data-label="Telemetry"
          >
            <RadioTower size={17} aria-hidden="true" />
            <span>Telemetry</span>
          </a>
        </nav>

        <ExplorerLibraryContainer
          open={libraryOpen}
          inline={inlineLibrary}
          panelRef={libraryPanelRef}
        >
          <header className="explorer-v2-library-heading">
            <div>
              <span>Workspace</span>
              <strong id="explorer-v2-library-title">Channel library</strong>
              <small>{formatCount(channels.length, { compact: false })} tracked channels</small>
            </div>
            <button
              type="button"
              onClick={() => {
                setLibraryOpen(false);
                libraryTriggerRef.current?.focus();
              }}
              aria-label="Close channel library"
              title="Close"
            >
              <X size={17} aria-hidden="true" />
            </button>
          </header>
          <p id="explorer-v2-library-bookmark-help" className="explorer-sr-only">
            Click to open a channel. Double-click to toggle its bookmark.
          </p>

        <ExplorerLibraryTools>
        <ExplorerAddChannelForm
          onAdd={handleAddChannel}
          onOpenChannel={selectChannel}
        />
        <ExplorerToolBoundary name="Folders and labels">
          <FolderLabelManager folders={folders} labels={labels} onChanged={() => void loadSidebar()} />
        </ExplorerToolBoundary>
        {userWorkEnabled && <>
          <ExplorerToolBoundary name="User-work backup">
            <Suspense fallback={null}>
              <UserWorkBackupEntry onPreferencesChanged={refreshUserWorkPreferences} />
            </Suspense>
          </ExplorerToolBoundary>
          {/* Local mode has no account, so there is nothing to sign out of. */}
          {ownerAccessMode() === 'password' && <a className="explorer-secondary-button" href="/auth">Account / sign out</a>}
          <ExplorerToolBoundary name="Channel removal">
            <Suspense fallback={null}>
              <ChannelTrashControls channelId={channels.some((channel) => channel.channel_id === selectedChannelId) ? selectedChannelId : undefined}
                channel={channels.find(channel => channel.channel_id === selectedChannelId)} apiAvailable={runtimeApiAvailable}
                onChanged={(removedId) => {
                  if (removedId) {
                    const bookmarks = loadExplorerBookmarks(); bookmarks.delete(removedId); saveExplorerBookmarks(bookmarks);
                  }
                  window.location.assign(rootPath);
                }} />
            </Suspense>
          </ExplorerToolBoundary>
        </>}

        <label className="explorer-channel-search">
          <span className="explorer-sr-only">Search tracked channels</span>
          <Search size={16} aria-hidden="true" />
          <input
            ref={librarySearchRef}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Find by name or YouTube link"
            aria-busy={isResolvingChannelLink}
            aria-describedby={channelSearchStatus ? 'explorer-channel-search-status' : undefined}
          />
        </label>
        {channelSearchStatus && (
          <span
            id="explorer-channel-search-status"
            className={`explorer-channel-search-status${isResolvingChannelLink ? ' is-loading' : ''}`}
            role="status"
            aria-live="polite"
          >
            {isResolvingChannelLink && <LoaderCircle size={13} aria-hidden="true" />}
            {channelSearchStatus}
          </span>
        )}

        <div className="explorer-channel-list-heading">
          <span>Tracked channels</span>
          <strong>{channels.length}</strong>
        </div>

        <div className="explorer-channel-sort" role="group" aria-label="Sort tracked channels">
          {SIDEBAR_SORT_OPTIONS.map((option) => {
            const active = sidebarSort.metric === option.key;
            const direction = active
              ? sidebarSort.direction
              : option.key === 'name' ? 'asc' : 'desc';
            const primaryLabel = option.key === 'name'
              ? direction === 'asc' ? 'A-Z' : 'Z-A'
              : option.label;
            const directionLabel = option.key === 'name'
              ? 'Name'
              : option.key === 'added_at'
                ? direction === 'desc' ? 'Newest' : 'Oldest'
                : direction === 'desc' ? 'High-Low' : 'Low-High';
            return (
              <button
                type="button"
                key={option.key}
                className={active ? 'is-active' : undefined}
                aria-pressed={active}
                onClick={() => setSidebarSort((current) => (
                  nextExplorerSidebarSort(current, option.key)
                ))}
                title={active
                  ? `Reverse ${option.key === 'added_at' ? 'date added' : option.label.toLocaleLowerCase()} sorting`
                  : `Sort channels by ${option.key === 'added_at' ? 'date added' : option.label.toLocaleLowerCase()}`}
              >
                <span>{primaryLabel}</span>
                <small>{directionLabel}</small>
              </button>
            );
          })}
        </div>

          <ExplorerLabelFilter
            labels={labels}
            selected={selectedLabelKeys}
            match={labelMatch}
            onlyUnlabeled={onlyUnlabeled}
            onToggle={(key) => {
              setOnlyUnlabeled(false);
              setSelectedLabelKeys((current) => {
                const next = new Set(current);
                if (next.has(key)) next.delete(key);
                else next.add(key);
                return next;
              });
            }}
            onMatchChange={setLabelMatch}
            onOnlyUnlabeled={() => {
              setSelectedLabelKeys(new Set());
              setOnlyUnlabeled((value) => !value);
            }}
            onClear={() => {
              setSelectedLabelKeys(new Set());
              setOnlyUnlabeled(false);
            }}
          />
        </ExplorerLibraryTools>

        <div className="explorer-channel-list">
          {sidebarState.kind === 'loading' && <LoadingState label="Loading channels…" compact />}
          {sidebarState.kind === 'error' && (
            <ErrorState
              title="Channel list could not load"
              message={sidebarState.message}
              onRetry={() => void loadSidebar()}
            />
          )}
          {sidebarState.kind === 'ready' && visibleChannelCount > 0 && (
            <div className="explorer-channel-groups">
              {sidebarGroups.map((group) => (
                <ExplorerFolderGroup
                  key={group.key}
                  label={group.label}
                  recent={sidebarSort.metric === 'added_at'}
                  count={group.channels.length}
                  collapsed={Boolean(
                    sidebarSort.metric !== 'added_at' && !normalizedSearch
                    && collapsedFolderKeys.has(group.key)
                  )}
                  toggleDisabled={sidebarSort.metric === 'added_at' || Boolean(normalizedSearch)}
                  onToggle={() => setCollapsedFolderKeys((current) => (
                    toggleCollapsedFolder(current, group.key)
                  ))}
                >
                  {group.channels.map((channel) => (
                    <ChannelButton
                      key={channel.channel_id || channel.title}
                      channel={channel}
                      active={channel.channel_id === selectedChannelId}
                      bookmarked={Boolean(
                        channel.channel_id && bookmarkedChannelIds.has(channel.channel_id),
                      )}
                      onSelect={(channelId) => selectChannel(channelId, true)}
                      onToggleBookmark={toggleChannelBookmark}
                      labels={labels}
                      showAdded={sidebarSort.metric === 'added_at'}
                      folderName={folders.find((folder) => folder.id === channel.folder_id)?.name}
                    />
                  ))}
                  {group.channels.length === 0 && (
                    <span className="explorer-folder-empty">No channels</span>
                  )}
                </ExplorerFolderGroup>
              ))}
            </div>
          )}
          {sidebarState.kind === 'ready' && visibleChannelCount > 0 && (
            <div className="explorer-mobile-channel-browser">
              {!normalizedSearch && sidebarSort.metric !== 'added_at' && (
                <div className="explorer-mobile-folder-tabs" aria-label="Channel folders">
                  <button
                    type="button"
                    className={mobileFolderKey === 'all' ? 'is-active' : undefined}
                    aria-pressed={mobileFolderKey === 'all'}
                    onClick={() => setMobileFolderKey('all')}
                  >
                    All <span>{channels.length}</span>
                  </button>
                  {sidebarGroups.map((group) => (
                    <button
                      type="button"
                      key={group.key}
                      className={mobileFolderKey === group.key ? 'is-active' : undefined}
                      aria-pressed={mobileFolderKey === group.key}
                      onClick={() => setMobileFolderKey(group.key)}
                    >
                      {group.label} <span>{group.channels.length}</span>
                    </button>
                  ))}
                </div>
              )}
              <div className="explorer-mobile-channel-strip">
                {mobileChannels.map((channel) => (
                  <ChannelButton
                    key={channel.channel_id || channel.title}
                    channel={channel}
                    active={channel.channel_id === selectedChannelId}
                    bookmarked={Boolean(
                      channel.channel_id && bookmarkedChannelIds.has(channel.channel_id),
                    )}
                    onSelect={(channelId) => selectChannel(channelId, true)}
                    onToggleBookmark={toggleChannelBookmark}
                    labels={labels}
                    showAdded={sidebarSort.metric === 'added_at'}
                    folderName={folders.find((folder) => folder.id === channel.folder_id)?.name}
                  />
                ))}
                {mobileChannels.length === 0 && (
                  <span className="explorer-folder-empty">No channels in this folder.</span>
                )}
              </div>
            </div>
          )}
          {sidebarState.kind === 'ready' && !isResolvingChannelLink && visibleChannelCount === 0 && (
            <span className="explorer-empty-copy">
              {channelSearchEmptyMessage}
            </span>
          )}
        </div>

        {!selectedChannelId && (
          <div className="explorer-sidebar-attribution">
            <YouTubeAttribution label="Open YouTube" />
          </div>
        )}
        </ExplorerLibraryContainer>
      </aside>

      <div
        ref={mainContentRef}
        className="explorer-content"
        id="explorer-main" tabIndex={-1}
      >
        <RuntimeNotices onChange={runtimeChanged} />
        {destination.kind === 'research' ? (
          pageBoundary('Research catalogue', <Suspense fallback={<LoadingState label="Loading catalogue…" />}><ResearchCatalogue folders={folders.flatMap(folder => folder.id && folder.name ? [{ id: folder.id, name: folder.name }] : [])} foldersLoaded={sidebarState.kind === 'ready'} apiAvailable={runtimeApiAvailable} /></Suspense>)
        ) : destination.kind === 'health' ? (
          pageBoundary('Health', <Suspense fallback={<LoadingState label="Loading Health…" />}><HealthPage /></Suspense>)
        ) : destination.kind === 'telemetry' ? (
          pageBoundary('Telemetry', <Suspense fallback={<LoadingState label="Loading Telemetry…" />}><TelemetryPage /></Suspense>)
        ) : destination.kind === 'saved' ? (
          pageBoundary('Saved', <Suspense fallback={<LoadingState label="Loading Saved…" />}><SavedPage destination={destination} channels={channels} onOpenView={openSavedView} onNavigate={navigateSaved} /></Suspense>)
        ) : selectedChannelId ? (
          // Keyed by channel: a failure in one channel does not follow the owner to the next.
          <ExplorerPageBoundaryV3 key={`channel:${selectedChannelId}`} name="Channel">
            <WorkspaceComponent
              key={selectedChannelId}
              channelId={selectedChannelId}
              channel={selectedChannel}
              folders={folders}
              labels={labels}
              onAssignFolder={assignFolder}
              onCreateFolder={createFolder}
              onCreateLabel={createLabel}
              onAssignLabel={assignLabel}
              capabilities={capabilityState}
              onRetryCapabilities={() => void loadCapabilities()}
              onBack={showChannelList}
            />
          </ExplorerPageBoundaryV3>
        ) : (
          pageBoundary('Home', <ExplorerHome
            channels={filteredChannels}
            sidebarState={sidebarState}
            homeState={homeState}
            systemState={systemState}
            onRetryHomeSummary={refreshHomeSummary}
            onRetrySystemSummary={refreshSystemSummary}
            onSelect={selectChannel}
            channelPath={(channelId) => explorerChannelPath(channelId)}
            healthPath={explorerOperationalPath('health')}
            onOpenHealth={() => showOperationalPage('health')}
          />)
        )}
      </div>
    </div>
  );
}

type AddChannelFeedback = {
  tone: 'success' | 'warning' | 'error';
  prefix?: string;
  message: string;
  channelId?: string;
};

function ExplorerAddChannelForm({
  onAdd,
  onOpenChannel,
}: {
  onAdd: (value: string, signal?: AbortSignal) => Promise<ExplorerAddChannelResult>;
  onOpenChannel: (channelId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [value, setValue] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [feedback, setFeedback] = useState<AddChannelFeedback>();
  useSourceExpiry(feedback, () => setFeedback(undefined));
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => () => controllerRef.current?.abort(), []);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const input = value.trim();
    if (!input || submitting) return;
    setSubmitting(true);
    setFeedback(undefined);
    const controller = new AbortController();
    controllerRef.current = controller;
    try {
      const result = await onAdd(input, controller.signal);
      const channelTitle = result.channel?.title || result.title || 'Channel';
      const channelId = result.channel?.channel_id;
      if (result.status === 'added') {
        setFeedback(transferSourceLifetime(result, { tone: 'success', prefix: 'Added:', message: channelTitle, channelId }));
        setValue('');
      } else if (result.status === 'restored') {
        setFeedback(transferSourceLifetime(result, { tone: 'success', prefix: 'Restored:', message: channelTitle, channelId }));
        setValue('');
      } else {
        setFeedback(transferSourceLifetime(result, { tone: 'warning', prefix: 'Already tracked:', message: channelTitle, channelId }));
      }
      if (channelId) onOpenChannel(channelId);
    } catch (error) {
      if (isAbortError(error)) return;
      setFeedback({
        tone: 'error',
        message: error instanceof Error ? error.message : String(error),
      });
    } finally {
      controllerRef.current = null;
      setSubmitting(false);
    }
  };

  if (!expanded) {
    return (
      <button
        type="button"
        className="explorer-add-channel-toggle"
        onClick={() => setExpanded(true)}
      >
        <Plus size={16} aria-hidden="true" />
        Add channel
      </button>
    );
  }

  return (
    <form className="explorer-add-channel" onSubmit={handleSubmit}>
      <div className="explorer-add-channel-heading">
        <label htmlFor="explorer-add-channel-input">Add channel</label>
        <button
          type="button"
          aria-label="Close add channel form"
          onClick={() => {
            controllerRef.current?.abort();
            setExpanded(false);
            setFeedback(undefined);
          }}
          disabled={submitting}
        >
          <X size={15} aria-hidden="true" />
        </button>
      </div>
      <div className="explorer-add-channel-row">
        <input
          id="explorer-add-channel-input"
          value={value}
          onChange={(event) => {
            setValue(event.target.value);
            setFeedback(undefined);
          }}
          placeholder="@handle or YouTube URL"
          autoComplete="off"
          autoFocus
          disabled={submitting}
        />
        <button type="submit" disabled={!value.trim() || submitting} title="Add channel">
          {submitting
            ? <LoaderCircle className="explorer-spin" size={16} aria-hidden="true" />
            : <Plus size={16} aria-hidden="true" />}
        </button>
      </div>
      {submitting && <p className="explorer-add-channel-status">Resolving channel…</p>}
      {feedback && (
        <p
          className={`explorer-add-channel-status is-${feedback.tone}`}
          aria-live="polite"
        >
          {feedback.prefix && <span>{feedback.prefix} </span>}
          {feedback.channelId ? (
            <button type="button" onClick={() => onOpenChannel(feedback.channelId!)}>
              {feedback.message}
            </button>
          ) : feedback.message}
        </p>
      )}
    </form>
  );
}

function ExplorerLabelFilter({
  labels,
  selected,
  match,
  onlyUnlabeled,
  onToggle,
  onMatchChange,
  onOnlyUnlabeled,
  onClear,
}: {
  labels: ExplorerLabel[];
  selected: ReadonlySet<string>;
  match: 'all' | 'any';
  onlyUnlabeled: boolean;
  onToggle: (key: string) => void;
  onMatchChange: (value: 'all' | 'any') => void;
  onOnlyUnlabeled: () => void;
  onClear: () => void;
}) {
  const activeCount = selected.size + (onlyUnlabeled ? 1 : 0);
  return (
    <details className="explorer-label-filter">
      <summary>Labels{activeCount ? ` · ${activeCount}` : ''}</summary>
      <div>
        <button type="button" className={onlyUnlabeled ? 'is-active' : ''} aria-pressed={onlyUnlabeled} onClick={onOnlyUnlabeled}>
          No labels
        </button>
        {labels.map((label) => (
          <button
            type="button"
            key={label.label_key}
            className={selected.has(label.label_key) ? 'is-active' : ''}
            aria-pressed={selected.has(label.label_key)}
            onClick={() => onToggle(label.label_key)}
          >
            {label.name}
          </button>
        ))}
      </div>
      {selected.size > 1 && (
        <fieldset>
          <legend>Match</legend>
          <button type="button" className={match === 'all' ? 'is-active' : ''} aria-pressed={match === 'all'} onClick={() => onMatchChange('all')}>All</button>
          <button type="button" className={match === 'any' ? 'is-active' : ''} aria-pressed={match === 'any'} onClick={() => onMatchChange('any')}>Any</button>
        </fieldset>
      )}
      {activeCount > 0 && <button type="button" onClick={onClear}>Clear labels</button>}
    </details>
  );
}

function ChannelButton({
  channel,
  active,
  bookmarked,
  onSelect,
  onToggleBookmark,
  labels,
  showAdded = false,
  folderName,
}: {
  channel: Channel;
  active: boolean;
  bookmarked: boolean;
  onSelect: (channelId: string) => void;
  onToggleBookmark: (channelId: string) => void;
  labels: ExplorerLabel[];
  showAdded?: boolean;
  folderName?: string;
}) {
  const channelId = channel.channel_id;
  const avatar = safeExplorerAvatarUrl(channel.avatar_url);
  const [avatarFailed, setAvatarFailed] = useState(false);
  const [bookmarkAnimating, setBookmarkAnimating] = useState(false);
  const [bookmarkAnnouncement, setBookmarkAnnouncement] = useState('');
  const bookmarkAnimationRef = useRef<number | undefined>(undefined);
  const channelLabels = labels.filter((label) => (channel.label_keys ?? []).includes(label.label_key));

  useEffect(() => {
    setAvatarFailed(false);
  }, [avatar]);

  useEffect(() => () => {
    window.clearTimeout(bookmarkAnimationRef.current);
  }, []);

  if (!channelId) return null;
  const handleBookmarkToggle = () => {
    onToggleBookmark(channelId);
    setBookmarkAnnouncement(`${bookmarked ? 'Bookmark removed for' : 'Bookmarked'} ${channel.title || channelId}`);
    if (!bookmarked) {
      setBookmarkAnimating(true);
      window.clearTimeout(bookmarkAnimationRef.current);
      bookmarkAnimationRef.current = window.setTimeout(() => {
        setBookmarkAnimating(false);
      }, BOOKMARK_ADDED_ANIMATION_MS);
    }
  };
  return (
    <div className={[
      'explorer-channel-row',
      active ? 'is-active' : '',
      bookmarked ? 'is-bookmarked' : '',
      bookmarkAnimating ? 'is-bookmarking' : '',
    ].filter(Boolean).join(' ')}>
      <button
        type="button"
        className="explorer-channel-button"
        aria-current={active ? 'page' : undefined}
        aria-label={`${channel.title || channelId}${bookmarked ? ', bookmarked' : ''}`}
        aria-describedby="explorer-v2-library-bookmark-help"
        onClick={() => onSelect(channelId)}
        onDoubleClick={handleBookmarkToggle}
        title={channel.title || channelId}
      >
        {avatar && !avatarFailed ? (
          <img
            className="explorer-channel-logo"
            src={avatar}
            alt=""
            loading="lazy"
            referrerPolicy="no-referrer"
            onError={() => setAvatarFailed(true)}
          />
        ) : (
          <span className="explorer-channel-initials" aria-hidden="true">
            {initials(channel.title)}
          </span>
        )}
        <span className="explorer-channel-body">
          <strong>{channel.title || channelId}</strong>
          {showAdded ? <>
            <small>{folderName ? `Folder: ${folderName}` : 'No folder'}</small>
            <small>Added: {formatTimestamp(channel.added_at, 'Date not recorded')}</small>
          </> : <>
            <small>{formatCount(channel.subscribers, { missing: '—' })} subscribers</small>
            <small>{formatCount(channel.video_count, { missing: '—' })} total videos</small>
          </>}
          {channelLabels.length > 0 && (
            <span className="explorer-channel-labels">
              {channelLabels.slice(0, 2).map((label) => (
                <span key={label.label_key}>{label.name}</span>
              ))}
              {channelLabels.length > 2 && <span>+{channelLabels.length - 2}</span>}
            </span>
          )}
        </span>
      </button>
      <span className="explorer-sr-only" role="status" aria-live="polite">{bookmarkAnnouncement}</span>
    </div>
  );
}

function ExplorerHome({
  channels,
  sidebarState,
  homeState,
  systemState,
  onRetryHomeSummary,
  onRetrySystemSummary,
  onSelect,
  channelPath,
  healthPath,
  onOpenHealth,
}: {
  channels: Channel[];
  sidebarState: SidebarState;
  homeState: SummaryState<ExplorerHomeSummary>;
  systemState: SummaryState<SystemSummary>;
  onRetryHomeSummary: () => void;
  onRetrySystemSummary: () => void;
  onSelect: (channelId: string) => void;
  channelPath: (channelId: string) => string;
  healthPath: string;
  onOpenHealth?: () => void;
}) {
  const system = systemPresentation(systemState);
  const syncAt = homeState.kind === 'ready'
    ? homeState.payload.last_successful_sync_at
    : null;
  const syncValue = homeState.kind === 'loading'
    ? 'Loading…'
    : homeState.kind === 'error'
      ? 'Sync unavailable'
      : syncAt
        ? `Sync ${formatRelativeTime(syncAt)}`
        : 'No successful sync';
  const dataChangeAt = homeState.kind === 'ready'
    ? homeState.payload.last_data_change_at
    : null;
  const dataChangeValue = homeState.kind === 'loading'
    ? 'Data change loading…'
    : homeState.kind === 'error'
      ? 'Data change unavailable'
      : dataChangeAt
        ? `Data changed ${formatRelativeTime(dataChangeAt)}`
        : 'No data change recorded';
  const trackedChannels = sidebarState.kind === 'ready'
    ? sidebarState.payload.channels?.length ?? 0
    : undefined;
  const folders = sidebarState.kind === 'ready'
    ? sidebarState.payload.folders?.length ?? 0
    : undefined;
  const savedVideos = homeState.kind === 'ready' ? homeState.payload.saved_videos : undefined;

  return (
    <main className="explorer-home explorer-v2-home">
      <header className="explorer-v2-home-header">
        <div>
          <span className="explorer-eyebrow">Workspace</span>
          <h1>Channel Explorer</h1>
          <p>Stored channel and video data, kept separate from calculated analysis.</p>
        </div>
        {trackedChannels !== undefined && (
          <span className="explorer-v2-home-count">
            {formatCount(trackedChannels, { compact: false })} tracked channels
          </span>
        )}
      </header>
      <ProviderKeyBanner
        refusal={systemState.kind === 'ready' ? providerKeyRefusal(systemState.payload.provider_key) : undefined}
      />
      <section
        className="explorer-home-summary explorer-v2-home-summary"
        aria-label="System and stored data summary"
      >
        <HomeSummaryCard
          label="System"
          icon={<ShieldCheck size={18} aria-hidden="true" />}
          tone={system.tone}
        >
          <strong>{system.label}</strong>
          {systemState.kind === 'ready' && systemState.payload.version && <small>Leti Cook {systemState.payload.version}</small>}
          {system.reason && <small title={system.detail}>{system.reason}</small>}
          {(systemState.kind === 'error' || systemState.kind === 'ready' && systemSummaryIncomplete(systemState.payload))
            && <button type="button" onClick={onRetrySystemSummary}>Try again</button>}
          {system.problem && (
            <a href={healthPath} onClick={(event) => {
              if (!onOpenHealth || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
              event.preventDefault(); onOpenHealth();
            }}>View Health</a>
          )}
        </HomeSummaryCard>

        <HomeSummaryCard
          label="Last update"
          icon={<Clock3 size={18} aria-hidden="true" />}
        >
          <strong title={syncAt ? formatTimestamp(syncAt) : undefined}>{syncValue}</strong>
          <small title={dataChangeAt ? formatTimestamp(dataChangeAt) : undefined}>{dataChangeValue}</small>
        </HomeSummaryCard>

        <HomeSummaryCard
          label="Tracked channels"
          icon={<Folder size={18} aria-hidden="true" />}
        >
          <strong>
            {sidebarState.kind === 'loading'
              ? 'Loading…'
              : trackedChannels === undefined
                ? 'Unavailable'
                : formatCount(trackedChannels)}
          </strong>
          <small>
            {sidebarState.kind === 'loading'
              ? 'Loading folder count…'
              : folders === undefined
                ? 'Folder count unavailable'
                : `${formatCount(folders, { compact: false })} folders`}
          </small>
        </HomeSummaryCard>

        <HomeSummaryCard
          label="Saved videos"
          icon={<Video size={18} aria-hidden="true" />}
        >
          <strong>
            {homeState.kind === 'loading'
              ? 'Loading…'
              : savedVideos
                ? formatCount(savedVideos.total)
                : 'Unavailable'}
          </strong>
          {savedVideos ? (
            <small className="explorer-video-type-counts">
              <span>Long-form {formatCount(savedVideos.long)}</span>
              <span>Shorts {formatCount(savedVideos.short)}</span>
              <span>Streams {formatCount(savedVideos.stream)}</span>
            </small>
          ) : (
            <small>
              {homeState.kind === 'loading'
                ? 'Loading typed inventory counts…'
                : 'Typed inventory counts unavailable'}
            </small>
          )}
          {homeState.kind !== 'loading' && !savedVideos && <button type="button" onClick={onRetryHomeSummary}>Try again</button>}
        </HomeSummaryCard>
      </section>

      <ExplorerRecentVideos
        channels={sidebarState.kind === 'ready' ? sidebarState.payload.channels ?? [] : []}
        channelPath={channelPath}
      />

      <section className="explorer-home-channels explorer-v2-home-channels">
        <header className="explorer-v2-home-section-header">
          <div>
            <span className="explorer-eyebrow">Library</span>
            <h2>Choose a channel</h2>
            <p>Open a tracked channel to review its stored long-form data.</p>
          </div>
        </header>
        {sidebarState.kind === 'loading' && <LoadingState label="Loading tracked channels…" />}
        {sidebarState.kind === 'error' && (
          <ErrorState title="Tracked channels unavailable" message={sidebarState.message} />
        )}
        {sidebarState.kind === 'ready' && (
          <div className="explorer-welcome-grid explorer-v2-channel-list">
            {channels.slice(0, 8).map((channel) => channel.channel_id ? (
              <button type="button" key={channel.channel_id} onClick={() => onSelect(channel.channel_id as string)}>
                <span className="explorer-channel-initials" aria-hidden="true">{initials(channel.title)}</span>
                <span>
                  <strong>{channel.title || channel.channel_id}</strong>
                  <small>Open Explorer</small>
                </span>
                <ChevronRight size={18} aria-hidden="true" />
              </button>
            ) : null)}
          </div>
        )}
      </section>
    </main>
  );
}

function HomeSummaryCard({
  label,
  icon,
  tone = 'neutral',
  children,
}: {
  label: string;
  icon: ReactNode;
  tone?: 'neutral' | 'healthy' | 'warning' | 'error';
  children: ReactNode;
}) {
  return (
    <article className={`explorer-home-card is-${tone} explorer-v2-summary-cell`}>
      <span className="explorer-home-card-label">{icon}{label}</span>
      <div className="explorer-v2-summary-value">{children}</div>
    </article>
  );
}

function formatRelativeTime(value: string): string {
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return 'at an invalid time';
  const elapsedMs = Date.now() - timestamp;
  if (elapsedMs < 0) return 'just now';
  const minutes = Math.floor(elapsedMs / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function shortReason(value?: string): string | undefined {
  const raw = value?.trim();
  if (!raw) return undefined;
  const knownReasons: Record<string, string> = {
    latest_run_failed: 'Latest sync failed',
    no_sync_runs: 'No sync runs recorded',
    system_warning: 'System needs attention',
  };
  const humanized = knownReasons[raw]
    ?? raw.replace(/_/g, ' ').replace(/\s+/g, ' ');
  const normalized = humanized.charAt(0).toUpperCase() + humanized.slice(1);
  if (!normalized) return undefined;
  return normalized.length > 92 ? `${normalized.slice(0, 89)}…` : normalized;
}

function systemPresentation(state: SummaryState<SystemSummary>): {
  label: 'Checking…' | 'Healthy' | 'Warning' | 'Error' | 'Paused';
  tone: 'neutral' | 'healthy' | 'warning' | 'error';
  problem: boolean;
  reason?: string;
  detail?: string;
} {
  if (state.kind === 'loading') {
    return { label: 'Checking…', tone: 'neutral', problem: false };
  }
  if (state.kind === 'error') {
    return {
      label: 'Warning',
      tone: 'warning',
      problem: true,
      reason: 'System health unavailable',
      detail: state.message,
    };
  }
  const status = state.payload.status?.toLowerCase();
  const warning = state.payload.warnings?.[0];
  const warningDetail = warning?.message || warning?.code;
  if (status === 'ok' || (status === 'running' && !warningDetail)) {
    return { label: 'Healthy', tone: 'healthy', problem: false };
  }
  const detail = warningDetail
    || state.payload.status_reason
    || 'System status needs attention';
  if (status === 'paused') {
    // Turned off on purpose: not a failure, but new channels wait until it is turned on.
    return { label: 'Paused', tone: 'neutral', problem: true, reason: shortReason(detail), detail };
  }
  if (status === 'error') {
    return { label: 'Error', tone: 'error', problem: true, reason: shortReason(detail), detail };
  }
  return { label: 'Warning', tone: 'warning', problem: true, reason: shortReason(detail), detail };
}
