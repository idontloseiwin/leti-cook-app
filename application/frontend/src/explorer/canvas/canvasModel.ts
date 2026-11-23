// The research canvas (graph version 2): pure model functions, no React. The server (research_query/graph.py,
// normalize_graph_v2) is the authority; these mirror its rules so the canvas can explain a connection before a run.
import { requestJson } from '../api';
import { canonicalSaved, type SavedQuery } from '../savedQueryDefinition';
import { savedRecord } from '../savedWork';
import { channelSource, defaultColumns, expressionFields, researchLabels, researchTypeNames, videoSource,
  type Expression, type ResearchField, type ResearchSort, type ResearchSource, type ResearchType } from '../researchQuery';
import { parseOperation } from '../researchOperations';
import { CANVAS_NOTES_BYTES, CHANNEL_COLUMNS, OPERATION_OF_BLOCK, SAVED_CANVAS_BYTES, parseBlockPage, blockFilters, type GraphResult, type NodeKind, type NodeResult,
  type ResearchDocument } from '../researchGraph';
import { WORKSHOP_BLOCKS } from '../workshopModel';
import { sourceExpired, sourceLifetime, transferSourceLifetime } from '../../sourceLifetime';

export type CanvasKind = 'source' | 'filter' | 'sort' | 'selection' | 'gallery' | 'table' | 'summary' | 'calendar' | 'history';
export type CanvasNode = { id: string; type: CanvasKind; parameters: Record<string, unknown> };
export type CanvasEdge = { from: string; to: string; output: 'rows'; input: 'rows' };
export type CanvasGraph = { version: 2; nodes: CanvasNode[]; edges: CanvasEdge[]; notes: string; layout: Record<string, { x: number; y: number }> };
export type Entity = 'video' | 'channel';
export type ScopeKind = 'channel' | 'folder' | 'label' | 'collection';
export type SourceParams = {
  source: { id: ResearchSource }; scope: { kind: ScopeKind; id: string; include_descendants?: false };
  video_types: ResearchType[] | null; time: { mode: 'current'; timezone: string };
  // As a study's query had them; the query's defaults (available data, a 1-hour change window) apply without them.
  coverage?: SavedQuery['coverage']; observations?: SavedQuery['observations'];
};
const DEFAULT_WINDOW = { version: 1, window_seconds: 3600, tolerance_seconds: 1800, age_seconds: 86400, age_tolerance_seconds: 7200 };
export type Category = 'source' | 'process' | 'calculation' | 'view';

// Budgets of the server (graph.GRAPH_V2_NODES / GRAPH_V2_OUTPUTS, MAX_DEFINITION, CANVAS_NOTES_BYTES).
export const CANVAS_NODES = 16, CANVAS_OUTPUTS = 6, CANVAS_BYTES = SAVED_CANVAS_BYTES, CANVAS_NOTES = CANVAS_NOTES_BYTES;

// ---- Size: the server counts UTF-8 bytes (a Cyrillic letter takes 2), so the canvas counts them too ----
const encoder = new TextEncoder();
/** UTF-8 bytes of a text: the server's measure of notes. */
export const textBytes = (text: string): number => encoder.encode(text).length;
/** The canvas as Save sends it ({version: 4, graph}), in UTF-8 bytes: the server's measure of the whole canvas. */
export const canvasBytes = (graph: CanvasGraph): number => textBytes(canonicalSaved({ version: 4, graph }));
const count = (n: number) => n.toLocaleString('en-US');
const notesLimit = `Notes are limited to ${count(CANVAS_NOTES)} bytes (about ${count(CANVAS_NOTES / 2)} Cyrillic letters)`;
const canvasLimit = `A canvas is limited to ${count(CANVAS_BYTES)} bytes`;
const shorten = 'Shorten the notes, pick fewer items or remove a node.';
/** Why this canvas cannot be saved or run for its size, or null. */
export function sizeProblem(graph: CanvasGraph): string | null {
  const notes = textBytes(graph.notes), bytes = canvasBytes(graph);
  if (notes > CANVAS_NOTES) return `${notesLimit}; they take ${count(notes)}. Shorten the notes.`;
  if (bytes > CANVAS_BYTES) return `${canvasLimit}; this one takes ${count(bytes)}. ${shorten}`;
  return null;
}
/** Why an edit is not made, or null. An edit never makes the notes or the canvas larger than Save accepts; an edit
 *  that makes an oversized canvas smaller is made. */
