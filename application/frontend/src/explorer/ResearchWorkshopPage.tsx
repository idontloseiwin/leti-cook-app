import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { fromStudy, newCanvas, type CanvasGraph } from './canvas/canvasModel';
import { localZone } from './WorkshopBlocks';
import { ResearchFieldSettings } from './ResearchFieldSettings';
import { newStudy, type ResearchDocument } from './researchGraph';
import { parsePresentation } from './researchQuery';
import type { CollectionsState } from './WorkshopBlocks';
import { loadSavedView, readWorkshopAddress, RESEARCH_WORKSHOP_PATH, type SavedView } from './savedViews';
import { loadSavedCollections } from './savedCollections';
import { newWorkshopEntry, REOPENED_SELECTION_NOTE, resolveWorkshopStudy, tabMirror, workshopMirror, writeWorkshopHistory, type WorkshopHandoff, type WorkshopStudy } from './workshopHandoff';
import { CanvasCopies, markResume, readDraft, tabStore, takeResume } from './canvasDrafts';
import { SESSION_ENDED_EVENT } from '../ownerSession';
// Shared .research-* classes (tables, galleries, conditions, Collection fields) and the canvas.
import './researchCatalogue.css';
import './researchWorkshop.css';

export type SourceChoice = { id: string; name: string };
// The canvas (React Flow) loads as its own file, like Overview: Library's main file does not grow.
const CanvasWorkshop = lazy(() => import('./canvas/CanvasWorkshop').then(m => ({ default: m.CanvasWorkshop })));
// The catalogue page's remembered columns and view (read only): the first view of a new video study.
const CATALOGUE_PRESENTATION = 'leti-cook.catalogue.presentation.v1';
function cataloguePreference() {
  try {
    const raw = localStorage.getItem(CATALOGUE_PRESENTATION);
    return raw && JSON.parse(raw)?.version === 1 ? parsePresentation(raw) : undefined;
  } catch { return undefined; }
}
const currentEntry = (): string | undefined => {
  const state = window.history.state as { workshop?: { entry?: unknown } } | null;
  return typeof state?.workshop?.entry === 'string' ? state.workshop.entry : undefined;
};

