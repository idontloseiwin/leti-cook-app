import type { Channel } from '../types';
import type { ExplorerDestination, SavedView } from './savedViews';
import { SAVED_VIEWS_PATH } from './savedViews';
import { COLLECTIONS_INDEX } from './savedCollections';
import { SavedViewsList } from './SavedViewsList';
import { SavedCollectionsList } from './SavedCollectionsList';
import { SavedCollectionDetail } from './SavedCollectionDetail';

export function SavedPage({ destination, channels, onOpenView, onNavigate }: {
  destination: Extract<ExplorerDestination, { kind: 'saved' }>; channels: Channel[]; onOpenView: (view: SavedView) => void; onNavigate: (path: string) => void;
}) {
  const collections = Boolean(destination.collectionId || destination.tab === 'collections');
  const navigation = <nav className="saved-kind-navigation" aria-label="Saved object types">
    {([[SAVED_VIEWS_PATH, 'Views', !collections], [COLLECTIONS_INDEX, 'Collections', collections]] as const).map(([path, label, selected]) =>
      <a key={path} href={path} aria-current={selected ? 'page' : undefined} onClick={(event) => {
        if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault(); onNavigate(path);
      }}>{label}</a>)}
  </nav>;
  if (destination.error) return <main className="saved-views-v2">{navigation}<p role="alert" className="saved-work-error">{destination.error}</p></main>;
  if (destination.collectionId) return <SavedCollectionDetail key={destination.collectionId} id={destination.collectionId} navigation={navigation} onNavigate={onNavigate} />;
  return collections ? <SavedCollectionsList navigation={navigation} onNavigate={onNavigate} />
    : <SavedViewsList channels={channels} onOpen={onOpenView} navigation={navigation} />;
}
