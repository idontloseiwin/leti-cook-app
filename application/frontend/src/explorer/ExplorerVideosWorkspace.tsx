import { useEffect, useState } from 'react';
import { readBrowserPreference, writeBrowserPreference } from '../state/browserPreferences';
import { catalogueDensityPreference, type CatalogueDensity } from './cataloguePreferences';
import { ExplorerVideosPanel } from './ExplorerVideos';
import { FilterToolbar, type FilterToolbarProps } from './FilterToolbar';
import type { VideoCatalogueState } from './model';
import { resourcePayload } from './resourceState';
import type { useCatalogueBrowse } from './useCatalogueBrowse';
import { CollectionTransferDialog } from './CollectionTransferDialog';
import { videoNoun } from './format';

export type ExplorerVideosWorkspaceProps = {
  channelId: string;
  selectionScopeKey: string;
  rawState: VideoCatalogueState;
  toolbar: FilterToolbarProps;
  growthSortAvailable: boolean;
  staticDetailsAvailable?: boolean;
  historyAvailable?: boolean;
  launchAvailable?: boolean;
  blockedMessage?: string;
  onResetQuery: () => void;
  browse: ReturnType<typeof useCatalogueBrowse>;
  onRetryCatalogue: () => void;
};

export function ExplorerVideosWorkspace({
  channelId, selectionScopeKey, rawState, toolbar, growthSortAvailable, staticDetailsAvailable = false, historyAvailable = false,
  blockedMessage, onResetQuery, browse, onRetryCatalogue, launchAvailable = false,
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
  const presentedState: VideoCatalogueState = blockedMessage ? {
    kind: 'error', scopeKey: selectionScopeKey, previous,
    message: `${blockedMessage}${previous ? ' Previous results are shown; the requested query has not been applied.' : ''}`,
  } : rawState;
  return <section className="explorer-v2-video-workspace-shell" aria-label="Video catalogue workspace"
    data-channel-id={channelId} data-selection-scope={selectionScopeKey}>
    <div className="explorer-v2-video-workspace-filter-region"><FilterToolbar {...toolbar}
      density={density} onDensityChange={changeDensity} /></div>
    {blockedMessage && <div><button type="button" className="explorer-secondary-button" onClick={onResetQuery}>Reset catalogue query</button></div>}
    {collectionMessage && <p className="saved-view-confirmation" role="status">{collectionMessage}</p>}
    <ExplorerVideosPanel state={presentedState} channelId={channelId}
      growthAvailable={previous ? previous.source?.kind === 'derived_video_view_growth_from_stored_snapshots' : growthSortAvailable}
      density={density} selectionScopeKey={selectionScopeKey} onRetry={onRetryCatalogue}
      hasMore={browse.hasMore && !blockedMessage} hasPrevious={browse.hasPrevious && !blockedMessage}
      isLoadingMore={browse.isLoadingMore} loadMoreError={browse.loadMoreError}
      loadMoreErrorDirection={browse.loadMoreErrorDirection} expired={browse.expired}
      onLoadMore={browse.onLoadMore} onLoadPrevious={browse.onLoadPrevious}
      onReload={onRetryCatalogue} onAnchorChange={browse.onAnchorChange} restoreAnchor={browse.restoreAnchor}
      onCollectMatching={blockedMessage ? undefined : browse.collectMatching}
      browseSessionId={browse.sessionId} staticDetailsAvailable={staticDetailsAvailable}
      historyAvailable={historyAvailable}
      launchAvailable={launchAvailable}
      onAddToCollection={(ids) => { setCollectionMessage(''); setCollectionIds([...ids]); }} />
    {collectionIds && <CollectionTransferDialog ids={collectionIds} onClose={(result) => {
      setCollectionIds(undefined);
      if (result) setCollectionMessage(result.complete ? `${result.total.toLocaleString('en')} ${videoNoun(result.total)} saved to “${result.collection?.name || 'collection'}”.` : 'Selected videos are kept. The collection may be partial; check Saved before continuing.');
    }} />}
  </section>;
}