export function editSizeProblem(current: CanvasGraph, next: CanvasGraph): string | null {
  const notes = textBytes(next.notes);
  if (notes > CANVAS_NOTES && notes > textBytes(current.notes)) return `${notesLimit}; this edit would make them ${count(notes)}. Shorten the notes.`;
  const bytes = canvasBytes(next);
  if (bytes > CANVAS_BYTES && bytes > canvasBytes(current)) return `${canvasLimit}; this edit would make it ${count(bytes)}. ${shorten}`;
  return null;
}

type Spec = { category: Category; label: string; hint: string; input: boolean; output: boolean; entities: readonly Entity[]; operation?: string;
  needsChannelScope?: true };
/** Every node of the first canvas, grouped by the catalogue's categories. */
export const CANVAS_CATALOG: Readonly<Record<CanvasKind, Spec>> = {
  source: { category: 'source', label: 'Source', hint: 'Stored videos or channels of a channel, folder, label or collection', input: false, output: true, entities: ['video', 'channel'] },
  filter: { category: 'process', label: 'Filter', hint: 'Keep the rows that match conditions on real fields', input: true, output: true, entities: ['video', 'channel'] },
  sort: { category: 'process', label: 'Sort', hint: 'Order the rows by up to three fields', input: true, output: true, entities: ['video', 'channel'] },
  selection: { category: 'process', label: 'Selected items', hint: 'Only, or all but, the items you picked', input: true, output: true, entities: ['video', 'channel'] },
  gallery: { category: 'view', label: 'Cards', hint: 'Video cards with thumbnails, as in Library', input: true, output: false, entities: ['video'] },
  table: { category: 'view', label: 'Table', hint: 'Rows with the columns you choose', input: true, output: false, entities: ['video', 'channel'] },
  summary: { category: 'calculation', label: 'Fingerprint', hint: 'Exact summaries and distributions of the rows', input: true, output: false, entities: ['video'], operation: OPERATION_OF_BLOCK.summary },
  calendar: { category: 'calculation', label: 'Publishing calendar', hint: 'Uploads by day, hour and weekday', input: true, output: false, entities: ['video'], operation: OPERATION_OF_BLOCK.calendar },
  history: { category: 'calculation', label: 'Channel history', hint: 'Views or subscribers of one channel over time', input: true, output: false, entities: ['channel'], operation: OPERATION_OF_BLOCK.history, needsChannelScope: true },
};
export const CATEGORY_NAMES: Record<Category, string> = { source: 'Sources', process: 'Processing', calculation: 'Calculations', view: 'Views' };
export const isResult = (type: CanvasKind) => !CANVAS_CATALOG[type].output;
const ROW_TYPES: readonly CanvasKind[] = ['gallery', 'table'];
export const GALLERY_COLUMNS: ResearchField[] = ['video.title', 'video.thumbnail_url', 'channel.title', 'video.published_at', 'video.duration_seconds', 'video.views'];
const fail = (): never => { throw new Error('Unsupported or invalid canvas.'); };

export function sourceParams(timezone: string, entity: Entity = 'video'): SourceParams {
  return { source: { id: entity === 'channel' ? channelSource : videoSource }, scope: { kind: 'folder', id: '', include_descendants: false },
    video_types: entity === 'channel' ? null : ['long'], time: { mode: 'current', timezone } };
}
const entityOfSource = (params: Record<string, unknown>): Entity => (savedRecord(params.source).id === channelSource ? 'channel' : 'video');
const defaultSort = (entity: Entity): ResearchSort[] => [{ field: entity === 'channel' ? 'channel.subscribers' : 'video.views', direction: 'desc' }];

/** A new node's parameters; an entity-dependent node takes its input's entity. */
export function defaultParameters(type: CanvasKind, entity: Entity, timezone: string): Record<string, unknown> {
  switch (type) {
    case 'source': return sourceParams(timezone, entity);
    case 'sort': return { order_by: defaultSort(entity) };
    case 'gallery': return { select: [...GALLERY_COLUMNS], page_size: 24 };
    case 'table': return { select: entity === 'channel' ? [...CHANNEL_COLUMNS] : [...defaultColumns], page_size: 25 };
    default: return WORKSHOP_BLOCKS[type as NodeKind].defaults(timezone);
  }
}

