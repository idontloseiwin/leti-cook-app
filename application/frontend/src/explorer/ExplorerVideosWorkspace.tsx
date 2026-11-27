import { useEffect, useState } from 'react';
import { readBrowserPreference, writeBrowserPreference } from '../state/browserPreferences';
import { catalogueDensityPreference, type CatalogueDensity } from './cataloguePreferences';
import { ExplorerVideosPanel, type LibraryInspectorSetup, type LibraryWorkshopActions } from './ExplorerVideos';
import { FilterToolbar, type FilterToolbarProps } from './FilterToolbar';
import { resourcePayload } from './resourceState';
import type { LibraryBrowse, LibraryVideosState } from './useCatalogueBrowse';
import { CollectionTransferDialog } from './CollectionTransferDialog';
import { videoNoun } from './format';
import type { VideoType } from '../types';

export type ExplorerVideosWorkspaceProps = {
  channelId: string;
  /** The tab's format: Long, Shorts and Streams mount the same workspace. */
  videoType?: VideoType;
  selectionScopeKey: string;
  rawState: LibraryVideosState;
  toolbar: FilterToolbarProps;
  growthSortAvailable: boolean;
  blockedMessage?: string;
  onResetQuery: () => void;
  browse: LibraryBrowse;
  /** Shared-backend Inspector panels: the server's gates and the viewer's zone. */
  inspector?: LibraryInspectorSetup;
  workshop?: LibraryWorkshopActions;
  onRetryCatalogue: () => void;
};

export function ExplorerVideosWorkspace({
  channelId, videoType = 'long', selectionScopeKey, rawState, toolbar, growthSortAvailable,
  blockedMessage, onResetQuery, browse, inspector, workshop, onRetryCatalogue,
}: ExplorerVideosWorkspaceProps) {
  const [density, setDensity] = useState(() => readBrowserPreference(catalogueDensityPreference));
  const [collectionIds, setCollectionIds] = useState<string[]>();
  const [collectionMessage, setCollectionMessage] = useState('');
  useEffect(() => setCollectionMessage(''), [channelId, selectionScopeKey]);
  const changeDensity = (next: CatalogueDensity) => {
    setDensity(next);
    writeBrowserPreference(catalogueDensityPreference, next);
  };
  const previous = resourcePayload(rawState);
  const presentedState: LibraryVideosState = blockedMessage ? {
    kind: 'error', scopeKey: selectionScopeKey, previous,
    message: `${blockedMessage}${previous ? ' Previous results are shown; the requested query has not been applied.' : ''}`,
  } : rawState;
  return <section className="explorer-v2-video-workspace-shell" aria-label="Video catalogue workspace"
    data-channel-id={channelId} data-selection-scope={selectionScopeKey}>
    <div className="explorer-v2-video-workspace-filter-region"><FilterToolbar {...toolbar}
      density={density} onDensityChange={changeDensity} /></div>
    {blockedMessage && <div><button type="button" className="explorer-secondary-button" onClick={onResetQuery}>Reset catalogue query</button></div>}
    {collectionMessage && <p className="saved-view-confirmation" role="status">{collectionMessage}</p>}
    <ExplorerVideosPanel state={presentedState} channelId={channelId} videoType={videoType}
      growthAvailable={previous ? previous.growth : growthSortAvailable}
      density={density} selectionScopeKey={selectionScopeKey} onRetry={onRetryCatalogue}
      hasMore={browse.hasMore && !blockedMessage} hasPrevious={browse.hasPrevious && !blockedMessage}
      isLoadingMore={browse.isLoadingMore} loadMoreError={browse.loadMoreError}
      loadMoreErrorDirection={browse.loadMoreErrorDirection} expired={browse.expired}
      onLoadMore={browse.onLoadMore} onLoadPrevious={browse.onLoadPrevious}
      anchorMissing={browse.anchorMissing} onStartFromBeginning={browse.startsBelowTop ? browse.onStartFromBeginning : undefined}
      onRefreshOrder={blockedMessage ? undefined : browse.onRefreshOrder}
      departedShown={browse.departedShown} refreshProblem={browse.refreshProblem} leftResults={browse.leftResults}
      onReload={onRetryCatalogue} onAnchorChange={browse.onAnchorChange} restoreAnchor={browse.restoreAnchor}
      onCollectMatching={blockedMessage ? undefined : browse.collectMatching}
      session={browse.sessionKey ? { frozenRef: browse.frozenRef, sessionKey: browse.sessionKey, pending: browse.pending } : undefined}
      inspector={inspector} preparing={browse.preparing}
      workshop={blockedMessage ? undefined : workshop}
      onAddToCollection={(ids) => { setCollectionMessage(''); setCollectionIds([...ids]); }} />
    {collectionIds && <CollectionTransferDialog ids={collectionIds} onClose={(result) => {
      setCollectionIds(undefined);
      if (result) setCollectionMessage(result.complete ? `${result.total.toLocaleString('en')} ${videoNoun(result.total)} saved to “${result.collection?.name || 'collection'}”.` : 'Selected videos are kept. The collection may be partial; check Saved before continuing.');
    }} />}
  </section>;
}
