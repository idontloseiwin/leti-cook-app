import { useSourceExpiry } from '../sourceLifetimeReact';
import { sourceExpired } from '../sourceLifetime.ts';
import {
  AlertTriangle,
  ArrowLeft,
  Check,
  ChevronDown,
  Copy,
  Folder as FolderIcon,
  FolderPlus,
  MoreHorizontal,
  RefreshCw,
  ShieldCheck,
  Tag,
} from 'lucide-react';
import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from 'react';
import type {
  ChannelExplorerChannel,
  ChannelExplorerCounterDynamicsMetric,
  ChannelExplorerCounterDynamicsPeriodKey,
  ChannelExplorerListInventoryPayload,
  ChannelExplorerProfilePayload,
  CreatedFolder,
  CreatedLabel,
  ExplorerLabel,
  Folder,
} from '../types';
import { YouTubeAttribution } from '../components/YouTubeAttribution';
import {
  isAbortError,
  loadExplorerListInventory,
  loadExplorerProfile,
  requestJson,
} from './api';
import {
  createOverviewClient,
  formatTotalDetail,
  type CounterChangeView,
  type FormatTotalsView,
} from './libraryData/overview';
import {
  formatCount,
  initials,
  youtubeChannelUrl,
  videoNoun,
} from './format';
import { unlistedVideoCount } from './unlistedVideos';
import { formatDateLocal, formatTimestampLocal } from './localTime';
import { copyText } from './clipboard';
import { preferredCounterDynamicsPeriod, presentCounterDynamicsPercent } from './counterDynamics';
import {
  formatChannelAge,
  formatCountryName,
} from './channelProfile';
import { safeExplorerAvatarUrl } from './sidebar';
import { CHANNEL_GONE_LABEL, channelGoneFromYouTube, goneChannelNotice } from './goneChannels';
import type { LibraryShellContext } from './libraryShell';

// Channel header of the Library page: identity, folder and labels, KPI cards
// with 7D/28D change, the notes under the profile banner and the page footer.
// Totals by format and the 7D/28D change are the shared backend's header
// operations; the profile, its notes and freshness stay with the
// profile and list-inventory services.

const headerClient = createOverviewClient(requestJson);

const MISSING_VALUE = '\u2014';

function hasReportedMetric(value: unknown): boolean {
  return value !== undefined && value !== null && value !== '';
}

type ProfileState =
  | { kind: 'loading'; channelId: string }
  | { kind: 'ready'; channelId: string; payload: ChannelExplorerProfilePayload }
  | { kind: 'error'; channelId: string; message: string };

type FormatTotalsState =
  | { kind: 'loading'; channelId: string }
  | { kind: 'ready'; channelId: string; payload: FormatTotalsView; refreshError?: string }
  | { kind: 'error'; channelId: string; message: string };

/** The list-inventory proof of the unlisted note: list counts only. */
type ListInventoryState =
  | { kind: 'loading'; channelId: string }
  | { kind: 'ready'; channelId: string; payload: ChannelExplorerListInventoryPayload }
  | { kind: 'error'; channelId: string };

type CounterDynamicsState =
  | { kind: 'loading'; channelId: string }
  | { kind: 'ready'; channelId: string; payload: CounterChangeView }
  | { kind: 'error'; channelId: string; message: string };

/** Channel folder and labels, owned by the app and passed through the shell. */
export type ChannelOrganizer = {
  folders: Folder[];
  labels: ExplorerLabel[];
  onAssignFolder?: (channelId: string, folderId: string) => Promise<void>;
  onCreateFolder?: (name: string) => Promise<CreatedFolder>;
  onCreateLabel?: (name: string) => Promise<CreatedLabel>;
  onAssignLabel?: (channelId: string, labelKey: string, enabled: boolean) => Promise<void>;
};

/** Stored channel fields of the loaded Videos page: fallback values until the profile responds. */
export type BrowseChannelFallback = { channel?: ChannelExplorerChannel };

function counterDynamicsObservationDetails(metric: ChannelExplorerCounterDynamicsMetric): string {
  const end = metric.observed_end_at;
  if (!end || !Number.isFinite(Date.parse(end))) return '';
  const start = metric.observed_start_at;
  const seconds = Number(metric.actual_interval_seconds);
  if (
    (metric.status !== 'ready' && metric.status !== 'partial' && metric.status !== 'zero_baseline')
    || !start || !Number.isFinite(Date.parse(start)) || Date.parse(end) <= Date.parse(start)
    || !Number.isFinite(seconds) || seconds <= 0
  ) return `Last observation: ${formatTimestampLocal(end)}.`;

  let remaining = Math.floor(seconds);
  const parts: string[] = [];
  for (const [size, unit] of [[86400, 'd'], [3600, 'h'], [60, 'm'], [1, 's']] as const) {
    const count = Math.floor(remaining / size);
    if (count) parts.push(`${count}${unit}`);
    remaining %= size;
  }
  return `Observed: ${formatTimestampLocal(start)} → ${formatTimestampLocal(end)}. Actual interval: ${parts.join(' ') || '<1s'}.`;
}