/** A new canvas: a Long-form source still to be chosen, feeding Cards. */
export function newCanvas(timezone: string): CanvasGraph {
  return { version: 2, nodes: [{ id: 'videos', type: 'source', parameters: sourceParams(timezone) },
    { id: 'cards', type: 'gallery', parameters: defaultParameters('gallery', 'video', timezone) }],
  edges: [{ from: 'videos', to: 'cards', output: 'rows', input: 'rows' }], notes: '', layout: { videos: { x: 40, y: 120 }, cards: { x: 420, y: 120 } } };
}

export const parentOf = (graph: CanvasGraph, id: string) => graph.edges.find(e => e.to === id)?.from;
/** The chain from the source to this node, source first; empty when the node has no path to a source (a draft). */
export function pathTo(graph: CanvasGraph, id: string): CanvasNode[] {
  const nodes = new Map(graph.nodes.map(n => [n.id, n])), path: CanvasNode[] = [], seen = new Set<string>();
  let node = nodes.get(id);
  while (node && !seen.has(node.id)) { seen.add(node.id); path.unshift(node); const parent = parentOf(graph, node.id); node = parent ? nodes.get(parent) : undefined; }
  return path[0]?.type === 'source' ? path : [];
}
export const isDraft = (graph: CanvasGraph, id: string) => pathTo(graph, id).length === 0;
export function entityOf(graph: CanvasGraph, id: string): Entity | null {
  const root = pathTo(graph, id)[0];
  return root ? entityOfSource(root.parameters) : null;
}

/** The query this node reads (results) or passes on (processing): the server's rules (graph._transform_v2). */
export function branchQuery(graph: CanvasGraph, id: string): SavedQuery {
  const path = pathTo(graph, id);
  if (!path.length) return fail();
  const source = path[0].parameters as SourceParams, channels = source.source.id === channelSource;
  const identity: ResearchField = channels ? 'channel.id' : 'video.id';
  const query: SavedQuery = { query_version: 1, source: structuredClone(source.source), scope: structuredClone(source.scope),
    ...(channels ? {} : { video_types: [...(source.video_types ?? ['long'])] }), time: structuredClone(source.time),
    select: [identity], where: null, order_by: channels ? [{ field: 'channel.title', direction: 'asc' }] : [{ field: 'video.published_at', direction: 'desc' }],
    page: { size: 25 }, ...(source.coverage ? { coverage: structuredClone(source.coverage) } : {}),
    ...(source.observations ? { observations: structuredClone(source.observations) } : {}) };
  for (const node of path.slice(1)) {
    const p = node.parameters;
    if (node.type === 'filter' && p.where) query.where = query.where ? { all: [query.where, p.where as Expression] } : p.where as Expression;
    if (node.type === 'sort') query.order_by = structuredClone(p.order_by as ResearchSort[]);
    if (node.type === 'selection') {
      let leaf: Expression = { field: identity, op: 'in', value: p.ids as string[] };
      if (p.mode === 'exclude') leaf = { not: leaf };
      query.where = query.where ? { all: [query.where, leaf] } : leaf;
    }
    if (ROW_TYPES.includes(node.type)) {
      query.select = [identity, ...(p.select as ResearchField[]).filter(f => f !== identity)].slice(0, 50);
      query.page = { size: Number(p.page_size) };
    }
  }
  return query;
}