/** The research workshop page: /research, ?saved=<id>, ?settings=fields and Library hand-offs. */
export function ResearchWorkshopPage({ channels, folders, labels, apiAvailable, handoffFor, onLeave }: {
  channels: SourceChoice[]; folders: SourceChoice[]; labels: SourceChoice[]; apiAvailable: boolean;
  // The hand-off of this history entry still in this tab's memory (with its fixed-list reference).
  handoffFor: (entry: string) => WorkshopHandoff | undefined;
  // A Saved item that opens in Library: replace this address with Library's.
  onLeave: (url: URL) => void;
}) {
  const [study, setStudy] = useState<WorkshopStudy | null>(null), [refreshKey, setRefreshKey] = useState(0);
  // ?settings=fields (Library's advanced-filters link) opens Collection fields with the first study only.
  const [fieldSettings, setFieldSettings] = useState(() => readWorkshopAddress(window.location.search).fieldSettings);
  const [collections, setCollections] = useState<SourceChoice[]>([]), collectionsRequested = useRef(false);
  const [collectionPage, setCollectionPage] = useState<{ loading: boolean; error: string; cursor: string | null }>({ loading: false, error: '', cursor: null });
  const collectionRequest = useRef<AbortController | null>(null);
  const props = useRef({ handoffFor, onLeave }); props.current = { handoffFor, onLeave };
  const entry = useRef<string | undefined>(undefined);
  // This entry's canvas in its two copies: history.state and this tab's draft (canvasDrafts).
  const copies = useRef<CanvasCopies | null>(null);
  copies.current ??= new CanvasCopies({ history: window.history, href: () => window.location.href, store: tabStore, now: Date.now });

  // Reload, Back and Forward return to the study of this history entry; otherwise the address decides.
  async function open(signal: AbortSignal) {
    const resolved = await resolveWorkshopStudy({ state: window.history.state, search: window.location.search, origin: window.location.origin,
      signal, loadSavedView, handoffFor: id => props.current.handoffFor(id), readDraft: id => readDraft(id),
      resume: takeResume(window.location.pathname + window.location.search, Date.now()) });
    if (!resolved || signal.aborted) return;
    if ('leave' in resolved) { props.current.onLeave(resolved.leave); return; }
    copies.current!.reset(resolved.study.seq ?? 0);
    window.history.replaceState(writeWorkshopHistory(window.history.state, tabMirror(resolved.study)), '', window.location.href);
    entry.current = resolved.study.entry; latest.current = {}; setStudy(resolved.study);
  }
  useEffect(() => {
    let controller = new AbortController();
    void open(controller.signal);
    const onPopState = () => {
      if (window.location.pathname.replace(/\/$/, '') !== RESEARCH_WORKSHOP_PATH) return; // the app leaves this page
      if (currentEntry() !== undefined && currentEntry() === entry.current) return;
      // The address already shows another entry: the edits waiting for the previous one go to its tab copy.
      copies.current!.flush(false);
      controller.abort(); controller = new AbortController();
      entry.current = undefined; setStudy(null); void open(controller.signal);
    };
    // Reload, closing the tab, leaving the site, or the end of the session (before sign-in): this entry is still the
    // current one, so both copies are written; with unsaved work the tab marks it, so a fresh entry at this address
    // (sign-in comes back here) takes it back.
    const onLeave = () => {
      copies.current!.flush(true);
      const id = entry.current;
      if (id && readDraft(id)) markResume(id, window.location.pathname + window.location.search, Date.now());
    };
    const onHidden = () => { if (document.visibilityState === 'hidden') copies.current!.flush(true); };
    window.addEventListener('popstate', onPopState); window.addEventListener('pagehide', onLeave); window.addEventListener(SESSION_ENDED_EVENT, onLeave);
    document.addEventListener('visibilitychange', onHidden);
    return () => {
      // Another page of the app: its address is already current, so the edits waiting here go to this entry's tab copy.
      controller.abort(); copies.current!.flush(false);
      window.removeEventListener('popstate', onPopState); window.removeEventListener('pagehide', onLeave); window.removeEventListener(SESSION_ENDED_EVENT, onLeave);
      document.removeEventListener('visibilitychange', onHidden);
    };
  }, []);

  // Canvas edits and the typed name reach this entry's history state and this tab's draft 500 ms after the last change,
  // and at once when the page is left, so Reload, Back and Forward return to them (unsaved edits included). Results
  // and Library's list reference are never written there.
  const latest = useRef<{ canvas?: CanvasGraph; name?: string; saved?: WorkshopStudy['saved']; baseRevision?: number }>({});
  function mirrorCanvas(current: WorkshopStudy) {
    copies.current!.edited(() => tabMirror({ ...current, ...latest.current }));
  }
  // After a conflict: the saved version opens in a new history entry; Back returns to this tab's unsaved changes.
  function openSavedVersion(view: SavedView) {
    if (view.conditions.version !== 4) return;
    const next: WorkshopStudy = { entry: newWorkshopEntry(), document: newStudy(), canvas: structuredClone(view.conditions.graph), name: view.name, saved: view,
      baseRevision: view.revision, fromQuery: false, autoRun: false, notice: 'The saved version is open. Back returns to your unsaved changes.',
      sourceMissing: !view.channel_available, run: { consistency: 'current' } };
    copies.current!.flush(true); copies.current!.reset(); latest.current = {};
    const url = new URL(RESEARCH_WORKSHOP_PATH, window.location.origin); url.searchParams.set('saved', view.id);
    window.history.pushState(writeWorkshopHistory(null, tabMirror(next)), '', url);
    entry.current = next.entry; setFieldSettings(false); setStudy(next);
  }
  // A template opens as a new canvas in a new history entry; Back returns to the previous canvas (its edits are kept first).
  function startCanvas(canvas: CanvasGraph, name: string, notice: string) {
    const next: WorkshopStudy = { entry: newWorkshopEntry(), document: newStudy(), canvas, name, fromQuery: false, autoRun: false, notice, sourceMissing: false, run: { consistency: 'current' } };
    copies.current!.flush(true); copies.current!.reset(); latest.current = {};
    window.history.pushState(writeWorkshopHistory(null, workshopMirror(next)), '', RESEARCH_WORKSHOP_PATH);
    entry.current = next.entry; setFieldSettings(false); setStudy(next);
  }
  // "New study", a template and an imported definition start a new history entry; Back returns to the previous study.
  function startStudy(document: ResearchDocument, name: string | undefined, notice: string) {
    // A template or import keeps the fixed-list mode only through its own Run: a new entry starts with current reads.
    const next: WorkshopStudy = { entry: newWorkshopEntry(), document, name, fromQuery: false, autoRun: false, notice, sourceMissing: false, run: { consistency: 'current' } };
    copies.current!.flush(true); copies.current!.reset(); latest.current = {};
    window.history.pushState(writeWorkshopHistory(null, workshopMirror(next)), '', RESEARCH_WORKSHOP_PATH);
    entry.current = next.entry; setFieldSettings(false); setStudy(next);
  }
  // Collections are paged (50 at a time): Refresh starts again, More continues. A missing collection on the loaded
  // pages is not proof that it was deleted.
  const loadCollections = (more = false) => {
    if (!more && collectionsRequested.current && collectionPage.loading) return;
    collectionsRequested.current = true;
    collectionRequest.current?.abort(); const controller = new AbortController(); collectionRequest.current = controller;
    setCollectionPage(page => ({ ...page, loading: true, error: '' }));
    loadSavedCollections(more ? collectionPage.cursor ?? undefined : undefined, controller.signal).then(page => {
      if (controller.signal.aborted) return;
      setCollections(previous => [...(more ? previous : []), ...page.collections.map(c => ({ id: c.id, name: c.name }))].filter((c, i, list) => list.findIndex(x => x.id === c.id) === i));
      setCollectionPage({ loading: false, error: '', cursor: page.next_cursor });
    }).catch(() => { if (!controller.signal.aborted) setCollectionPage(page => ({ ...page, loading: false, error: 'Collections could not be loaded. Try again.' })); });
  };
  useEffect(() => () => collectionRequest.current?.abort(), []);
  const collectionState: CollectionsState = { loading: collectionPage.loading, error: collectionPage.error, more: Boolean(collectionPage.cursor), load: loadCollections };
  useEffect(() => { if (study?.document.query.scope.kind === 'collection' && !collectionsRequested.current) loadCollections(); }, [study?.entry]);
  // Channels, folders and labels arrive together in one sidebar read: any of them proves the lists are loaded.
  const sourcesLoaded = channels.length + folders.length + labels.length > 0;

  if (!study) return <section className="research-workshop" aria-busy="true"><p role="status">Opening the study…</p></section>;
  // Every study opens on the canvas (graph version 2): a study's query (graph version 1) becomes its Source, a Filter and
  // a Sort; its result blocks keep their columns. A Library hand-off reuses Library's live fixed list on the
  // branch with Library's exact query; without it (reopened entry, expired list) the canvas says it reads current data.
  const run = study.run, fixedList = study.handoff?.frozen && run.consistency === 'frozen' && run.frozen_ref === study.handoff.frozen.ref ? study.handoff.frozen : undefined;
  const notice = study.notice === REOPENED_SELECTION_NOTE || (study.handoff?.kind === 'selection' && !fixedList)
    ? "Opened from Library. Library's fixed list is no longer available in this tab, so the canvas reads current data." : study.notice;
  return <Suspense fallback={<section className="research-workshop" aria-busy="true"><p role="status">Opening the canvas…</p></section>}>
    <CanvasWorkshop key={study.entry} initial={study.canvas ?? (study.fromQuery || study.saved || study.handoff ? fromStudy(study.document) : newCanvas(localZone()))}
      name={study.name} notice={notice} apiAvailable={apiAvailable} autoRun={study.autoRun} fixedList={fixedList}
      saved={study.saved} sourceMissing={study.sourceMissing} onSaved={view => {
        // This entry now names the Saved canvas: Reload, Back and Forward reopen it, and the address can be shared.
        latest.current = { ...latest.current, name: view.name, saved: view, baseRevision: view.revision };
        const url = new URL(window.location.href); url.searchParams.set('saved', view.id);
        window.history.replaceState(window.history.state, '', url);
        copies.current!.flush(false); copies.current!.write(tabMirror({ ...study, ...latest.current }), true);
      }}
      baseRevision={study.baseRevision} onOpenSaved={openSavedVersion}
      onBase={revision => { latest.current = { ...latest.current, baseRevision: revision }; mirrorCanvas(study); }}
      onChange={(canvas, name) => { latest.current = { ...latest.current, canvas, name }; mirrorCanvas(study); }}
      onTemplate={startCanvas}
      sources={{ folder: folders, label: labels, channel: channels, collection: collections }} sourcesLoaded={sourcesLoaded} collections={collectionState}
      settingsOpen={fieldSettings} refreshKey={refreshKey}
      pageActions={<button type="button" onClick={() => startStudy(newStudy(cataloguePreference()), undefined, '')}>New canvas</button>}
      settings={<ResearchFieldSettings initialOpen={fieldSettings} changed={() => setRefreshKey(value => value + 1)} />} />
  </Suspense>;
}