function CounterDynamicsTrend({
  state,
  metricName,
  label,
  period,
  onPeriodChange,
}: {
  state: CounterDynamicsState;
  metricName: 'subscribers' | 'views';
  label: string;
  period: ChannelExplorerCounterDynamicsPeriodKey;
  onPeriodChange: (period: ChannelExplorerCounterDynamicsPeriodKey) => void;
}) {
  const nextPeriod: ChannelExplorerCounterDynamicsPeriodKey = period === '28d' ? '7d' : '28d';
  const periodDays = period === '7d' ? 7 : 28;
  const periodLabel = period.toUpperCase();
  const toggleLabel = `Show ${nextPeriod.toUpperCase()} ${label.toLowerCase()} change`;
  if (state.kind === 'loading') {
    return (
      <button
        key={period}
        type="button"
        className="explorer-v2-kpi-trend is-loading"
        aria-label={`Loading ${periodDays}-day ${label.toLowerCase()} change. ${toggleLabel}.`}
        onClick={() => onPeriodChange(nextPeriod)}
      >
        <i aria-hidden="true" />
        <small aria-hidden="true">{periodLabel}</small>
      </button>
    );
  }
  if (state.kind === 'error') {
    return (
      <button
        key={period}
        type="button"
        className="explorer-v2-kpi-trend is-unavailable"
        aria-label={`${periodDays}-day ${label.toLowerCase()} change unavailable. ${toggleLabel}.`}
        title={state.message}
        onClick={() => onPeriodChange(nextPeriod)}
      >
        <span aria-hidden="true">—</span>
        <small aria-hidden="true">{periodLabel}</small>
      </button>
    );
  }
  const metric = state.payload.periods[period].metrics[metricName];
  if (sourceExpired(metric)) return <button type="button" className="explorer-v2-kpi-trend is-unavailable"
    aria-label={`${label} change is being refreshed. ${toggleLabel}.`} onClick={() => onPeriodChange(nextPeriod)}>
    <span aria-hidden="true">—</span><small>{periodLabel}</small></button>;
  const partial = metric.coverage === 'partial' || metric.status === 'partial';
  const presentation = presentCounterDynamicsPercent(partial ? {
    ...metric, status: 'not_enough_history',
    reason: `Not enough stored history for the full ${periodDays}-day comparison (partial window).`,
  } : metric);
  const observationDetails = counterDynamicsObservationDetails(metric);
  const trendGlyph = presentation.kind === 'positive'
    ? '▲'
    : presentation.kind === 'negative'
      ? '▼'
      : '';
  const provenance = 'Change calculated by Leti Cook from stored YouTube API snapshots.';
  const context = metricName === 'subscribers'
    ? "Change in YouTube's reported public subscriber counter."
    : 'Change in the channel lifetime view counter; negative values can reflect source corrections.';
  const changeLabel = presentation.kind === 'unavailable'
    ? `Change over ${periodDays} days unavailable.`
    : `${presentation.text} over ${periodDays} days.`;
  return (
    <button
      key={period}
      type="button"
      className={`explorer-v2-kpi-trend is-${presentation.kind}`}
      aria-label={`${label}: ${changeLabel} ${toggleLabel}.`}
      title={[context, provenance, presentation.unavailableReason, observationDetails, `Click to show ${nextPeriod.toUpperCase()}.`].filter(Boolean).join('\n')}
      onClick={() => onPeriodChange(nextPeriod)}
    >
      <span aria-hidden="true">{trendGlyph ? `${trendGlyph} ` : ''}{presentation.text}</span>
      <small aria-hidden="true">{periodLabel}</small>
    </button>
  );
}