/** Why `from` cannot feed `to`, or null. The server checks the same again on every run. */
export function connectionProblem(graph: CanvasGraph, from: string, to: string): string | null {
  const a = graph.nodes.find(n => n.id === from), b = graph.nodes.find(n => n.id === to);
  if (!a || !b || from === to) return 'Choose two different nodes.';
  if (!CANVAS_CATALOG[a.type].output) return `${CANVAS_CATALOG[a.type].label} shows a result and feeds nothing.`;
  if (!CANVAS_CATALOG[b.type].input) return 'A source has no input.';
  let current: string | undefined = from;
  const seen = new Set<string>([to]);
  while (current) { if (seen.has(current)) return 'This connection would create a loop.'; seen.add(current); current = parentOf(graph, current); }
  const entity = entityOf(graph, from);
  if (entity && !CANVAS_CATALOG[b.type].entities.includes(entity))
    return `${CANVAS_CATALOG[b.type].label} works with ${CANVAS_CATALOG[b.type].entities.map(e => e === 'video' ? 'videos' : 'channels').join(' or ')}, not ${entity === 'video' ? 'videos' : 'channels'}.`;
  return null;
}
/** Connect; an existing input of `to` is replaced (one input per node). */
export function connect(graph: CanvasGraph, from: string, to: string): CanvasGraph {
  if (connectionProblem(graph, from, to)) return graph;
  return { ...graph, edges: [...graph.edges.filter(e => e.to !== to), { from, to, output: 'rows', input: 'rows' }] };
}
export const disconnect = (graph: CanvasGraph, to: string): CanvasGraph => ({ ...graph, edges: graph.edges.filter(e => e.to !== to) });
/** "Connect to…": the nodes `from` can feed, by the rules of a dragged wire (connectionProblem), in canvas
 *  order, without the ones it already feeds. `replaces`: the input that wire would replace (one input per node). */
export function connectionTargets(graph: CanvasGraph, from: string): { id: string; replaces?: string }[] {
  return graph.nodes.filter(n => !graph.edges.some(e => e.from === from && e.to === n.id) && connectionProblem(graph, from, n.id) === null)
    .map(n => { const replaces = parentOf(graph, n.id); return replaces ? { id: n.id, replaces } : { id: n.id }; });
}
/** Every node's name in words, as menus and screen readers say it: "Filter: Views ≥ 100"; equal names get a number. */
export function nodeNames(graph: CanvasGraph, names: { scope?: (kind: string, id: string) => string } = {}): Record<string, string> {
  const plain = graph.nodes.map(n => `${CANVAS_CATALOG[n.type].label}: ${nodeSummary(graph, n, names)}`);
  return Object.fromEntries(graph.nodes.map((n, i) => {
    const same = plain.filter(text => text === plain[i]).length, at = plain.slice(0, i + 1).filter(text => text === plain[i]).length;
    return [n.id, same > 1 ? `${plain[i]} (${at})` : plain[i]];
  }));
}
/** Where a key moves the focus in a canvas menu of `count` items: an item index, 'close' for Escape, or
 *  null when the key is not the menu's. The arrows wrap, Home and End go to the ends. */
export function menuMove(key: string, at: number, count: number): number | 'close' | null {
  if (key === 'Escape') return 'close';
  if (!count) return null;
  switch (key) {
    case 'ArrowDown': return at < 0 ? 0 : (at + 1) % count;
    case 'ArrowUp': return at < 0 ? count - 1 : (at - 1 + count) % count;
    case 'Home': return 0;
    case 'End': return count - 1;
    default: return null;
  }
}

export function newNodeId(graph: CanvasGraph, type: CanvasKind): string {
  const taken = new Set(graph.nodes.map(n => n.id)), base = type === 'gallery' ? 'cards' : type;
  for (let i = 1; i < 100; i++) { const id = i === 1 ? base : `${base}_${i}`; if (!taken.has(id)) return id; }
  return fail();
}
export function budgetProblem(graph: CanvasGraph, add: CanvasKind[]): string | null {
  if (graph.nodes.length + add.length > CANVAS_NODES) return `A canvas holds up to ${CANVAS_NODES} nodes.`;
  if (graph.nodes.filter(n => isResult(n.type)).length + add.filter(isResult).length > CANVAS_OUTPUTS) return `A canvas holds up to ${CANVAS_OUTPUTS} result nodes.`;
  return null;
}
const clamp = (v: number) => Math.max(-10000, Math.min(10000, Math.round(v)));
/** The first place at or below `at` where a new node does not cover another one. */
function freeSpot(graph: CanvasGraph, at: { x: number; y: number }): { x: number; y: number } {
  let y = at.y;
  while (Object.values(graph.layout).some(p => Math.abs(p.x - at.x) < 240 && Math.abs(p.y - y) < 130) && y < at.y + 3000) y += 140;
  return { x: at.x, y };
}
/** Add a node at a position; with `parent` it is connected when the connection is allowed. */
export function addNode(graph: CanvasGraph, type: CanvasKind, at: { x: number; y: number }, timezone: string, parent?: string): { graph: CanvasGraph; id: string } {
  const id = newNodeId(graph, type), entity: Entity = parent ? entityOf(graph, parent) ?? 'video' : 'video', spot = freeSpot(graph, at);
  let next: CanvasGraph = { ...graph, nodes: [...graph.nodes, { id, type, parameters: defaultParameters(type, entity, timezone) }],
    layout: { ...graph.layout, [id]: { x: clamp(spot.x), y: clamp(spot.y) } } };
  if (parent) next = connect(next, parent, id);
  return { graph: next, id };
}
/** Put a node that was just connected after `parent` between `parent` and its only other child: the child
 * (with everything after it) moves one step right and now reads through the new node. */
