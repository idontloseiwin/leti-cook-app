// The research canvas: nodes and typed wires on React Flow, a
// palette with search on the left, the selected node's properties on the right, results below. The graph
// (canvasModel.CanvasGraph, version 2) is the state; React Flow only draws it. Runs read stored data only.
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { Background, Controls, Handle, MiniMap, Panel, Position, ReactFlow, ReactFlowProvider, applyNodeChanges, useReactFlow,
  type Connection, type Edge, type FinalConnectionState, type Node, type NodeChange, type NodeProps } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import './canvas.css';
import { ResearchConditions, firstCondition } from '../ResearchConditions';
import { ObservationWindowPicker, ResearchColumnsPicker, ResearchSortEditor, changeFields, type SortKey } from '../ResearchControls';
import { ResearchBlockResult, localZone, type CollectionsState, type SourceChoices } from '../WorkshopBlocks';
import { channelSource, loadResearchCapabilities, researchError, researchLabels, researchTypeNames, videoSource,
  type Expression, type FieldDefinition, type ResearchCapabilities, type ResearchField, type ResearchSource, type ResearchType } from '../researchQuery';
import { canonicalSaved } from '../savedQueryDefinition';
import { ExplorerApiError } from '../api';
import { SESSION_ENDED_EVENT } from '../../ownerSession';
import { createSavedView, loadSavedView, updateSavedResearch, type SavedView, type SavedViewCreate } from '../savedViews';
import { newSavedWorkId, savedName } from '../savedWork';
import { recordEdit, redoEdit, shortcutAction, startHistory, undoEdit, type EditHistory } from '../workshopModel';
import { sameSavedContent, savedCanvasName, savedConflict, type FixedListReference } from '../workshopHandoff';
import { CANVAS_TEMPLATE_GROUPS, TEMPLATE_NAMES, openResearch, parseResearchDefinition, parseSavedCanvas, researchTemplate, templateOperations,
  type NodeResult, type SavedCanvas, type StudyTemplate } from '../researchGraph';
import { CanvasCards } from './CanvasCards';
import { CANVAS_BYTES, CANVAS_CATALOG, CANVAS_NOTES, CATEGORY_NAMES, addNode, branchQuery, budgetProblem, canvasBytes, connect, connectionProblem, connectionTargets, continueWithSelected, disconnect,
  canvasFields, duplicateNodes, editSizeProblem, entityOf, fromStudy, insertBetween, isDraft, isResult, menuMove, moveNode, nodeNames, nodeStudy, nodeSummary, pathTo, removeNodes, runCanvas,
  setParameters, sizeProblem, textBytes,
  type CanvasGraph, type CanvasKind, type CanvasNode, type CanvasResult, type Category, type Entity, type SourceParams } from './canvasModel';

type NodeData = { node: CanvasNode; summary: string; state: string; stateKey: string; rows: string };
type CanvasFlowNode = Node<NodeData, 'canvas'>;
const fieldName = (id: string) => (researchLabels as Record<string, string>)[id] ?? id;
const count = (n: number) => n.toLocaleString('en-US');
const VALUE_WORDS: Record<string, string> = { uint_decimal: 'Number', signed_decimal: 'Number', utc_instant: 'Date and time', local_date: 'Date',
  boolean: 'Yes or no', text_array: 'List of words', video_type: 'Format', video_id: 'ID', channel_id: 'ID' };

function CanvasNodeView({ data, selected }: NodeProps<CanvasFlowNode>) {
  const spec = CANVAS_CATALOG[data.node.type];
  return <div className={`canvas-node canvas-${spec.category}${selected ? ' selected' : ''}`}>
    {spec.input && <Handle type="target" position={Position.Left} id="in" title="Input: rows" aria-label="Input" />}
    <div className="canvas-node-kind">{CATEGORY_NAMES[spec.category]}</div>
    <div className="canvas-node-title">{spec.label}</div>
    <div className="canvas-node-summary">{data.summary}</div>
    <div className={`canvas-node-state state-${data.stateKey}`}>{data.state}</div>
    {spec.output && <Handle type="source" position={Position.Right} id="out" title={`Output: ${data.rows}`} aria-label={`Output: ${data.rows}`} />}
  </div>;
}
const NODE_TYPES = { canvas: CanvasNodeView };

/** A canvas menu of buttons: it takes the focus when it opens; the arrow keys, Home and End move through its
 *  buttons, and Escape closes it. When it closes with the focus inside (Escape, Cancel or a choice), the focus returns
 *  to `returnTo`; an edit elsewhere that closes it leaves the focus where it is. */
function CanvasMenu({ id, label, className, style, returnTo, onClose, children }: { id?: string; label: string; className?: string;
  style?: CSSProperties; returnTo: () => HTMLElement | null | undefined; onClose: () => void; children: ReactNode }) {
  const box = useRef<HTMLDivElement>(null), back = useRef(returnTo);
  back.current = returnTo;
  const buttons = () => [...(box.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [])];
  useLayoutEffect(() => {
    const menu = box.current;
    buttons()[0]?.focus();
    // Before the menu leaves the page, so the focus can still be seen inside it.
    return () => { if (menu?.contains(document.activeElement)) back.current()?.focus(); };
  }, []);
  return <div id={id} ref={box} className={'canvas-menu' + (className ? ' ' + className : '')} style={style} role="menu" aria-label={label}
    onKeyDown={event => {
      const items = buttons(), move = menuMove(event.key, items.indexOf(document.activeElement as HTMLButtonElement), items.length);
      if (move === null) return;
      event.preventDefault(); event.stopPropagation();
      if (move === 'close') onClose(); else items[move].focus();
    }}>{children}</div>;
}

type PaletteItem = { key: string; label: string; hint: string; category: Category; make: (graph: CanvasGraph, at: { x: number; y: number }, parent?: string) => { graph: CanvasGraph; id: string } | null };

export function CanvasWorkshop(props: CanvasProps) {
  return <ReactFlowProvider><CanvasEditor {...props} /></ReactFlowProvider>;
}
export type CanvasProps = {
  initial: CanvasGraph; name?: string; notice?: string; sources: SourceChoices; sourcesLoaded: boolean; collections: CollectionsState;
  // Every change of the graph or the typed name (the page keeps both in this tab's copies of the entry).
  apiAvailable: boolean; pageActions?: ReactNode; settings?: ReactNode; settingsOpen?: boolean; refreshKey?: number; onChange?: (graph: CanvasGraph, name: string) => void;
  // The Saved item this canvas came from: a canvas is updated in place, a study is kept and saved as a new canvas.
  saved?: SavedView; onSaved?: (view: SavedView) => void;
  // The revision this tab's edits started from (kept in the tab), reported again when it changes; the saved version,
  // opened in a new history entry after a conflict.
  baseRevision?: number; onBase?: (revision: number) => void; onOpenSaved?: (view: SavedView) => void;
  // A saved item whose source (or one of its sources) was deleted: said once, when the canvas opens.
  sourceMissing?: boolean;
  // Opened from Library: run once on arrival; Library's live fixed list, kept in this tab's memory only.
  autoRun?: boolean; fixedList?: FixedListReference;
  // A template starts a new canvas (a new history entry) on the same source.
  onTemplate?: (graph: CanvasGraph, name: string, notice: string) => void;
};