/** State and requests of the header, footer and channel notes (called by the shell). */
export function useChannelHeader(
  context: LibraryShellContext,
  organizer: ChannelOrganizer,
  rawPayload: BrowseChannelFallback | undefined,
) {
  const {
    channelId, channel, timeZone, videoType,
    profileReloadToken, setProfileReloadToken,
    formatTotalsReloadToken, setFormatTotalsReloadToken, listInventoryReloadToken,
    counterDynamicsReloadToken, setCounterDynamicsReloadToken,
    setAnnouncement,
  } = context;
  const { folders, labels, onAssignFolder, onCreateFolder, onCreateLabel, onAssignLabel } = organizer;
  const [avatarFailed, setAvatarFailed] = useState(false);
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const [folderDraftId, setFolderDraftId] = useState(channel?.folder_id || '');
  const [folderSaving, setFolderSaving] = useState(false);
  const [folderError, setFolderError] = useState('');
  const [folderCreationOpen, setFolderCreationOpen] = useState(false);
  const [folderName, setFolderName] = useState('');
  const [labelName, setLabelName] = useState('');
  const [labelSavingKey, setLabelSavingKey] = useState('');
  const [labelError, setLabelError] = useState('');
  const [profileState, setProfileState] = useState<ProfileState>({ kind: 'loading', channelId });
  const [formatTotalsState, setFormatTotalsState] = useState<FormatTotalsState>(
    { kind: 'loading', channelId },
  );
  const [listInventoryState, setListInventoryState] = useState<ListInventoryState>(
    { kind: 'loading', channelId },
  );
  // Expiring list evidence refreshes independently of totals.
  const [listInventoryReread, setListInventoryReread] = useState(0);
  const [counterDynamicsState, setCounterDynamicsState] = useState<CounterDynamicsState>(
    { kind: 'loading', channelId },
  );
  const [counterDynamicsPeriodChoices, setCounterDynamicsPeriodChoices] = useState<{
    channelId: string;
    subscribers: ChannelExplorerCounterDynamicsPeriodKey | null;
    views: ChannelExplorerCounterDynamicsPeriodKey | null;
  }>({ channelId, subscribers: null, views: null });
  useEffect(() => {
    setCounterDynamicsPeriodChoices({ channelId, subscribers: null, views: null });
  }, [channelId]);
  const chooseCounterDynamicsPeriod = (metricName: 'subscribers' | 'views', period: ChannelExplorerCounterDynamicsPeriodKey) => {
    setCounterDynamicsPeriodChoices((current) => ({
      channelId,
      subscribers: current.channelId === channelId ? current.subscribers : null,
      views: current.channelId === channelId ? current.views : null,
      [metricName]: period,
    }));
  };
  const [, repaintSources] = useState(0);
  useSourceExpiry(profileState, () => setProfileState({ kind: 'loading', channelId }), () => setProfileReloadToken(value => value + 1));
  useSourceExpiry(formatTotalsState, () => repaintSources(value => value + 1), () => setFormatTotalsReloadToken(value => value + 1));
  useSourceExpiry(listInventoryState, () => repaintSources(value => value + 1), () => setListInventoryReread(value => value + 1));
  useSourceExpiry(counterDynamicsState, () => repaintSources(value => value + 1), () => setCounterDynamicsReloadToken(value => value + 1));
  const copyResetRef = useRef<number | undefined>(undefined);
  const folderMenuRef = useRef<HTMLDetailsElement | null>(null);
  const labelMenuRef = useRef<HTMLDetailsElement | null>(null);
  const operationsMenuRef = useRef<HTMLDetailsElement | null>(null);

  useEffect(() => {
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!(event.target instanceof Node)) return;
      const folderMenu = folderMenuRef.current;
      if (folderMenu?.open && !folderMenu.contains(event.target)) {
        folderMenu.open = false;
        setFolderCreationOpen(false);
      }
      const labelMenu = labelMenuRef.current;
      if (labelMenu?.open && !labelMenu.contains(event.target)) {
        labelMenu.open = false;
      }
      const operationsMenu = operationsMenuRef.current;
      if (operationsMenu?.open && !operationsMenu.contains(event.target)) {
        operationsMenu.open = false;
      }
    };
    document.addEventListener('pointerdown', closeOnOutsidePointer);
    return () => document.removeEventListener('pointerdown', closeOnOutsidePointer);
  }, []);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const requestedChannelId = channelId;
    setProfileState({ kind: 'loading', channelId: requestedChannelId });
    void loadExplorerProfile(requestedChannelId, controller.signal)
      .then((payload) => {
        if (!cancelled) setProfileState({ kind: 'ready', channelId: requestedChannelId, payload });
      })
      .catch((error) => {
        if (cancelled || isAbortError(error)) return;
        setProfileState({
          kind: 'error',
          channelId: requestedChannelId,
          message: error instanceof Error ? error.message : String(error),
        });
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [channelId, profileReloadToken]);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const requestedChannelId = channelId;
    setFormatTotalsState(previous => previous.kind === 'ready' && previous.channelId === requestedChannelId ? previous : { kind: 'loading', channelId: requestedChannelId });
    void headerClient.formatTotals(requestedChannelId, controller.signal)
      .then((payload) => {
        if (cancelled) return;
        setFormatTotalsState({
          kind: 'ready', channelId: requestedChannelId, payload,
        });
      })
      .catch((error) => {
        if (cancelled || isAbortError(error)) return;
        setFormatTotalsState({
          kind: 'error',
          channelId: requestedChannelId,
          message: error instanceof Error ? error.message : String(error),
        });
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [channelId, formatTotalsReloadToken]);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const requestedChannelId = channelId;
    setListInventoryState(previous => previous.kind === 'ready' && previous.channelId === requestedChannelId ? previous : { kind: 'loading', channelId: requestedChannelId });
    void loadExplorerListInventory(requestedChannelId, controller.signal)
      .then((payload) => {
        if (!cancelled) setListInventoryState({ kind: 'ready', channelId: requestedChannelId, payload });
      })
      .catch((error) => {
        // Coverage errors are reported separately while the catalogue remains readable.
        if (cancelled || isAbortError(error)) return;
        setListInventoryState({ kind: 'error', channelId: requestedChannelId });
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [channelId, listInventoryReloadToken, listInventoryReread]);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const requestedChannelId = channelId;
    setCounterDynamicsState(previous => previous.kind === 'ready' && previous.channelId === requestedChannelId ? previous : { kind: 'loading', channelId: requestedChannelId });
    void headerClient.counterChange(requestedChannelId, controller.signal)
      .then((payload) => {
        if (cancelled) return;
        setCounterDynamicsState({ kind: 'ready', channelId: requestedChannelId, payload });
      })
      .catch((error) => {
        if (cancelled || isAbortError(error)) return;
        setCounterDynamicsState({
          kind: 'error',
          channelId: requestedChannelId,
          message: error instanceof Error ? error.message : String(error),
        });
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [channelId, counterDynamicsReloadToken]);

  useEffect(() => {
    setFolderDraftId(channel?.folder_id || '');
    setFolderError('');
  }, [channel?.folder_id, channelId]);

  useEffect(() => () => window.clearTimeout(copyResetRef.current), []);

  const visibleProfileState: ProfileState = profileState.channelId === channelId
    ? profileState
    : { kind: 'loading', channelId };
  const profilePayload = visibleProfileState.kind === 'ready'
    && !sourceExpired(visibleProfileState.payload)
    ? visibleProfileState.payload
    : undefined;
  const formatTotalsPayload = formatTotalsState.kind === 'ready'
    && formatTotalsState.channelId === channelId
    ? formatTotalsState.payload
    : undefined;
  // A channel YouTube no longer returns has no current title: name it plainly (docs/DATA.md).
  const gone = channelGoneFromYouTube(channel);
  const untitled = gone ? CHANNEL_GONE_LABEL : channelId;
  const resolvedTitle = profilePayload
    ? profilePayload.channel.title || untitled
    : rawPayload?.channel?.title || channel?.title || untitled;
  const goneNotice = gone && channel ? goneChannelNotice(channel) : '';
  const avatarUrl = safeExplorerAvatarUrl(channel?.avatar_url);
  const subscriberCount = profilePayload
    ? profilePayload.channel.subscribers
    : rawPayload ? rawPayload.channel?.subscribers : channel?.subscribers;
  const channelViewCount = profilePayload
    ? profilePayload.channel.views
    : rawPayload ? rawPayload.channel?.views : channel?.views;
  const longFormat = sourceExpired(formatTotalsPayload?.formats.long) ? undefined : formatTotalsPayload?.formats.long;
  const listInventoryPayload = listInventoryState.kind === 'ready' && listInventoryState.channelId === channelId
    ? listInventoryState.payload : undefined;
  const unlistedVideos = unlistedVideoCount(
    sourceExpired(listInventoryPayload?.inventory) ? undefined : listInventoryPayload,
  );
  const shortFormat = sourceExpired(formatTotalsPayload?.formats.short) ? undefined : formatTotalsPayload?.formats.short;
  const streamFormat = sourceExpired(formatTotalsPayload?.formats.stream) ? undefined : formatTotalsPayload?.formats.stream;
  const typedViewsLoading = formatTotalsState.kind === 'loading'
    || formatTotalsState.channelId !== channelId;
  const formatTotalsError = formatTotalsState.kind === 'error'
    && formatTotalsState.channelId === channelId
    ? formatTotalsState.message
    : '';
  const countryCode = profilePayload
    ? profilePayload.channel.country_code
    : rawPayload ? rawPayload.channel?.country_code : channel?.country_code;
  const channelCreatedAt = profilePayload
    ? profilePayload.channel.channel_created_at
    : channel?.channel_created_at;
  const metadataUpdatedAt = profilePayload
    ? profilePayload.channel.channel_metadata_checked_at
    : rawPayload
      ? rawPayload.channel?.channel_metadata_checked_at
      : channel?.channel_metadata_checked_at;
  useEffect(() => {
    setAvatarFailed(false);
  }, [avatarUrl]);

  const handleCopyChannelId = async () => {
    const copied = await copyText(channelId);
    setCopyState(copied ? 'copied' : 'failed');
    setAnnouncement(copied ? 'Channel ID copied.' : 'Channel ID could not be copied.');
    window.clearTimeout(copyResetRef.current);
    copyResetRef.current = window.setTimeout(() => setCopyState('idle'), 1600);
  };

  const closeFolderMenu = () => {
    if (folderMenuRef.current) folderMenuRef.current.open = false;
    setFolderCreationOpen(false);
  };

  const closeLabelMenu = () => {
    if (labelMenuRef.current) labelMenuRef.current.open = false;
  };

  const handleFolderChange = async (folderId: string): Promise<boolean> => {
    if (!onAssignFolder || folderSaving) return false;
    if (folderId === folderDraftId) return true;
    const previousFolderId = folderDraftId;
    setFolderDraftId(folderId);
    setFolderSaving(true);
    setFolderError('');
    try {
      await onAssignFolder(channelId, folderId);
      const selectedFolder = folders.find((folder) => folder.id === folderId);
      setAnnouncement(
        folderId
          ? `Channel moved to ${selectedFolder?.name || 'the selected folder'}.`
          : 'Channel removed from its folder.',
      );
      return true;
    } catch (error) {
      setFolderDraftId(previousFolderId);
      setFolderError(error instanceof Error ? error.message : String(error));
      setAnnouncement('Folder assignment failed.');
      return false;
    } finally {
      setFolderSaving(false);
    }
  };

  const handleFolderSelection = async (folderId: string) => {
    if (await handleFolderChange(folderId)) closeFolderMenu();
  };

  const handleCreateFolder = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const name = folderName.trim();
    if (!name || !onCreateFolder || !onAssignFolder || folderSaving) return;
    let createdFolder: CreatedFolder | undefined;
    setFolderSaving(true);
    setFolderError('');
    try {
      createdFolder = await onCreateFolder(name);
      const createdFolderId = String(createdFolder.id || '').trim();
      if (!createdFolderId) throw new Error('The created folder did not return an id.');
      await onAssignFolder(channelId, createdFolderId);
      setFolderDraftId(createdFolderId);
      setFolderName('');
      closeFolderMenu();
      // A name that already exists reuses that folder instead of making a second one.
      setAnnouncement(createdFolder.created === false
        ? `Moved to the existing ${createdFolder.name || name} folder.`
        : `${createdFolder.name || name} folder created and assigned.`);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setFolderError(
        createdFolder?.id
          ? `Folder created, but the channel could not be assigned. ${message}`
          : message,
      );
      setAnnouncement(createdFolder?.id ? 'Folder assignment failed.' : 'Folder creation failed.');
    } finally {
      setFolderSaving(false);
    }
  };

  const assignedLabelKeys = new Set(channel?.label_keys ?? []);
  const assignedLabels = labels.filter((label) => assignedLabelKeys.has(label.label_key));
  const visibleAssignedLabels = assignedLabels.slice(0, 2);
  const hiddenAssignedLabelCount = Math.max(0, assignedLabels.length - visibleAssignedLabels.length);
  const selectedFolderName = folderDraftId
    ? folders.find((folder) => folder.id === folderDraftId)?.name || 'Unknown folder'
    : 'No folder';

  const handleLabelToggle = async (labelKey: string, enabled: boolean) => {
    if (!onAssignLabel || labelSavingKey) return;
    setLabelSavingKey(labelKey);
    setLabelError('');
    try {
      await onAssignLabel(channelId, labelKey, enabled);
      setAnnouncement(enabled ? 'Label added.' : 'Label removed.');
    } catch (error) {
      setLabelError(error instanceof Error ? error.message : String(error));
      setAnnouncement('Label update failed.');
    } finally {
      setLabelSavingKey('');
    }
  };

  const handleCreateLabel = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const name = labelName.trim();
    if (!name || !onCreateLabel || !onAssignLabel || labelSavingKey) return;
    setLabelSavingKey('creating');
    setLabelError('');
    try {
      const label = await onCreateLabel(name);
      await onAssignLabel(channelId, label.label_key, true);
      setLabelName('');
      setAnnouncement(label.created === false
        ? `The existing ${label.name} label was added.`
        : `${label.name} label created and added.`);
    } catch (error) {
      setLabelError(error instanceof Error ? error.message : String(error));
      setAnnouncement('Label creation failed.');
    } finally {
      setLabelSavingKey('');
    }
  };

  const visibleCounterDynamicsState: CounterDynamicsState = counterDynamicsState.channelId === channelId
    ? counterDynamicsState
    : { kind: 'loading', channelId };
  const availableCounterDynamicsPeriods = visibleCounterDynamicsState.kind === 'ready'
    ? visibleCounterDynamicsState.payload.periods : undefined;
  const counterDynamicsPeriods = {
    subscribers: (counterDynamicsPeriodChoices.channelId === channelId ? counterDynamicsPeriodChoices.subscribers : null)
      ?? preferredCounterDynamicsPeriod(availableCounterDynamicsPeriods, 'subscribers'),
    views: (counterDynamicsPeriodChoices.channelId === channelId ? counterDynamicsPeriodChoices.views : null)
      ?? preferredCounterDynamicsPeriod(availableCounterDynamicsPeriods, 'views'),
  };
  const profileFresh = visibleProfileState.kind === 'ready'
    && visibleProfileState.payload.freshness.channel_data_fresh;
  const dateLabel = (value: string | null | undefined) => value
    ? new Intl.DateTimeFormat('en', { dateStyle: 'medium', timeStyle: 'short', timeZone }).format(new Date(value)) + ' (' + timeZone + ')'
    : 'unknown';
  const listDates: string[] = [];
  const currentLibrary = listInventoryPayload?.library;
  let coverageMessage = (listInventoryState.kind === 'error' && listInventoryState.channelId === channelId) || (listInventoryPayload && !currentLibrary)
    ? 'List coverage could not be loaded. Reload to check again.' : '';
  if (currentLibrary) {
    const names = { long: 'Long-form', short: 'Shorts', stream: 'Streams' } as const;
    const ready = currentLibrary.status === 'ready' && Object.values(currentLibrary.formats).every(
      format => !format.source_expires_at || !sourceExpired(format),
    );
    listDates.push(ready ? 'Channel coverage: ready' : 'Channel coverage: partial');
    for (const kind of ['long', 'short', 'stream'] as const) {
      const format = currentLibrary.formats[kind];
      if (format.status === 'turned_off') { listDates.push(names[kind] + ': turned off'); continue; }
      const expired = Boolean(format.source_expires_at) && sourceExpired(format);
      const full = !expired && format.full_completed_at ? dateLabel(format.full_completed_at) : 'unconfirmed';
      listDates.push(names[kind] + ' last full check: ' + full);
      if (!expired && format.absent_checked_at) listDates.push(names[kind] + ' public list absent at ' + dateLabel(format.absent_checked_at));
      if (kind === videoType) {
        const status = expired ? 'partial' : format.status;
        coverageMessage = {
          complete: '', absent: 'No public ' + names[kind] + ' list at the last check.',
          checking: 'List check in progress.', check_failed: 'The last list check failed.',
          waiting_for_quota: 'The list check is waiting for quota.', partial: 'Full list coverage is unconfirmed.',
          not_checked: 'No completed list check yet.',
        }[status];
      }
    }
  }
  const rawFormat = currentLibrary?.formats[videoType];
  const currentFormat = rawFormat?.manual_check && rawFormat.source_expires_at && sourceExpired(rawFormat)
    ? { ...rawFormat, status: 'partial' as const, manual_check: { ...rawFormat.manual_check, estimated_general: null } }
    : rawFormat;
  const profileStatusTitle = visibleProfileState.kind === 'loading'
    ? 'Checking stored channel data'
    : visibleProfileState.kind === 'error'
      ? 'Stored profile check failed'
      : profileFresh
        ? `Channel profile checked ${dateLabel(metadataUpdatedAt)}`
        : `Stored profile checked ${dateLabel(metadataUpdatedAt)}`;
  const profileStatusDetail = visibleProfileState.kind === 'error'
    ? `Header totals use fallback stored values. ${visibleProfileState.message}`
    : visibleProfileState.kind === 'loading'
      ? 'Fallback values remain marked until the stored profile responds.'
      : `Freshness limit: ${visibleProfileState.payload.freshness.maximum_age_days} days.`;
  // Any header read still running (the shell's Reload control waits for it).
  const loading = visibleProfileState.kind === 'loading'
    || typedViewsLoading
    || (listInventoryState.kind === 'loading' && listInventoryState.channelId === channelId)
    || visibleCounterDynamicsState.kind === 'loading';

  return {
    channelId, folders, labels, onAssignFolder, onCreateFolder, onCreateLabel, onAssignLabel,
    setProfileReloadToken, setFormatTotalsReloadToken,
    avatarFailed, setAvatarFailed, copyState, folderDraftId, folderSaving, folderError, setFolderError,
    folderCreationOpen, setFolderCreationOpen, folderName, setFolderName, labelName, setLabelName,
    labelSavingKey, labelError, chooseCounterDynamicsPeriod, folderMenuRef, labelMenuRef, operationsMenuRef,
    visibleProfileState, profilePayload, resolvedTitle, avatarUrl, subscriberCount, channelViewCount,
    longFormat, unlistedVideos, shortFormat, streamFormat, typedViewsLoading,
    formatTotalsError, countryCode, channelCreatedAt,
    handleCopyChannelId, closeFolderMenu, closeLabelMenu, handleFolderSelection, handleCreateFolder,
    assignedLabelKeys, assignedLabels, visibleAssignedLabels, hiddenAssignedLabelCount, selectedFolderName,
    handleLabelToggle, handleCreateLabel, visibleCounterDynamicsState, counterDynamicsPeriods,
    profileFresh, profileStatusTitle, profileStatusDetail, loading, goneNotice, listDates, coverageMessage,
    currentFormat,
  };
}

export type ChannelHeaderModel = ReturnType<typeof useChannelHeader>;

export function ChannelHeader({ header }: { header: ChannelHeaderModel }) {
  const {
    channelId, folders, labels, onAssignFolder, onCreateFolder, onCreateLabel, onAssignLabel,
    setFormatTotalsReloadToken, avatarFailed, setAvatarFailed, folderDraftId, folderSaving, folderError,
    setFolderError, folderCreationOpen, setFolderCreationOpen, folderName, setFolderName, labelName,
    setLabelName, labelSavingKey, labelError, chooseCounterDynamicsPeriod, folderMenuRef, labelMenuRef,
    visibleProfileState, resolvedTitle, avatarUrl, subscriberCount, channelViewCount, longFormat,
    shortFormat, streamFormat, typedViewsLoading, formatTotalsError,
    countryCode, channelCreatedAt, closeFolderMenu, closeLabelMenu,
    handleFolderSelection, handleCreateFolder, assignedLabelKeys, assignedLabels, visibleAssignedLabels,
    hiddenAssignedLabelCount, selectedFolderName, handleLabelToggle, handleCreateLabel,
    visibleCounterDynamicsState, counterDynamicsPeriods,
  } = header;
  return (
      <header className="explorer-channel-header explorer-channel-profile explorer-v2-channel-hero">
        <div className="explorer-v2-profile-main">
          <a
            className="explorer-avatar explorer-profile-avatar"
            href={youtubeChannelUrl(channelId)}
            target="_blank"
            rel="noreferrer"
            aria-label={`Open ${resolvedTitle} on YouTube`}
          >
            {avatarUrl && !avatarFailed ? (
              <img src={avatarUrl} alt="" referrerPolicy="no-referrer" onError={() => setAvatarFailed(true)} />
            ) : initials(resolvedTitle)}
          </a>
          <div className="explorer-channel-identity">
            <span className="explorer-eyebrow">Channel Explorer</span>
            <h1>{resolvedTitle}</h1>
            <div className="explorer-channel-meta" aria-label="Channel metadata">
              <span>{formatCountryName(countryCode)}</span>
              <span title={channelCreatedAt ? `Created ${formatDateLocal(channelCreatedAt)}` : undefined}>
                {formatChannelAge(channelCreatedAt)}
              </span>
              <section className="explorer-v2-hero-organizer" aria-label={`Organize ${resolvedTitle}`}>
                <div className="explorer-folder-control">
                  <details
                    ref={folderMenuRef}
                    className="explorer-folder-menu"
                    onToggle={(event) => {
                      if (event.currentTarget.open) closeLabelMenu();
                      else setFolderCreationOpen(false);
                    }}
                    onKeyDown={(event) => {
                      if (event.key !== 'Escape') return;
                      event.preventDefault();
                      closeFolderMenu();
                      folderMenuRef.current?.querySelector<HTMLElement>('summary')?.focus();
                    }}
                  >
                    <summary
                      aria-label={`Folder for ${resolvedTitle}: ${selectedFolderName}`}
                      aria-disabled={!onAssignFolder || folderSaving}
                      onClick={(event) => {
                        if (!onAssignFolder || folderSaving) event.preventDefault();
                      }}
                    >
                      <span className="explorer-organizer-action-label">
                        <FolderIcon size={14} aria-hidden="true" />
                        <span>Folder</span>
                      </span>
                      <span className="explorer-organizer-current-value" title={selectedFolderName}>
                        {selectedFolderName}
                      </span>
                      <ChevronDown size={13} aria-hidden="true" />
                    </summary>
                    <div className="explorer-folder-popover">
                      {!folderCreationOpen ? (
                        <button
                          type="button"
                          className="explorer-folder-create-trigger"
                          disabled={!onCreateFolder || !onAssignFolder || folderSaving}
                          onClick={() => {
                            setFolderError('');
                            setFolderCreationOpen(true);
                          }}
                        >
                          <FolderPlus size={14} aria-hidden="true" />
                          <span>Add new folder</span>
                        </button>
                      ) : (
                        <form
                          className="explorer-folder-create-form"
                          onSubmit={(event) => void handleCreateFolder(event)}
                        >
                          <input
                            autoFocus
                            value={folderName}
                            onChange={(event) => setFolderName(event.target.value)}
                            maxLength={80}
                            placeholder="New folder"
                            aria-label="New folder name"
                          />
                          <button type="submit" disabled={!folderName.trim() || folderSaving}>
                            {folderSaving ? 'Adding...' : 'Add'}
                          </button>
                          <button
                            type="button"
                            className="explorer-folder-create-cancel"
                            disabled={folderSaving}
                            onClick={() => {
                              setFolderName('');
                              setFolderCreationOpen(false);
                            }}
                          >
                            Cancel
                          </button>
                        </form>
                      )}
                      <div className="explorer-folder-options" aria-label="Available folders">
                        <button
                          type="button"
                          className={!folderDraftId ? 'is-active' : ''}
                          disabled={!onAssignFolder || folderSaving}
                          onClick={() => void handleFolderSelection('')}
                        >
                          <span>No folder</span>
                          {!folderDraftId && <Check size={12} aria-hidden="true" />}
                        </button>
                        {folders.map((folder) => folder.id ? (
                          <button
                            type="button"
                            key={folder.id}
                            className={folder.id === folderDraftId ? 'is-active' : ''}
                            disabled={!onAssignFolder || folderSaving}
                            onClick={() => void handleFolderSelection(folder.id || '')}
                          >
                            <span>{folder.name || 'Untitled folder'}</span>
                            {folder.id === folderDraftId && <Check size={12} aria-hidden="true" />}
                          </button>
                        ) : null)}
                      </div>
                    </div>
                  </details>
                  {folderSaving && <small>Saving…</small>}
                  {folderError && <small className="is-error">{folderError}</small>}
                </div>

                <div className="explorer-label-editor" aria-label="Channel labels">
                  <details
                    ref={labelMenuRef}
                    className={`explorer-label-menu ${assignedLabels.length > 0 ? 'has-labels' : 'is-empty'}`}
                    onToggle={(event) => {
                      if (event.currentTarget.open) closeFolderMenu();
                    }}
                    onKeyDown={(event) => {
                      if (event.key !== 'Escape') return;
                      event.preventDefault();
                      closeLabelMenu();
                      labelMenuRef.current?.querySelector<HTMLElement>('summary')?.focus();
                    }}
                  >
                    <summary aria-label={`Manage channel labels (${assignedLabels.length} assigned)`}>
                      {assignedLabels.length > 0 ? (
                        <>
                          {visibleAssignedLabels.map((label) => (
                            <span key={label.label_key} className="explorer-label-chip">{label.name}</span>
                          ))}
                          {hiddenAssignedLabelCount > 0 && (
                            <span
                              className="explorer-label-overflow"
                              title={assignedLabels.slice(2).map((label) => label.name).join(', ')}
                            >
                              +{hiddenAssignedLabelCount}
                            </span>
                          )}
                        </>
                      ) : (
                        <>
                          <Tag size={13} aria-hidden="true" />
                          <span>Add label</span>
                        </>
                      )}
                    </summary>
                    <div className="explorer-label-popover">
                      {labels.length > 0 && (
                        <div className="explorer-label-options" aria-label="Available labels">
                          {labels.map((label) => {
                            const enabled = assignedLabelKeys.has(label.label_key);
                            return (
                              <button
                                type="button"
                                key={label.label_key}
                                className={enabled ? 'is-active' : ''}
                                aria-pressed={enabled}
                                disabled={!onAssignLabel || Boolean(labelSavingKey)}
                                onClick={() => void handleLabelToggle(label.label_key, !enabled)}
                              >
                                <span>{label.name}</span>
                                {enabled && <Check size={12} aria-hidden="true" />}
                              </button>
                            );
                          })}
                        </div>
                      )}
                      <form onSubmit={(event) => void handleCreateLabel(event)}>
                        <input
                          value={labelName}
                          onChange={(event) => setLabelName(event.target.value)}
                          maxLength={40}
                          placeholder="New label"
                          aria-label="New custom label"
                        />
                        <button
                          type="submit"
                          disabled={!labelName.trim() || !onCreateLabel || Boolean(labelSavingKey)}
                        >
                          {labelSavingKey === 'creating' ? 'Adding…' : 'Add'}
                        </button>
                      </form>
                    </div>
                  </details>
                  {labelError && <small className="is-error">{labelError}</small>}
                </div>
              </section>
            </div>
          </div>
        </div>
        <section
          className="explorer-v2-hero-kpis"
          aria-label="Channel source totals by format"
          aria-busy={
            typedViewsLoading
            || visibleProfileState.kind === 'loading'
            || visibleCounterDynamicsState.kind === 'loading'
          }
        >
          <div className="explorer-channel-kpis explorer-v2-kpi-grid" aria-label="Channel and stored format view totals">
            <article className="explorer-v2-kpi-card">
              <div className="explorer-v2-kpi-heading">
                <span>Subscribers</span>
                <small className="explorer-v2-kpi-source" title="YouTube API">YouTube API</small>
              </div>
              <div className="explorer-v2-kpi-value-row">
                <strong>{hasReportedMetric(subscriberCount) ? formatCount(subscriberCount) : MISSING_VALUE}</strong>
                <CounterDynamicsTrend
                  state={visibleCounterDynamicsState}
                  metricName="subscribers"
                  label="Subscribers"
                  period={counterDynamicsPeriods.subscribers}
                  onPeriodChange={(period) => chooseCounterDynamicsPeriod('subscribers', period)}
                />
              </div>
            </article>
            <article className="explorer-v2-kpi-card">
              <div className="explorer-v2-kpi-heading">
                <span>Total views</span>
                <small className="explorer-v2-kpi-source" title="YouTube API">YouTube API</small>
              </div>
              <div className="explorer-v2-kpi-value-row">
                <strong>{hasReportedMetric(channelViewCount) ? formatCount(channelViewCount) : MISSING_VALUE}</strong>
                <CounterDynamicsTrend
                  state={visibleCounterDynamicsState}
                  metricName="views"
                  label="Total views"
                  period={counterDynamicsPeriods.views}
                  onPeriodChange={(period) => chooseCounterDynamicsPeriod('views', period)}
                />
              </div>
            </article>
            {([
              { key: 'long', label: 'Videos views', note: 'long-form', format: longFormat },
              { key: 'short', label: 'Shorts views', note: 'short-form', format: shortFormat },
              { key: 'stream', label: 'Live views', note: 'live', format: streamFormat },
            ] as const).map(({ key, label, note, format }) => (
              <article className="explorer-v2-kpi-card" key={key}>
                <div className="explorer-v2-kpi-heading">
                  <span>{label}</span>
                  <small className="explorer-v2-kpi-source is-product" title="Computed by product analysis over stored videos">Analysis</small>
                </div>
                <div className="explorer-v2-kpi-value-row">
                  <strong>
                    {typedViewsLoading ? (
                      <span className="explorer-v2-kpi-value-skeleton" aria-label={`Loading current ${label.toLowerCase()}`} />
                    ) : format?.status === 'ready' ? formatCount(format.views) : MISSING_VALUE}
                  </strong>
                  {!typedViewsLoading && (
                    formatTotalsError ? (
                      <button
                        type="button"
                        className="explorer-v2-kpi-inline-retry"
                        title={formatTotalsError}
                        onClick={() => setFormatTotalsReloadToken((value) => value + 1)}
                      >
                        <RefreshCw size={11} aria-hidden="true" />
                        Retry
                      </button>
                    ) : (
                      <small
                        className={format?.status === 'ready' ? 'explorer-v2-kpi-format-note' : 'explorer-v2-kpi-state'}
                        title={formatTotalDetail(format) || undefined}
                      >
                        {format?.status === 'ready' ? note
                          : format?.status === 'absent' ? 'No public playlist'
                          : format?.status === 'turned_off' ? 'Turned off'
                            : format?.status === 'stale' ? 'Needs refresh'
                              : format?.status === 'incomplete' ? 'Incomplete' : 'Not collected'}
                      </small>
                    )
                  )}
                </div>
              </article>
            ))}
          </div>
        </section>
      </header>
  );
}

/** Notes under the profile banner: public video count and unlisted difference. */
export function ChannelNotes({ header }: { header: ChannelHeaderModel }) {
  const { profilePayload, unlistedVideos, goneNotice } = header;
  return (
    <>
      {goneNotice ? (
        <p className="explorer-v2-unlisted-note explorer-channel-gone-note" role="note">
          <AlertTriangle size={14} aria-hidden="true" /> {goneNotice}
        </p>
      ) : null}
      {profilePayload?.public_video_count_notice
        && !sourceExpired(profilePayload.public_video_count_notice) ? (
          <p className="explorer-v2-unlisted-note" role="note">
            Public video count decreased since the previous check.
          </p>
        ) : null}
      {unlistedVideos ? (
        <p className="explorer-v2-unlisted-note" role="note">
          YouTube counts {formatCount(unlistedVideos, { compact: false })} more {videoNoun(unlistedVideos)} for this
          {' '}channel than its public lists contain. The difference may include members-only videos or new uploads
          {' '}awaiting the next collection pass; it does not identify which videos are missing.
        </p>
      ) : null}
    </>
  );
}

/** Page footer: breadcrumb, profile presence, YouTube link and the channel actions menu. */
export function ChannelFooter({
  header,
  onBack,
  reloadCurrentView,
  reloadBusy,
  overviewVisible,
  coverage,
}: {
  header: ChannelHeaderModel;
  onBack: () => void;
  reloadCurrentView: () => void;
  reloadBusy: boolean;
  overviewVisible: boolean;
  /** Overview sample coverage, shown in the actions menu (owned by the Overview section). */
  coverage: ReactNode;
}) {
  const {
    channelId, setProfileReloadToken, copyState, operationsMenuRef, visibleProfileState, resolvedTitle,
    handleCopyChannelId, profileFresh, profileStatusTitle, profileStatusDetail,
  } = header;
  return (
      <footer className="explorer-page-controls explorer-v2-topbar">
        <button
          type="button"
          className="explorer-back-button explorer-v2-breadcrumb"
          onClick={onBack}
          aria-label={`Back to channels from ${resolvedTitle}`}
        >
          <ArrowLeft size={16} aria-hidden="true" />
          <span>Channels</span>
          <small aria-hidden="true">/</small>
          <strong>{resolvedTitle}</strong>
        </button>
        <div className="explorer-v2-topbar-actions">
          <div
            className={`explorer-v2-profile-presence is-${visibleProfileState.kind === 'ready' ? profileFresh ? 'current' : 'stale' : visibleProfileState.kind}`}
            role={visibleProfileState.kind === 'error' ? 'alert' : 'status'}
          >
            <span className="explorer-v2-status-dot" aria-hidden="true" />
            <div>
              <div>{profileStatusTitle}</div>
              {header.listDates.map(line => <div key={line}>{line}</div>)}
            </div>
          </div>
          <p className="explorer-v2-footer-note">
            Movement uses stored observations. Missing coverage stays unavailable rather than being estimated.
          </p>
          <div className="explorer-v2-footer-actions">
            <div className="explorer-v2-youtube-link">
              <YouTubeAttribution
                href={youtubeChannelUrl(channelId)}
                label={`Open ${resolvedTitle} on YouTube`}
              />
            </div>
            <details
              ref={operationsMenuRef}
              className="explorer-v2-operations"
              onKeyDown={(event) => {
                if (event.key !== 'Escape') return;
                event.preventDefault();
                const menu = operationsMenuRef.current;
                if (!menu) return;
                menu.open = false;
                menu.querySelector<HTMLElement>('summary')?.focus();
              }}
            >
            <summary aria-label="More channel actions">
              <MoreHorizontal size={19} aria-hidden="true" />
              <span className="explorer-sr-only">More channel actions</span>
            </summary>
            <div className="explorer-v2-operations-popover">
              <section className="explorer-v2-operation-section" aria-labelledby="explorer-v2-actions-title">
                <div className="explorer-v2-operation-heading">
                  <div>
                    <span className="explorer-eyebrow">Workspace</span>
                    <h2 id="explorer-v2-actions-title">Channel actions</h2>
                  </div>
                </div>
                <div className="explorer-v2-operation-actions">
                  <button
                    type="button"
                    className={`explorer-secondary-button explorer-copy-profile-id is-${copyState}`}
                    onClick={() => void handleCopyChannelId()}
                    aria-label={`Copy channel ID for ${resolvedTitle}`}
                  >
                    {copyState === 'copied'
                      ? <Check size={14} aria-hidden="true" />
                      : <Copy size={14} aria-hidden="true" />}
                    {copyState === 'copied'
                      ? 'Copied'
                      : copyState === 'failed' ? 'Retry copy' : 'Copy channel ID'}
                  </button>
                  <button
                    type="button"
                    className="explorer-secondary-button explorer-v2-reload"
                    onClick={reloadCurrentView}
                    disabled={reloadBusy}
                    aria-label={overviewVisible ? 'Reload profile' : 'Reload current'}
                  >
                    <RefreshCw className={reloadBusy ? 'explorer-spin' : ''} size={16} aria-hidden="true" />
                    <span>{overviewVisible ? 'Reload profile' : 'Reload current'}</span>
                  </button>
                </div>

              </section>

              <section
                className={`explorer-profile-status explorer-v2-operation-section is-${visibleProfileState.kind === 'ready' ? profileFresh ? 'current' : 'stale' : visibleProfileState.kind}`}
                aria-labelledby="explorer-v2-data-status-title"
              >
                {visibleProfileState.kind === 'ready' && profileFresh
                  ? <ShieldCheck size={16} aria-hidden="true" />
                  : visibleProfileState.kind === 'loading'
                    ? <RefreshCw className="explorer-spin" size={16} aria-hidden="true" />
                    : <AlertTriangle size={16} aria-hidden="true" />}
                <div>
                  <strong id="explorer-v2-data-status-title">{profileStatusTitle}</strong>
                  <span>{profileStatusDetail}</span>
                </div>
                {visibleProfileState.kind === 'error' && (
                  <button type="button" onClick={() => setProfileReloadToken((value) => value + 1)}>Retry profile</button>
                )}
              </section>

              {coverage}

            </div>
            </details>
          </div>
        </div>
      </footer>
  );
}