export function insertBetween(graph: CanvasGraph, parent: string, id: string): CanvasGraph {
  const children = graph.edges.filter(e => e.from === parent && e.to !== id).map(e => e.to);
  if (children.length !== 1 || parentOf(graph, id) !== parent || connectionProblem(graph, id, children[0])) return graph;
  const child = children[0], moved = new Set<string>(), stack = [child];
  while (stack.length) { const n = stack.pop()!; moved.add(n); stack.push(...graph.edges.filter(e => e.from === n).map(e => e.to)); }
  const from = graph.layout[parent], layout = { ...graph.layout, [id]: { x: clamp(from.x + 380), y: from.y } };
  for (const n of moved) layout[n] = { x: clamp(graph.layout[n].x + 380), y: graph.layout[n].y };
  return { ...connect(graph, id, child), layout };
}
/** Remove nodes and their connections; their former children stay on the canvas as drafts (never removed silently). */
export function removeNodes(graph: CanvasGraph, ids: readonly string[]): CanvasGraph {
  const gone = new Set(ids);
  if (graph.nodes.filter(n => !gone.has(n.id)).length === 0) return graph; // a canvas keeps at least one node
  return { ...graph, nodes: graph.nodes.filter(n => !gone.has(n.id)), edges: graph.edges.filter(e => !gone.has(e.from) && !gone.has(e.to)),
    layout: Object.fromEntries(Object.entries(graph.layout).filter(([id]) => !gone.has(id))) };
}
/** Duplicate nodes (with the connections among them) next to the originals. */
export function duplicateNodes(graph: CanvasGraph, ids: readonly string[]): { graph: CanvasGraph; ids: string[] } {
  let next = graph; const map = new Map<string, string>();
  for (const id of ids) {
    const node = graph.nodes.find(n => n.id === id); if (!node) continue;
    const copy = newNodeId(next, node.type), at = graph.layout[id];
    map.set(id, copy);
    next = { ...next, nodes: [...next.nodes, { ...structuredClone(node), id: copy }], layout: { ...next.layout, [copy]: { x: clamp(at.x + 40), y: clamp(at.y + 160) } } };
  }
  const edges = graph.edges.filter(e => map.has(e.from) && map.has(e.to)).map(e => ({ ...e, from: map.get(e.from)!, to: map.get(e.to)! }));
  return { graph: { ...next, edges: [...next.edges, ...edges] }, ids: [...map.values()] };
}
export const moveNode = (graph: CanvasGraph, id: string, x: number, y: number): CanvasGraph => ({ ...graph, layout: { ...graph.layout, [id]: { x: clamp(x), y: clamp(y) } } });
export function setParameters(graph: CanvasGraph, id: string, parameters: Record<string, unknown>): CanvasGraph {
  return { ...graph, nodes: graph.nodes.map(n => n.id === id ? { ...n, parameters } : n) };
}

