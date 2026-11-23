import type { Dispatch, SetStateAction } from 'react';
import type { Channel, ChannelBaselinePeriodKey, VideoType } from '../types';
import type { useCatalogueNavigation } from './useCatalogueNavigation';

export type LibraryNavigation = ReturnType<typeof useCatalogueNavigation>;
type ReloadToken = Dispatch<SetStateAction<number>>;

/**
 * What the channel shell (ExplorerWorkspace) owns and hands to every Library
 * part: the header, Overview and Videos. Each part keeps its own state and
 * requests in its own file; the shell owns the address, profile loading
 * (hydration), the reload tokens and the live announcement.
 */
export type LibraryShellContext = {
  channelId: string;
  channel?: Channel;
  /** The media tab, read from the address (`type=`; absent = long). */
  videoType: VideoType;
  /** False while the HOT API is closed. Library reads do not wait for it: a closed API answers with its error. */
  apiAvailable: boolean;
  navigation: LibraryNavigation;
  activeSection: 'overview' | 'videos';
  overviewPeriod: ChannelBaselinePeriodKey;
  /** Publication hours, days and years: the viewer's zone or UTC. */
  timeZone: string;
  overviewVisible: boolean;
  videosVisible: boolean;
  profileReloadToken: number;
  setProfileReloadToken: ReloadToken;
  formatTotalsReloadToken: number;
  setFormatTotalsReloadToken: ReloadToken;
  /** Reload's own read of the header's list proof (list-inventory): not tied to the format totals. */
  listInventoryReloadToken: number;
  counterDynamicsReloadToken: number;
  setCounterDynamicsReloadToken: ReloadToken;
  catalogueReloadToken: number;
  setCatalogueReloadToken: ReloadToken;
  baselineReloadToken: number;
  setBaselineReloadToken: ReloadToken;
  setAnnouncement: (message: string) => void;
};