function CanvasEditor({ initial, name: initialName, notice, sources, collections, apiAvailable, pageActions, settings, settingsOpen, refreshKey, onChange,
  saved: initialSaved, onSaved, sourceMissing, autoRun, fixedList, onTemplate, baseRevision, onBase, onOpenSaved }: CanvasProps) {
  // Used while it lives and the server accepts it; then every branch reads current data, and the canvas says so.
  const [fixed, setFixed] = useState(() => fixedList && Date.parse(fixedList.valid_before) > Date.now() ? fixedList : undefined);
  const [saved, setSaved] = useState(initialSaved), [saving, setSaving] = useState(false), createCommand = useRef<SavedViewCreate | undefined>(undefined);
  // Save sends the revision this tab's edits started from, so a newer save elsewhere is refused (409), never
  // overwritten. Saved elsewhere after the edits began: the canvas says so and offers the choice.
  const [opened] = useState(() => savedConflict(initialSaved, baseRevision, initial, initialName));
  const [base, setBase] = useState(opened.base), [conflict, setConflict] = useState(opened.conflict), [replacing, setReplacing] = useState(false);
  useEffect(() => { if (base !== undefined) onBase?.(base); }, [base]);
  const zone = useMemo(localZone, []);
  const [history, setHistory] = useState<EditHistory<CanvasGraph>>(() => startHistory(initial));
  const graph = history.present;
  const [name, setName] = useState(initialName ?? '');
  const [selected, setSelected] = useState<string | null>(initial.nodes.find(n => isResult(n.type))?.id ?? initial.nodes[0]?.id ?? null);
  const [caps, setCaps] = useState<Partial<Record<ResearchSource, ResearchCapabilities>>>({});
  const [result, setResult] = useState<{ value: CanvasResult; graph: CanvasGraph; token: number } | null>(null);
  const [busy, setBusy] = useState(false), [message, setMessage] = useState(() => [notice, sourceMissing ? 'A source of this canvas is no longer available. Choose another one in its Source node.' : ''].filter(Boolean).join(' ')), [search, setSearch] = useState('');
  const [palette, setPalette] = useState(true), [menu, setMenu] = useState<{ x: number; y: number; flow: { x: number; y: number }; parent?: string } | null>(null);
  // The node whose "Connect to…" menu is open; another selection closes it.
  const [connecting, setConnecting] = useState<string | null>(null);
  useEffect(() => setConnecting(null), [selected]);
  const running = useRef<AbortController | null>(null), tokens = useRef(0);
  const flow = useReactFlow(), importInput = useRef<HTMLInputElement>(null), flowBox = useRef<HTMLDivElement>(null), connectButton = useRef<HTMLButtonElement>(null);
  useEffect(() => { onChange?.(graph, name); }, [graph, name]);
  useEffect(() => () => running.current?.abort(), []);
  // Fields and operators of both sources from the server capability index.
  useEffect(() => {
    const controller = new AbortController();
    for (const source of [videoSource, channelSource] as ResearchSource[])
      loadResearchCapabilities(controller.signal, source).then(value => setCaps(c => ({ ...c, [source]: value }))).catch(() => {});
    return () => controller.abort();
  }, [refreshKey]);

  // An edit retires the last message (a later message set in the same handler still shows). An edit that would make
  // the canvas too large to save is not made: the canvas says how large and what to shorten (returned to the caller too).
  function change(next: CanvasGraph, key?: string): string | null {
    const problem = editSizeProblem(graph, next);
    setMenu(null); setConnecting(null); setMessage(problem ?? '');
    if (!problem) setHistory(h => recordEdit(h, next, key));
    return problem;
  }
  const step = (direction: 'undo' | 'redo') => { setHistory(h => direction === 'undo' ? undoEdit(h) : redoEdit(h)); setMessage(''); };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const { key, ctrlKey, metaKey, shiftKey, altKey } = event; // getters of the event, which a spread would drop
      const action = shortcutAction({ key, ctrlKey, metaKey, shiftKey, altKey, target: event.target as HTMLElement });
      if (action) { event.preventDefault(); step(action); return; }
      const tag = (event.target as HTMLElement)?.tagName;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'd' && selected && !['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) {
        event.preventDefault(); duplicate([selected]);
      }
    };
    window.addEventListener('keydown', onKey); return () => window.removeEventListener('keydown', onKey);
  });

  const scopeName = (kind: string, id: string) => (sources as Record<string, { id: string; name: string }[]>)[kind]?.find(c => c.id === id)?.name ?? id;
  const runGraph = result?.graph;
  const stale = (id: string) => {
    if (!runGraph || !runGraph.nodes.some(n => n.id === id) || isDraft(graph, id) || isDraft(runGraph, id)) return true;
    try {
      const now = graph.nodes.find(n => n.id === id)!, then = runGraph.nodes.find(n => n.id === id)!;
      return canonicalSaved(branchQuery(graph, id)) !== canonicalSaved(branchQuery(runGraph, id)) || canonicalSaved(now.parameters) !== canonicalSaved(then.parameters);
    } catch { return true; }
  };
  function stateOf(node: CanvasNode): { text: string; key: string } {
    if (isDraft(graph, node.id)) return { text: 'Draft: connect to a source', key: 'draft' };
    const root = pathTo(graph, node.id)[0];
    if (root && !(root.parameters as SourceParams).scope.id) return { text: 'Choose what to read in the Source', key: 'draft' };
    const r = result?.value.nodes[node.id];
    if (busy) return { text: 'Running…', key: 'running' };
    if (!r) return { text: isResult(node.type) ? 'Ready to run' : 'Ready', key: 'ready' };
    if (stale(node.id)) return { text: 'Changed: run again', key: 'stale' };
    if (r.state === 'error') return { text: 'Error', key: 'error' };
    if (r.state === 'draft') return { text: 'Draft', key: 'draft' };
    if (isResult(node.type) && r.value && 'rows' in (r.value as object)) {
      const page = r.value as { rows: unknown[]; page: { has_more: boolean } };
      return { text: page.rows.length ? `${page.rows.length}${page.page.has_more ? '+' : ''} shown` : '0 matching', key: 'done' };
    }
    return { text: isResult(node.type) ? 'Result ready' : 'Ready', key: 'done' };
  }
  const entityLabel = (id: string) => { const e = entityOf(graph, id); if (!e) return 'rows'; if (e === 'channel') return 'channels';
    const p = graph.nodes.find(n => n.id === (pathTo0(graph, id)))?.parameters as SourceParams | undefined; return researchTypeNames[p?.video_types?.[0] ?? 'long'] + ' videos'; };

  // Every node's name in words: on the node for screen readers, and in "Connect to…".
  const names = useMemo(() => nodeNames(graph, { scope: scopeName }), [graph, sources]);
  // React Flow draws the graph; positions move locally while dragging and commit once on release (one Undo step).
  const derived = useMemo<CanvasFlowNode[]>(() => graph.nodes.map(node => {
    const state = stateOf(node);
    return { id: node.id, type: 'canvas', position: graph.layout[node.id] ?? { x: 0, y: 0 }, selected: node.id === selected, ariaLabel: `${names[node.id]}. ${state.text}`,
      data: { node, summary: nodeSummary(graph, node, { scope: scopeName }), state: state.text, stateKey: state.key, rows: entityLabel(node.id) } };
  }), [graph, selected, result, busy, sources]);
  const [nodes, setNodes] = useState<CanvasFlowNode[]>(derived);
  useEffect(() => setNodes(derived), [derived]);
  const edges = useMemo<Edge[]>(() => graph.edges.map(e => ({ id: `${e.from}->${e.to}`, source: e.from, target: e.to, sourceHandle: 'out', targetHandle: 'in',
    animated: busy, className: stale(e.to) ? 'canvas-edge-stale' : undefined })), [graph, busy, result]);

  function onNodesChange(changes: NodeChange<CanvasFlowNode>[]) {
    setNodes(current => applyNodeChanges(changes, current));
    for (const c of changes) {
      // A move the size rule refuses (a full canvas, a longer coordinate) puts the node back where the canvas has it.
      if (c.type === 'position' && c.dragging === false && c.position && change(moveNode(graph, c.id, c.position.x, c.position.y), 'move:' + c.id)) setNodes(derived);
      if (c.type === 'select' && c.selected) setSelected(c.id);
    }
  }
  const isValidConnection = (c: Connection | Edge) => Boolean(c.source && c.target && connectionProblem(graph, c.source, c.target) === null);
  // One wire, dragged between handles or chosen in "Connect to…": the same rules (connectionProblem) and one
  // edit, so Undo takes it back and the top bar shows Unsaved changes. `done` is said after a menu choice.
  function connectNodes(from: string, to: string, done = '') {
    const problem = connectionProblem(graph, from, to);
    if (problem) { setMessage(problem); return; }
    if (!change(connect(graph, from, to))) setMessage(done);
  }
  function onConnect(c: Connection) { connectNodes(c.source, c.target); }
  function onConnectEnd(event: MouseEvent | TouchEvent, state: FinalConnectionState) {
    if (state.isValid || !state.fromNode || state.fromHandle?.type !== 'source') return;
    const point = 'changedTouches' in event ? event.changedTouches[0] : event;
    setMenu({ x: point.clientX, y: point.clientY, flow: flow.screenToFlowPosition({ x: point.clientX, y: point.clientY }), parent: state.fromNode.id });
  }
  function duplicate(ids: string[]) {
    const kinds = ids.map(id => graph.nodes.find(n => n.id === id)?.type).filter(Boolean) as CanvasKind[];
    const problem = budgetProblem(graph, kinds); if (problem) { setMessage(problem); return; }
    const next = duplicateNodes(graph, ids); if (!change(next.graph)) setSelected(next.ids[0] ?? null);
  }
  function remove(ids: string[]) {
    const next = removeNodes(graph, ids);
    if (next === graph) { setMessage('A canvas keeps at least one node.'); return; }
    const orphans = graph.edges.filter(e => ids.includes(e.from) && !ids.includes(e.to)).length;
    change(next); setSelected(null);
    setMessage(orphans ? `Removed. ${orphans} connected node${orphans > 1 ? 's stay' : ' stays'} on the canvas as draft${orphans > 1 ? 's' : ''}; Undo restores everything.` : 'Removed. Undo restores it.');
  }

  // ---- Palette: categories of the catalogue, plus field actions found by search ----
  const videoFields = caps[videoSource]?.fields ?? [], channelFields = caps[channelSource]?.fields ?? [];
  const operations = new Set([...(caps[videoSource]?.operations ?? []), ...(caps[channelSource]?.operations ?? [])]);
  const plain = (type: CanvasKind, entity?: Entity): PaletteItem['make'] => (g, at, parent) => {
    const problem = budgetProblem(g, [type]); if (problem) { setMessage(problem); return null; }
    if (type === 'source') { const made = addNode(g, 'source', at, zone); return entity === 'channel' ? { graph: setParameters(made.graph, made.id, { ...made.graph.nodes.find(n => n.id === made.id)!.parameters, source: { id: channelSource }, video_types: null }), id: made.id } : made; }
    return addNode(g, type, at, zone, parent && !connectionProblem({ ...g, nodes: [...g.nodes, { id: '__probe', type, parameters: {} }] }, parent, '__probe') ? parent : undefined);
  };
  const items: PaletteItem[] = [
    { key: 'videos', label: 'Videos', hint: 'Stored videos of one format from a channel, folder, label or collection', category: 'source', make: plain('source', 'video') },
    { key: 'channels', label: 'Channels', hint: 'Stored channels of a channel, folder or label', category: 'source', make: plain('source', 'channel') },
    { key: 'filter', label: 'Filter', hint: CANVAS_CATALOG.filter.hint, category: 'process', make: plain('filter') },
    { key: 'sort', label: 'Sort', hint: CANVAS_CATALOG.sort.hint, category: 'process', make: plain('sort') },
    { key: 'gallery', label: 'Cards', hint: CANVAS_CATALOG.gallery.hint, category: 'view', make: plain('gallery') },
    { key: 'table', label: 'Table', hint: CANVAS_CATALOG.table.hint, category: 'view', make: plain('table') },
    ...(['summary', 'calendar', 'history'] as CanvasKind[]).filter(t => operations.has(CANVAS_CATALOG[t].operation!))
      .map(t => ({ key: t, label: CANVAS_CATALOG[t].label, hint: CANVAS_CATALOG[t].hint, category: 'calculation' as Category, make: plain(t) })),
  ];
  const query = search.trim().toLowerCase();
  const fieldItems: PaletteItem[] = !query ? [] : [...videoFields.map(f => ({ f, entity: 'video' as Entity })), ...channelFields.filter(f => f.id.startsWith('channel.')).map(f => ({ f, entity: 'channel' as Entity }))]
    .filter(({ f }) => fieldName(f.id).toLowerCase().includes(query) || f.id.includes(query))
    .flatMap(({ f, entity }) => {
      const who = entity === 'channel' ? 'channels' : 'videos', out: PaletteItem[] = [];
      out.push({ key: `filter:${entity}:${f.id}`, label: `Filter ${who} by ${fieldName(f.id)}`, hint: `${VALUE_WORDS[f.value_type] ?? 'Text'}${f.operators.length ? ' · ' + f.operators.length + ' comparisons' : ''}`, category: 'process',
        make: (g, at, parent) => { const made = plain('filter')(g, at, parent); return made && { graph: setParameters(made.graph, made.id, { where: firstConditionFor(f) }), id: made.id }; } });
      if (f.sortable) out.push({ key: `sort:${entity}:${f.id}`, label: `Sort ${who} by ${fieldName(f.id)}`, hint: 'Highest first', category: 'process',
        make: (g, at, parent) => { const made = plain('sort')(g, at, parent); return made && { graph: setParameters(made.graph, made.id, { order_by: [{ field: f.id, direction: 'desc' }] }), id: made.id }; } });
      return out;
    }).slice(0, 12);
  const shown = [...items.filter(i => !query || i.label.toLowerCase().includes(query) || i.hint.toLowerCase().includes(query)), ...fieldItems];
  function place(item: PaletteItem, at?: { x: number; y: number }, parent?: string) {
    const target = parent ?? (selected && graph.nodes.find(n => n.id === selected && !isResult(n.type)) ? selected : undefined);
    const anchor = target ? graph.layout[target] : undefined;
    const point = at ?? (anchor ? { x: anchor.x + 360, y: anchor.y + 30 } : flow.screenToFlowPosition({ x: window.innerWidth / 2, y: window.innerHeight / 2 }));
    const made = item.make(graph, point, item.category === 'source' ? undefined : target);
    if (!made) return;
    if (target && item.category === 'process' && !at) made.graph = insertBetween(made.graph, target, made.id);
    if (change(made.graph)) return;
    setSelected(made.id);
    if (!at) window.setTimeout(() => void flow.fitView({ padding: 0.3, maxZoom: 1, duration: 200 }), 0); // keep the new node in view
    if (target && item.category !== 'source' && !made.graph.edges.some(e => e.to === made.id)) setMessage(`${item.label} was added as a draft: ${connectionProblem({ ...graph, nodes: [...graph.nodes, made.graph.nodes.find(n => n.id === made.id)!] }, target, made.id) ?? 'connect it to a source.'}`);
  }

  async function run() {
    const tooLarge = sizeProblem(graph); // the server refuses it too; this names what to shorten
    if (tooLarge) { setMessage(tooLarge); return; }
    running.current?.abort(); const controller = new AbortController(); running.current = controller;
    setBusy(true); setMessage('');
    const snapshot = graph;
    try {
      const value = await runCanvas(snapshot, controller.signal, fixed ? { consistency: 'frozen', frozen_ref: fixed.ref } : undefined);
      if (running.current !== controller) return; // a newer run or a stop owns the screen now
      setResult({ value, graph: snapshot, token: ++tokens.current });
      const shown = Object.values(value.nodes).filter(r => 'consistency' in r && r.consistency === 'frozen').length;
      setMessage((value.status === 'partial' ? 'Some branches could not be completed; the others are shown.' : 'Canvas complete.')
        + (!fixed ? '' : value.run?.fixed ? ` ${shown} ${shown === 1 ? 'result shows' : 'results show'} Library's fixed list (${value.run.fixed.total_count.toLocaleString('en-US')} rows, until ${value.run.fixed.valid_before.slice(11, 16)} UTC); other branches read current data.`
          : " No branch has Library's exact query any more (its conditions, sorting, columns or page size changed), so every branch read current data."));
      const firstResult = snapshot.nodes.find(n => isResult(n.type) && !isDraft(snapshot, n.id));
      if (firstResult && (!selected || !isResult(snapshot.nodes.find(n => n.id === selected)?.type ?? 'source'))) setSelected(firstResult.id);
    } catch (error) {
      if (controller.signal.aborted) return;
      if (fixed && error instanceof ExplorerApiError && (error.code === 'cursor_expired' || error.code === 'cursor_context_changed')) {
        setFixed(undefined); // Never replaced silently: the next run reads current data, and this says so.
        setMessage("Library's fixed list expired or its data changed. Run the canvas again to read current data.");
        return;
      }
      setMessage(researchError(error, 'Run the canvas again.'));
    } finally { if (running.current === controller) { running.current = null; setBusy(false); } }
  }
  function stop() { running.current?.abort(); running.current = null; setBusy(false); setMessage('Stopped. Your canvas is kept; run it again when ready.'); }
  // Import: a canvas file (Export) or a research definition file (a study), as one edit (Undo returns). Never data.
  async function importFile(file: File) {
    if (file.size > 40000) { setMessage('Canvas files are limited to 40 KB.'); return; }
    try {
      const raw = JSON.parse(await file.text()) as Record<string, unknown> | null, keys = raw ? Object.keys(raw).sort().join() : '';
      let next: CanvasGraph, title = '';
      if (raw?.format === 'leti-cook-canvas' && raw.version === 1 && keys === 'format,graph,name,version' && typeof raw.name === 'string') {
        next = parseSavedCanvas({ version: 4, graph: raw.graph }).graph; title = raw.name.slice(0, 120);
      } else if (raw?.format === 'leti-cook-research' && raw.version === 1 && keys === 'channel_id,definition,format,version') {
        next = fromStudy(openResearch(parseResearchDefinition(raw.definition), raw.channel_id as string | null));
      } else throw new Error();
      if (change(next)) return;
      setSelected(null); setResult(null); if (title) setName(title);
      setMessage('Definition imported. Undo returns to the previous canvas; Run checks current access and fields.');
    } catch { setMessage('The file is not a supported canvas or research definition.'); }
  }
  // Templates: the study templates' starting views on the selected (or first) source, as a new canvas.
  const offered = (t: StudyTemplate) => templateOperations(t).length === 0 || templateOperations(t).some(o => operations.has(o));
  function startTemplate(template: StudyTemplate) {
    if (!template || !onTemplate) return;
    const source = graph.nodes.find(n => n.id === selected && n.type === 'source') ?? graph.nodes.find(n => n.type === 'source');
    if (!source) { setMessage('Add a Source first: a template starts from its channel, folder, label or collection.'); return; }
    try {
      const name = TEMPLATE_NAMES[template];
      onTemplate(fromStudy(researchTemplate(branchQuery(graph, source.id), template, [...operations])), name, `Template: ${name}, on the same source.`);
    } catch { setMessage('This template is not available for this source.'); }
  }
  function exportFile() {
    const blob = new Blob([canonicalSaved({ format: 'leti-cook-canvas', version: 1, name, graph })], { type: 'application/json' }), url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = (name || 'canvas') + '.json'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // ---- Save: the graph only (Saved conditions version 4); results are never stored. An opened study is saved
  // as a new canvas and the study is kept. A retried create sends the same ID, so it never makes two items. ----
  function saveProblem(): string | null {
    if (graph.nodes.some(n => n.type === 'source' && !(n.parameters as SourceParams).scope.id)) return 'Choose what every Source reads before saving.';
    return sizeProblem(graph);
  }
  const saveError = (error: unknown) => error instanceof Error && error.message ? error.message : 'The canvas could not be saved.';
  async function createOnce(title: string): Promise<SavedView> {
    const next = { name: savedName(title), channel_id: null, conditions: { version: 4, graph } as SavedCanvas };
    if (!createCommand.current || canonicalSaved({ ...createCommand.current, id: undefined }) !== canonicalSaved(next)) createCommand.current = { id: newSavedWorkId(), ...next };
    const view = await createSavedView(createCommand.current);
    createCommand.current = undefined;
    return view;
  }
  function savedAs(view: SavedView, text: string) {
    setSaved(view); setName(view.name); setBase(view.revision); setConflict(false); setReplacing(false); onSaved?.(view); setMessage(text);
  }
  async function save() {
    const problem = saveProblem(); if (problem) { setMessage(problem); return; }
    const title = savedCanvasName(name), conditions: SavedCanvas = { version: 4, graph }, study = saved && saved.conditions.version !== 4;
    setSaving(true); setMessage('');
    try {
      const view = saved && !study ? await updateSavedResearch(base !== undefined ? { ...saved, revision: base } : saved, title, conditions) : await createOnce(title);
      savedAs(view, study ? 'Saved as a new canvas. The original study is kept. Results were not stored.' : 'Canvas saved. Results were not stored.');
    } catch (error) {
      if (error instanceof ExplorerApiError && error.status === 409) { setConflict(true); setReplacing(false); }
      else setMessage(saveError(error));
    } finally { setSaving(false); }
  }
  // The three ways out of a conflict; none of them loses either version without the person choosing it.
  async function saveMineAsNew() {
    const problem = saveProblem(); if (problem) { setMessage(problem); return; }
    const suffix = ' (my version)', title = [...savedCanvasName(name)].slice(0, 120 - suffix.length).join('').trim() + suffix;
    setSaving(true); setMessage('');
    try { savedAs(await createOnce(title), 'Saved as a new canvas. The version saved elsewhere is kept as it was.'); }
    catch (error) { setMessage(saveError(error)); } finally { setSaving(false); }
  }
  async function openSavedVersion() {
    if (!saved || !onOpenSaved) return;
    setSaving(true); setMessage('');
    try { onOpenSaved(await loadSavedView(saved.id)); }
    catch { setMessage('The saved version could not be opened: it may have been deleted. Save yours as a new canvas.'); }
    finally { setSaving(false); }
  }
  async function replaceSavedVersion() {
    const problem = saveProblem(); if (problem || !saved) { setMessage(problem ?? ''); return; }
    setSaving(true); setMessage('');
    try {
      const newest = await loadSavedView(saved.id);
      savedAs(await updateSavedResearch(newest, savedCanvasName(name), { version: 4, graph }), 'Canvas saved. Your version replaced the one saved elsewhere.');
    } catch (error) {
      setReplacing(false);
      setMessage(error instanceof ExplorerApiError && error.status === 409 ? 'It was saved elsewhere again just now. Choose again.' : saveError(error));
    } finally { setSaving(false); }
  }
  // The same notion of saved content (graph and name) as the tab's copies and the conflict check.
  const unsaved = !sameSavedContent(saved, graph, name);

  // Closing the tab discards unsaved changes, so the browser asks first (also on Reload, which the tab's copies survive).
  // A new empty canvas — nothing chosen, no notes, never saved — does not ask.
  const keepWorth = unsaved && (saved !== undefined || graph.notes.trim() !== '' || name.trim() !== ''
    || graph.nodes.some(n => n.type === 'source' && Boolean((n.parameters as SourceParams).scope.id)));
  // At the end of a session the page goes to sign-in by itself, with the work kept in the tab: no question then.
  const sessionEnded = useRef(false);
  useEffect(() => {
    const ended = () => { sessionEnded.current = true; };
    window.addEventListener(SESSION_ENDED_EVENT, ended);
    return () => window.removeEventListener(SESSION_ENDED_EVENT, ended);
  }, []);
  useEffect(() => {
    if (!keepWorth) return;
    const ask = (event: BeforeUnloadEvent) => { if (sessionEnded.current) return; event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', ask);
    return () => window.removeEventListener('beforeunload', ask);
  }, [keepWorth]);
  const node = graph.nodes.find(n => n.id === selected) ?? null;
  // Notes count in UTF-8 bytes, like on the server; the whole canvas is shown once it passes three quarters of its limit.
  const notesUsed = textBytes(graph.notes), canvasUsed = useMemo(() => canvasBytes(graph), [graph]);
  const [notesRefusal, setNotesRefusal] = useState(''); // why the last notes edit was not made, shown under the notes
  const runnable = graph.nodes.some(n => isResult(n.type) && !isDraft(graph, n.id)) && graph.nodes.every(n => n.type !== 'source' || Boolean((n.parameters as SourceParams).scope.id));
  // A Library hand-off runs once on arrival, like Library's own first page.
  const arrived = useRef(false);
  useEffect(() => { if (autoRun && !arrived.current && runnable && apiAvailable) { arrived.current = true; void run(); } }, [autoRun, runnable, apiAvailable]);
  return <section className="research-workshop canvas-workshop">
    <header className="canvas-topbar">
      <span className="explorer-eyebrow">Research workshop</span>
      <input className="canvas-name" aria-label="Study name" maxLength={120} placeholder="Untitled canvas" value={name} onChange={e => setName(e.target.value)} />
      <div className="research-actions">
        <span className="canvas-save-state" role="status">{saving ? 'Saving…' : unsaved ? 'Unsaved changes' : 'Saved'}</span>
        <button type="button" disabled={saving || !apiAvailable || !unsaved} onClick={() => void save()}>Save</button>
        <button type="button" className="canvas-run" disabled={busy || !runnable || !apiAvailable} onClick={() => void run()}>{busy ? 'Running…' : 'Run'}</button>
        {busy && <button type="button" onClick={stop}>Stop</button>}
        <button type="button" disabled={!history.past.length} title="Undo (Ctrl+Z)" onClick={() => step('undo')}>Undo</button>
        <button type="button" disabled={!history.future.length} title="Redo (Ctrl+Shift+Z)" onClick={() => step('redo')}>Redo</button>
        <button type="button" onClick={exportFile}>Export</button>
        <button type="button" onClick={() => importInput.current?.click()}>Import</button>
        <input ref={importInput} type="file" accept="application/json,.json" hidden aria-label="Import a canvas or research definition"
          onChange={e => { const file = e.target.files?.[0]; if (file) void importFile(file); e.target.value = ''; }} />
        <button type="button" aria-pressed={palette} onClick={() => setPalette(p => !p)}>{palette ? 'Hide palette' : 'Show palette'}</button>
        {pageActions}
      </div>
    </header>
    {conflict && saved && <div className="canvas-conflict" role="alert">
      <p>This canvas was saved elsewhere after you started editing here. Your changes are kept in this tab.</p>
      {replacing ? <div className="research-actions">
        <span>Replace the saved version with yours? The changes saved elsewhere will be lost.</span>
        <button type="button" disabled={saving || !apiAvailable} onClick={() => void replaceSavedVersion()}>Replace the saved version</button>
        <button type="button" disabled={saving} onClick={() => setReplacing(false)}>Cancel</button>
      </div> : <div className="research-actions">
        <button type="button" disabled={saving || !apiAvailable} onClick={() => void saveMineAsNew()}>Save mine as a new canvas</button>
        {onOpenSaved && <button type="button" disabled={saving || !apiAvailable} onClick={() => void openSavedVersion()}>Open the saved version</button>}
        <button type="button" disabled={saving || !apiAvailable} onClick={() => setReplacing(true)}>Replace the saved version with mine</button>
      </div>}
    </div>}
    <p className="canvas-status canvas-status-line" role="status">{!apiAvailable ? 'Stored data is temporarily unavailable.' : message || (!runnable ? 'Choose a channel, folder, label or collection in every Source and connect a result node to run.' : '')}</p>
    <div className={'canvas-body' + (palette ? '' : ' no-palette')}>
      {palette && <aside className="canvas-palette" aria-label="Add nodes">
        <input type="search" placeholder="Search nodes and fields (e.g. duration)" aria-label="Search nodes and fields" value={search} onChange={e => setSearch(e.target.value)} />
        {onTemplate && <select className="canvas-template" aria-label="Start a new canvas from a template" value="" onChange={e => startTemplate(e.target.value as StudyTemplate)}>
          <option value="">Start from a template…</option>
          {CANVAS_TEMPLATE_GROUPS.filter(group => group.templates.some(offered)).map(group => <optgroup key={group.label} label={group.label}>
            {group.templates.filter(offered).map(t => <option key={t} value={t}>{TEMPLATE_NAMES[t]}</option>)}</optgroup>)}
        </select>}
        {(['source', 'process', 'calculation', 'view'] as Category[]).map(category => {
          const group = shown.filter(i => i.category === category);
          return group.length > 0 && <section key={category}><h3>{CATEGORY_NAMES[category]}</h3>
            {group.map(item => <button type="button" key={item.key} className={`canvas-palette-item canvas-${category}`} onClick={() => place(item)}>
              <strong>{item.label}</strong><small>{item.hint}</small></button>)}</section>;
        })}
        {query && !shown.length && <p>Nothing matches “{search}”.</p>}
      </aside>}
      <div className="canvas-flow" ref={flowBox}>
        <ReactFlow<CanvasFlowNode, Edge> nodes={nodes} edges={edges} nodeTypes={NODE_TYPES} onNodesChange={onNodesChange}
          onConnect={onConnect} isValidConnection={isValidConnection} onConnectEnd={onConnectEnd}
          onDelete={({ nodes: gone, edges: cut }) => { // one edit, so one Undo brings the nodes back with their wires
            if (gone.length) remove(gone.map(n => n.id)); else if (cut.length) change(cut.reduce((g, e) => disconnect(g, e.target), graph)); }}
          onPaneClick={() => { setMenu(null); setSelected(null); }} deleteKeyCode={['Delete', 'Backspace']} fitView fitViewOptions={{ padding: 0.3, maxZoom: 1 }}
          minZoom={0.2} maxZoom={1.75} snapToGrid snapGrid={[20, 20]} colorMode="dark" proOptions={{ hideAttribution: false }}
          zoomOnScroll={false} preventScrolling={false}>{/* the wheel scrolls the page to the results; Ctrl + wheel or a pinch zooms */}
          <Background gap={20} size={1.2} />
          <Panel position="top-left" className="canvas-hint">Drag the background to move · Ctrl + wheel to zoom</Panel>
          <Controls showInteractive={false} />
          <MiniMap pannable zoomable nodeStrokeWidth={2} ariaLabel="Canvas overview" />
        </ReactFlow>
        {/* Closed with the focus inside, the menu gives it back to the node the wire was dragged from. */}
        {menu && <CanvasMenu style={{ left: menu.x, top: menu.y }} label="Add a connected node" onClose={() => setMenu(null)}
          returnTo={() => menu.parent ? flowBox.current?.querySelector<HTMLElement>(`.react-flow__node[data-id="${menu.parent}"]`) : null}>
          <strong>Add after {CANVAS_CATALOG[graph.nodes.find(n => n.id === menu.parent)?.type ?? 'source'].label}</strong>
          {items.filter(i => i.category !== 'source' && (!menu.parent || !connectionProblem({ ...graph, nodes: [...graph.nodes, { id: '__probe', type: i.key as CanvasKind, parameters: {} }] }, menu.parent, '__probe')))
            .map(i => <button type="button" role="menuitem" key={i.key} onClick={() => place(i, menu.flow, menu.parent)}>{i.label}</button>)}
          <button type="button" onClick={() => setMenu(null)}>Cancel</button>
        </CanvasMenu>}
      </div>
      {node && <aside className="canvas-properties" aria-label="Node properties">
        <h2>{CANVAS_CATALOG[node.type].label}</h2>
        <p className="research-note">{CANVAS_CATALOG[node.type].hint}</p>
        <NodeProperties node={node} graph={graph} caps={caps} sources={sources} collections={collections} zone={zone}
          onParameters={(parameters, key) => change(setParameters(graph, node.id, parameters), key)} />
        <div className="research-actions">
          {/* Wires without a mouse: the nodes this one can feed, by the rules of a dragged wire. */}
          {CANVAS_CATALOG[node.type].output && <button type="button" ref={connectButton} aria-haspopup="menu" aria-expanded={connecting === node.id}
            aria-controls={connecting === node.id ? 'canvas-connect-menu' : undefined} onClick={() => setConnecting(open => open === node.id ? null : node.id)}>Connect to…</button>}
          <button type="button" onClick={() => duplicate([node.id])}>Duplicate</button>
          <button type="button" onClick={() => remove([node.id])}>Remove</button>
          {graph.edges.some(e => e.to === node.id) && <button type="button" onClick={() => change(disconnect(graph, node.id))}>Disconnect input</button>}
        </div>
        {connecting === node.id && <ConnectMenu graph={graph} from={node.id} names={names} returnTo={() => connectButton.current}
          onClose={() => setConnecting(null)} onConnect={(to, said) => connectNodes(node.id, to, said)} />}
      </aside>}
    </div>
    <label className="workshop-notes canvas-notes">My research notes<textarea rows={3} value={graph.notes} aria-describedby="canvas-notes-size"
      onChange={e => setNotesRefusal(change({ ...graph, notes: e.target.value }, 'notes') ?? '')} placeholder="Your question and observations. Results are never stored with the canvas." /></label>
    <p id="canvas-notes-size" className={'canvas-notes-size' + (notesRefusal || notesUsed > CANVAS_NOTES || canvasUsed > CANVAS_BYTES ? ' over' : '')} aria-live="polite">
      {notesRefusal || `Notes: ${count(notesUsed)} of ${count(CANVAS_NOTES)} bytes (a Cyrillic letter takes 2)`
        + (canvasUsed > CANVAS_BYTES * 0.75 ? ` · Canvas: ${count(canvasUsed)} of ${count(CANVAS_BYTES)} bytes` : '')}</p>
    {settings && <details className="canvas-settings" open={settingsOpen}><summary>Collection settings</summary>{settings}</details>}
    {result && <CanvasResults graph={graph} result={result} selected={selected} stale={stale} onSelect={setSelected}
      onContinue={(id, ids) => { const next = continueWithSelected(graph, id, ids, zone); if ('problem' in next) { setMessage(next.problem); return next.problem; }
        const problem = change(next.graph); if (problem) return problem;
        setSelected(next.id); setMessage(`${ids.length} picked item${ids.length > 1 ? 's' : ''} continue in a Selected items node with Cards after it. Run to read them.`); return null; }} />}
  </section>;
}

/** "Connect to…": every node `from` can feed, named in words; a node that already has an input says which wire
 *  the new one replaces. A choice makes the same wire as dragging it. */
function ConnectMenu({ graph, from, names, returnTo, onClose, onConnect }: { graph: CanvasGraph; from: string; names: Record<string, string>;
  returnTo: () => HTMLElement | null; onClose: () => void; onConnect: (to: string, said: string) => void }) {
  const targets = connectionTargets(graph, from);
  return <CanvasMenu id="canvas-connect-menu" className="canvas-menu-inline" label={`Connect ${names[from]} to`} returnTo={returnTo} onClose={onClose}>
    <strong>{targets.length ? 'Connect to' : 'No node can take these rows. Add one from the palette, then connect it.'}</strong>
    {targets.map(t => <button type="button" role="menuitem" key={t.id} onClick={() => onConnect(t.id,
      `Connected: ${names[from]} → ${names[t.id]}${t.replaces ? ` (instead of ${names[t.replaces]})` : ''}. Undo removes the wire.`)}>
      {names[t.id]}{t.replaces ? ` (replaces the wire from ${names[t.replaces]})` : ''}</button>)}
    <button type="button" onClick={onClose}>Cancel</button>
  </CanvasMenu>;
}

function pathTo0(graph: CanvasGraph, id: string): string | undefined {
  let current: string | undefined = id; const seen = new Set<string>();
  while (current && !seen.has(current)) { seen.add(current); const parent = graph.edges.find(e => e.to === current)?.from; if (!parent) return current; current = parent; }
  return undefined;
}
function firstConditionFor(field: FieldDefinition): Expression {
  if (field.operators.includes('gte')) return { field: field.id, op: 'gte', value: field.id === 'video.duration_seconds' ? '600' : '0' };
  return firstCondition([field]);
}

function NodeProperties({ node, graph, caps, sources, collections, zone, onParameters }: {
  node: CanvasNode; graph: CanvasGraph; caps: Partial<Record<ResearchSource, ResearchCapabilities>>; sources: SourceChoices; collections: CollectionsState; zone: string;
  onParameters: (parameters: Record<string, unknown>, key?: string) => void;
}) {
  const p = node.parameters, entity = node.type === 'source' ? (savedSource(p) === channelSource ? 'channel' : 'video') : entityOf(graph, node.id);
  const root = graph.nodes.find(n => n.id === pathTo0(graph, node.id));
  const type = ((root?.parameters as SourceParams | undefined)?.video_types?.[0] ?? 'long') as ResearchType;
  const capability = entity ? caps[entity === 'channel' ? channelSource : videoSource] : undefined;
  const fields = (capability?.fields ?? []).filter(f => entity === 'channel' ? f.id.startsWith('channel.') : f.id !== 'channel.id' && (!f.enabled_video_types || f.enabled_video_types.includes(type)));
  if (!entity && node.type !== 'source') return <p role="status">Connect this node to a source to choose its fields.</p>;
  switch (node.type) {
    case 'source': {
      const s = p as SourceParams, kinds = (entity === 'channel' ? ['folder', 'label', 'channel'] : ['folder', 'label', 'channel', 'collection']) as SourceParams['scope']['kind'][];
      const choices = (sources as Record<string, { id: string; name: string }[]>)[s.scope.kind] ?? [];
      return <div className="canvas-form">
        <fieldset><legend>Rows</legend>
          {(['video', 'channel'] as Entity[]).map(e => <label key={e}><input type="radio" name={'entity-' + node.id} checked={entity === e}
            onChange={() => onParameters({ ...s, source: { id: e === 'channel' ? channelSource : videoSource }, video_types: e === 'channel' ? null : ['long'],
              scope: e === 'channel' && s.scope.kind === 'collection' ? { kind: 'folder', id: '', include_descendants: false } : s.scope })} />{e === 'channel' ? 'Channels' : 'Videos'}</label>)}
        </fieldset>
        <label>From <select value={s.scope.kind} onChange={e => { const kind = e.target.value as SourceParams['scope']['kind']; if (kind === 'collection') collections.load();
          onParameters({ ...s, scope: kind === 'folder' ? { kind, id: '', include_descendants: false } : { kind, id: '' } }); }}>
          {kinds.map(k => <option key={k} value={k}>{k[0].toUpperCase() + k.slice(1)}</option>)}</select></label>
        <label>Which <select value={s.scope.id} onChange={e => onParameters({ ...s, scope: { ...s.scope, id: e.target.value } })}>
          <option value="">Choose…</option>{choices.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select></label>
        {s.scope.kind === 'collection' && collections.loading && <small>Loading collections…</small>}
        {s.scope.kind === 'collection' && collections.more && <button type="button" onClick={() => collections.load(true)}>More collections</button>}
        {entity === 'video' && <label>Format <select value={s.video_types?.[0] ?? 'long'} onChange={e => onParameters({ ...s, video_types: [e.target.value as ResearchType] })}>
          {(['long', 'short', 'stream'] as ResearchType[]).map(t => <option key={t} value={t}>{researchTypeNames[t]}{t === 'stream' ? ' (completed)' : ''}</option>)}</select></label>}
        <label className="canvas-check"><input type="checkbox" checked={s.coverage?.mode === 'require_complete'}
          onChange={e => onParameters(withRule(s, 'coverage', e.target.checked ? { ...s.coverage, mode: 'require_complete' }
            : s.coverage?.required_fields?.length ? { ...s.coverage, mode: 'available' } : undefined))} />Require complete data</label>
        {entity === 'video' && canvasFields(graph).some(f => changeFields.includes(f)) && <ObservationWindowPicker window={s.observations?.window_seconds ?? 3600}
          limits={capability?.observation_age_limits[type]} onChange={w => onParameters(withRule(s, 'observations', w.window_seconds === 3600 ? undefined : w))} />}
        <small>Dates count in {s.time.timezone}{s.time.timezone !== zone ? ` (your zone is ${zone})` : ''}.</small>
      </div>;
    }
    case 'filter': return <div className="canvas-form">{p.where
      ? <><ResearchConditions value={p.where as Expression} fields={fields} onChange={where => onParameters({ where })} /><button type="button" onClick={() => onParameters({ where: null })}>Clear conditions</button></>
      : <><p>No condition yet.</p><button type="button" disabled={!fields.length} onClick={() => onParameters({ where: firstCondition(fields) })}>Add a condition</button></>}
      <small>A filter narrows only its own branch. Unknown values never count as zero.</small></div>;
    case 'sort': return <div className="canvas-form"><ResearchSortEditor value={p.order_by as SortKey[]} fields={fields} channels={entity === 'channel'}
      onChange={order => onParameters({ order_by: order.map(s => ({ field: s.field, direction: s.direction })) })} /><small>The last Sort on a branch sets its whole order.</small></div>;
    case 'selection': return <div className="canvas-form"><label>Keep <select value={String(p.mode)} onChange={e => onParameters({ ...p, mode: e.target.value })}>
      <option value="include">only these</option><option value="exclude">all but these</option></select></label><p>{(p.ids as string[]).length} picked item(s).</p></div>;
    case 'gallery': case 'table': return <div className="canvas-form">
      <label>Per page <select value={Number(p.page_size)} onChange={e => onParameters({ ...p, page_size: Number(e.target.value) })}>
        {[...new Set([...(node.type === 'gallery' ? [12, 24, 48, 96] : [25, 50, 100]), Number(p.page_size)])].sort((a, b) => a - b)
        .map(n => <option key={n} value={n}>{n}</option>)}</select></label>
      <details open><summary>Fields shown ({(p.select as string[]).length})</summary>
        <ResearchColumnsPicker columns={p.select as ResearchField[]} fields={fields} channels={entity === 'channel'} loaded={Boolean(capability)}
          onChange={select => onParameters({ ...p, select: select.length ? select : (p.select as ResearchField[]) })} /></details>
      <small>Fields shown belong to this result only; they never narrow the rows.</small></div>;
    case 'calendar': return <div className="canvas-form"><label>Year <input type="number" min={1} max={9999} value={Number(p.year)} onChange={e => onParameters({ ...p, year: Number(e.target.value) })} /></label></div>;
    case 'history': return <div className="canvas-form"><label>Metric <select value={String(p.metric)} onChange={e => onParameters({ ...p, metric: e.target.value })}>
      <option value="views">Views</option><option value="subscribers">Subscribers</option></select></label>
      {(root?.parameters as SourceParams | undefined)?.scope.kind !== 'channel' && <p role="status">Channel history needs a Source of one channel.</p>}</div>;
    default: return null;
  }
}
const savedSource = (p: Record<string, unknown>) => (p.source as { id?: string } | undefined)?.id;
/** A Source with one rule set, or removed (the query's default then applies). */
function withRule<K extends 'coverage' | 'observations'>(s: SourceParams, key: K, value: SourceParams[K] | undefined): Record<string, unknown> {
  const next: Record<string, unknown> = { ...s };
  if (value === undefined) delete next[key]; else next[key] = value;
  return next;
}

function CanvasResults({ graph, result, selected, stale, onSelect, onContinue }: {
  graph: CanvasGraph; result: { value: CanvasResult; graph: CanvasGraph; token: number }; selected: string | null; stale: (id: string) => boolean;
  // A refusal (the picked items do not fit) comes back to show next to the button.
  onSelect: (id: string) => void; onContinue: (id: string, ids: string[]) => string | null;
}) {
  const outputs = result.graph.nodes.filter(n => isResult(n.type) && result.value.nodes[n.id]?.state !== 'draft');
  const current = outputs.find(n => n.id === selected) ?? outputs[0];
  if (!current) return null;
  const r = result.value.nodes[current.id] as NodeResult, study = nodeStudy(result.graph, current.id);
  // A tab is named by its branch: "Cards after Filter → Sort"; equal names get a number.
  const names = outputs.map(n => { const steps = pathTo(result.graph, n.id).slice(1, -1).map(s => CANVAS_CATALOG[s.type].label);
    return CANVAS_CATALOG[n.type].label + (steps.length ? ' after ' + steps.join(' → ') : ''); });
  const tabName = (i: number) => names[i] + (names.filter(x => x === names[i]).length > 1 ? ` (${names.slice(0, i + 1).filter(x => x === names[i]).length})` : '');
  return <section className="canvas-results" aria-label="Results">
    <div className="canvas-tabs" role="tablist">{outputs.map((n, i) => <button type="button" role="tab" key={n.id} aria-selected={n.id === current.id} onClick={() => onSelect(n.id)}>
      {tabName(i)}{stale(n.id) ? ' (changed)' : ''}</button>)}</div>
    {stale(current.id) && <p role="status">This branch changed after the run. Run again to see its current result.</p>}
    {current.type === 'gallery' ? <CanvasCards key={result.token + ':' + current.id} id={current.id} study={study} result={r} onContinue={ids => onContinue(current.id, ids)} />
      : <ResearchBlockResult key={result.token + ':' + current.id} id={current.id} type={current.type === 'table' ? 'table' : current.type as 'summary'} document={study} result={r}
        fixedStudy={false} onKeepComparison={current.type === 'table' ? ids => onContinue(current.id, ids) : undefined} />}
    {graph.nodes.length !== result.graph.nodes.length && <small>Nodes added after the run are not shown until the next run.</small>}
  </section>;
}