/** "Continue with selected": a Selected items node on the same input as the result node, with Cards after it. */
export function continueWithSelected(graph: CanvasGraph, resultId: string, ids: readonly string[], timezone: string): { graph: CanvasGraph; id: string } | { problem: string } {
  const parent = parentOf(graph, resultId);
  if (!parent || !ids.length) return { problem: 'Select at least one item first.' };
  if (ids.length > 1000) return { problem: 'Up to 1,000 items can be carried to the next step.' };
  const budget = budgetProblem(graph, ['selection', 'gallery']);
  if (budget) return { problem: budget };
  const at = graph.layout[resultId], entity = entityOf(graph, parent) ?? 'video';
  const build = (carried: readonly string[]) => {
    const picked = addNode(graph, 'selection', { x: at.x, y: at.y + 220 }, timezone, parent);
    const next = setParameters(picked.graph, picked.id, { mode: 'include', ids: [...carried] });
    return { graph: addNode(next, entity === 'channel' ? 'table' : 'gallery', { x: at.x + 380, y: at.y + 220 }, timezone, picked.id).graph, id: picked.id };
  };
  const made = build(ids), bytes = canvasBytes(made.graph);
  if (bytes <= CANVAS_BYTES) return made;
  // Nothing is cut silently: the person learns how many fit (IDs of one kind have one length, so more never fit fewer).
  let fit = 0, high = ids.length - 1;
  while (fit < high) { const mid = Math.ceil((fit + high) / 2); if (canvasBytes(build(ids.slice(0, mid)).graph) <= CANVAS_BYTES) fit = mid; else high = mid - 1; }
  const over = `the canvas would take ${count(bytes)} of ${count(CANVAS_BYTES)} bytes.`;
  return { problem: fit ? `These ${count(ids.length)} items do not fit: ${over} Up to ${count(fit)} fit; pick fewer or shorten the notes.`
    : `No picked items fit: ${over} Shorten the notes or remove a node.` };
}

// ---- Words a person reads on a node ----
const fieldName = (id: string) => (researchLabels as Record<string, string>)[id] ?? id;
const OP: Record<string, string> = { eq: '=', in: 'is one of', gt: '>', gte: '≥', lt: '<', lte: '≤', contains_casefold_v1: 'contains', has_any: 'has any of',
  has_all: 'has all of', is_known: 'is known', is_unknown: 'is unknown', state_is: 'state is', within_last_hours_v1: 'within the last (h)' };
function number(value: string): string { return /^\d+$/.test(value) ? BigInt(value).toLocaleString('en-US') : value; }
function predicateText(e: Expression): string {
  if ('field' in e) {
    const value = Array.isArray(e.value) ? e.value.slice(0, 3).join(', ') + (e.value.length > 3 ? '…' : '') : e.value === undefined ? '' : typeof e.value === 'string' ? number(e.value) : String(e.value);
    if (e.field === 'video.duration_seconds' && typeof e.value === 'string' && /^\d+$/.test(e.value)) {
      const s = Number(e.value); return `${fieldName(e.field)} ${OP[e.op] ?? e.op} ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    }
    return `${fieldName(e.field)} ${OP[e.op] ?? e.op}${value ? ' ' + value : ''}`;
  }
  if ('not' in e) return `not (${predicateText(e.not)})`;
  const parts = 'all' in e ? e.all : e.any;
  return parts.length === 1 ? predicateText(parts[0]) : `${parts.length} conditions (${'all' in e ? 'all' : 'any'})`;
}
/** One short line of what the node does, in words. */
export function nodeSummary(graph: CanvasGraph, node: CanvasNode, names: { scope?: (kind: string, id: string) => string } = {}): string {
  const p = node.parameters;
  switch (node.type) {
    case 'source': {
      const s = p as SourceParams, what = s.source.id === channelSource ? 'Channels' : researchTypeNames[s.video_types?.[0] ?? 'long'];
      const rules = (s.coverage?.mode === 'require_complete' ? ' · complete data only' : '') + (s.observations?.window_seconds === 86400 ? ' · 1-day change' : '');
      if (!s.scope.id) return `${what} · choose a ${s.scope.kind}${rules}`;
      return `${what} · ${s.scope.kind} ${names.scope?.(s.scope.kind, s.scope.id) ?? s.scope.id}${rules}`;
    }
    case 'filter': return p.where ? predicateText(p.where as Expression) : 'No condition yet';
    case 'sort': return (p.order_by as ResearchSort[]).map(s => `${fieldName(s.field)} ${s.direction === 'desc' ? '↓' : '↑'}`).join(', ');
    case 'selection': return `${p.mode === 'exclude' ? 'All but' : 'Only'} ${(p.ids as string[]).length} picked`;
    case 'gallery': case 'table': return `${(p.select as string[]).length} fields · ${p.page_size} per page`;
    case 'calendar': return `Year ${p.year}`;
    case 'history': return `${p.metric === 'subscribers' ? 'Subscribers' : 'Views'} over time`;
    default: return CANVAS_CATALOG[node.type].hint;
  }
}
/** Every field the canvas reads (for the "disabled field" check). */
export function canvasFields(graph: CanvasGraph): string[] {
  return [...new Set(graph.nodes.flatMap(n => n.type === 'filter' ? expressionFields((n.parameters.where ?? null) as Expression | null)
    : n.type === 'sort' ? (n.parameters.order_by as ResearchSort[]).map(s => s.field) : ROW_TYPES.includes(n.type) ? n.parameters.select as string[] : []))];
}

// ---- Old studies (version 1, one study query) open on the canvas without loss ----
/** Study query → its source node, a Filter (its conditions), a Sort (its order); result blocks take its columns and page. */
export function fromStudy(document: ResearchDocument): CanvasGraph {
  const q = document.query, channels = q.source.id === channelSource, old = document.graph;
  const source = old.nodes.find(n => n.type === 'source')!;
  const nodes: CanvasNode[] = [], edges: CanvasEdge[] = [], layout: CanvasGraph['layout'] = {};
  const at = (id: string) => old.layout[id] ?? { x: 0, y: 0 };
  // The study's completeness rule and change window stay with its source (only when they differ from the defaults).
  const coverage = q.coverage && (q.coverage.mode !== 'available' || q.coverage.required_fields?.length) ? { coverage: structuredClone(q.coverage) } : {};
  const observations = !channels && q.observations && canonicalSaved(q.observations) !== canonicalSaved(DEFAULT_WINDOW) ? { observations: structuredClone(q.observations) } : {};
  nodes.push({ id: source.id, type: 'source', parameters: { source: { id: q.source.id }, scope: structuredClone(q.scope), video_types: channels ? null : [...(q.video_types ?? ['long'])],
    time: { mode: 'current', timezone: q.time?.timezone ?? 'UTC' }, ...coverage, ...observations } });
  layout[source.id] = at(source.id);
  let head = source.id; const taken = new Set(old.nodes.map(n => n.id));
  const fresh = (base: string) => { for (let i = 1; ; i++) { const id = i === 1 ? base : `${base}_${i}`; if (!taken.has(id)) { taken.add(id); return id; } } };
  const shift = (dx: number) => ({ x: Math.max(-10000, Math.min(10000, at(source.id).x + dx)), y: at(source.id).y + 140 });
  if (q.where) { const id = fresh('study_conditions'); nodes.push({ id, type: 'filter', parameters: { where: structuredClone(q.where) } }); edges.push({ from: head, to: id, output: 'rows', input: 'rows' }); layout[id] = shift(0); head = id; }
  const order = q.order_by.map(s => ({ field: s.field, direction: s.direction }));
  { const id = fresh('study_order'); nodes.push({ id, type: 'sort', parameters: { order_by: order } }); edges.push({ from: head, to: id, output: 'rows', input: 'rows' }); layout[id] = shift(200); head = id; }
  for (const node of old.nodes) {
    if (node.type === 'source') continue;
    const parameters = ROW_TYPES.includes(node.type as CanvasKind) ? { select: q.select.filter(f => f !== (channels ? 'channel.id' : 'video.id')), page_size: Math.min(100, q.page?.size ?? 25) } : structuredClone(node.parameters);
    nodes.push({ id: node.id, type: node.type as CanvasKind, parameters });
    layout[node.id] = { x: at(node.id).x + 420, y: at(node.id).y };
  }
  for (const edge of old.edges) edges.push({ from: edge.from === source.id ? head : edge.from, to: edge.to, output: 'rows', input: 'rows' });
  return { version: 2, nodes, edges, notes: old.notes, layout };
}

/** A one-source, one-result study of the query this node reads: the shape the existing result views understand. */
export function nodeStudy(graph: CanvasGraph, id: string): ResearchDocument {
  const node = graph.nodes.find(n => n.id === id) ?? fail(), query = branchQuery(graph, id);
  const type: NodeKind = isResult(node.type) ? node.type as NodeKind : 'table';
  const parameters = isResult(node.type) && !ROW_TYPES.includes(node.type) ? structuredClone(node.parameters) : {};
  const origin = id === 'origin' ? 'origin_source' : 'origin'; // a node id of the version 1 shape ([a-z][a-z0-9_]*)
  return { version: 1, query, graph: { version: 1, nodes: [{ id: origin, type: 'source', parameters: {} }, { id, type, parameters }],
    edges: [{ from: origin, to: id, input: 'query', output: 'query' }], notes: '', layout: { [origin]: { x: 0, y: 0 }, [id]: { x: 300, y: 0 } } } };
}

export type CanvasResult = Omit<GraphResult, 'nodes'> & { nodes: Record<string, NodeResult | { state: 'draft' }> };
/** Run the canvas on stored data (never YouTube) and check every block's answer against its own query. */
/** How a run reads rows: current reads, or Library's live fixed list reused on the branches with its exact query. */
export type CanvasRun = { consistency: 'current' } | { consistency: 'frozen'; frozen_ref: string };
export async function runCanvas(graph: CanvasGraph, signal: AbortSignal, options: CanvasRun = { consistency: 'current' }): Promise<CanvasResult> {
  const value = await requestJson('/research/graphs/execute', signal, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: canonicalSaved({ version: 2, graph, run: options }) });
  const root = savedRecord(value), context = savedRecord(root.context), results = savedRecord(root.nodes);
  const run = savedRecord(root.run), fixed = run.fixed === null ? null : savedRecord(run.fixed);
  if (fixed && (fixed.reused !== true || typeof fixed.valid_before !== 'string' || !Number.isFinite(Date.parse(fixed.valid_before))
    || !Number.isSafeInteger(fixed.total_count) || Number(fixed.total_count) < 0)) return fail();
  if (root.version !== 1 || !['ready', 'partial'].includes(String(root.status)) || typeof root.graph_fingerprint !== 'string' || !/^[a-f0-9]{64}$/.test(root.graph_fingerprint)
    || run.consistency !== options.consistency || (options.consistency === 'current' && fixed !== null)
    || context.consistency !== (fixed ? 'fixed_source_list' : 'sequential_current_reads')
    || typeof context.valid_before !== 'string' || !Number.isFinite(Date.parse(context.valid_before)) || !sourceLifetime(value) || sourceExpired(value)
    || Object.keys(results).sort().join() !== graph.nodes.map(n => n.id).sort().join()) return fail();
  for (const node of graph.nodes) {
    const r = savedRecord(results[node.id]);
    if (isDraft(graph, node.id)) { if (r.state !== 'draft' || Object.keys(r).length !== 1) return fail(); continue; }
    if (r.state === 'error') { if (typeof r.code !== 'string' || !/^[a-z_]{1,80}$/.test(r.code) || r.value !== undefined) return fail(); continue; }
    if (r.state !== 'ready') return fail();
    if (!isResult(node.type)) { if (r.output !== 'query' || r.value !== undefined) return fail(); continue; }
    const payload = savedRecord(r.value), nodeContext = savedRecord(payload.context);
    if (typeof nodeContext.valid_before !== 'string' || !Number.isFinite(Date.parse(nodeContext.valid_before))) return fail();
    const tagged = transferSourceLifetime(value, { ...payload, source_read_at: root.source_read_at, source_expires_at: nodeContext.valid_before });
    const study = nodeStudy(graph, node.id);
    if (node.type in OPERATION_OF_BLOCK) {
      if (r.output !== 'operation') return fail();
      r.value = parseOperation(tagged, OPERATION_OF_BLOCK[node.type as keyof typeof OPERATION_OF_BLOCK], study.query.source.id);
    } else {
      if (r.output !== 'page' || !(r.consistency === 'current' || r.consistency === 'frozen' && fixed) || typeof r.query !== 'object' || r.query === null) return fail();
      r.value = parseBlockPage(tagged, blockFilters(study, node.id));
    }
  }
  return value as CanvasResult;
}
