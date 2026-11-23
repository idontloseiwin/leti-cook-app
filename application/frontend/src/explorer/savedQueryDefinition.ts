import { researchFields, validScope, videoSource, channelSource, type ResearchSource, type ResearchFilters, type ResearchField, type ResearchType, type Expression, type ResearchSort } from './researchQuery';
import { SavedWorkContractError, savedRecord } from './savedWork';
import { TIME_ZONE_PATTERN } from './timeZones';
export type SavedQuery = {
  query_version: 1; source: { id: ResearchSource }; scope: { kind: 'channel' | 'folder' | 'label' | 'collection'; id: string; include_descendants?: false };
  video_types?: ResearchType[]; select: ResearchField[]; where?: Expression | null; order_by: (ResearchSort & { nulls?: 'last' })[];
  time?: { mode: 'current'; timezone: string }; observations?: { version: 1; window_seconds: 3600 | 86400; tolerance_seconds: number; age_seconds: 86400; age_tolerance_seconds: number };
  coverage?: { mode: 'available' | 'require_complete'; required_fields?: ResearchField[] };
  projection?: { missing_capability: 'error' }; consistency?: 'current'; page?: { size: number };
};
// Where a saved query opens and how its rows are shown; never part of the query.
// library: the channel's Library Videos tab. time_zone: the zone its dates were entered in.
export type SavedPresentation = { version: 1; surface: 'library'; view: 'grid' | 'table'; density?: 'large' | 'compact'; columns?: ResearchField[]; time_zone?: string };
export type SavedQueryDefinition = { version: 2; query: SavedQuery; presentation?: SavedPresentation };
function fail(): never { throw new SavedWorkContractError('invalid saved query definition.'); }
function exact(value: unknown, required: string[], optional: string[] = []) {
  const row = savedRecord(value);
  if (required.some(key => !Object.hasOwn(row, key)) || Object.keys(row).some(key => !required.includes(key) && !optional.includes(key))) fail();
  return row;
}
const field = (value: unknown): value is string => typeof value === 'string' && /^[a-z][a-z0-9_.]{0,95}$/.test(value);
export function parseSavedDefinition(value: unknown): SavedQueryDefinition {
  const root = exact(value, ['version', 'query'], ['presentation']);
  if (root.version !== 2 || new TextEncoder().encode(JSON.stringify(value)).length > 8192) fail();
  const q = exact(root.query, ['query_version','source','scope','select','order_by'], ['video_types','where','time','observations','coverage','projection','consistency','page']);
  const source = exact(q.source,['id']), scope = exact(q.scope,['kind','id'],['include_descendants']);
  if (q.query_version !== 1 || ![videoSource,channelSource].includes(source.id as ResearchSource) || (scope.include_descendants !== undefined && scope.include_descendants !== false)) fail();
  if (scope.kind === 'channel' ? scope.id !== '$channel' : !validScope(scope as SavedQuery['scope'])) fail();
  if (source.id===channelSource ? Object.hasOwn(q,'video_types')||Object.hasOwn(q,'observations')||scope.kind==='collection' : !Array.isArray(q.video_types)||q.video_types.length!==1||!['long','short','stream'].includes(q.video_types[0])) fail();
  if (!Array.isArray(q.select) || !q.select.length || q.select.length > 50 || !q.select.every(field) || new Set(q.select).size !== q.select.length) fail();
  if (!Array.isArray(q.order_by) || !q.order_by.length || q.order_by.length > 3) fail();
  q.order_by.forEach(value => { const sort = exact(value,['field','direction'],['nulls']); if (!field(sort.field) || !['asc','desc'].includes(String(sort.direction)) || sort.nulls !== undefined && sort.nulls !== 'last') fail(); });
  let leaves = 0;
  const expression = (value: unknown, depth = 0): void => {
    if (depth > 8) fail();
    const node = savedRecord(value);
    if ('all' in node || 'any' in node) {
      const key = 'all' in node ? 'all' : 'any'; exact(node,[key]);
      if (!Array.isArray(node[key]) || !(node[key] as unknown[]).length || (node[key] as unknown[]).length > 128) fail();
      (node[key] as unknown[]).forEach(value => expression(value,depth+1));
    } else if ('not' in node) { exact(node,['not']); expression(node.not,depth+1); }
    else {
      exact(node,['field','op'],['value']);
      // Operator ids may carry a version digit, like the server's text search contains_casefold_v1.
      if (++leaves > 128 || !field(node.field) || typeof node.op !== 'string' || !/^[a-z][a-z0-9_]{0,31}$/.test(node.op)) fail();
      if (Object.hasOwn(node,'value') && !(typeof node.value === 'string' || typeof node.value === 'boolean' || Array.isArray(node.value) && node.value.length <= 1000 && node.value.every(v => typeof v === 'string'))) fail();
    }
  };
  if (q.where !== undefined && q.where !== null) expression(q.where);
  // time.timezone: UTC or any IANA zone name the server accepted; offsets and free text stay refused.
  if (q.time !== undefined) { const t = exact(q.time,['mode','timezone']); if (t.mode !== 'current' || typeof t.timezone !== 'string' || t.timezone.length > 64 || !TIME_ZONE_PATTERN.test(t.timezone)) fail(); }
  if (q.coverage !== undefined) { const c = exact(q.coverage,['mode'],['required_fields']); if (!['available','require_complete'].includes(String(c.mode)) || c.required_fields !== undefined && (!Array.isArray(c.required_fields) || c.required_fields.length > 50 || !c.required_fields.every(field))) fail(); }
  if (q.projection !== undefined && exact(q.projection,['missing_capability']).missing_capability !== 'error') fail();
  if (q.consistency !== undefined && q.consistency !== 'current') fail();
  if (q.page !== undefined) { const p = exact(q.page,['size']); if (!Number.isInteger(p.size) || Number(p.size) < 1 || Number(p.size) > 200) fail(); }
  if (q.observations !== undefined) {
    const o = exact(q.observations,['version','window_seconds','tolerance_seconds','age_seconds','age_tolerance_seconds']);
    if (o.version !== 1 || (typeof o.window_seconds !== 'number' || ![3600,86400].includes(o.window_seconds)) || o.age_seconds !== 86400 || !Number.isInteger(o.tolerance_seconds) || !Number.isInteger(o.age_tolerance_seconds) || Number(o.tolerance_seconds)<0 || Number(o.tolerance_seconds)>Math.min(7200,Number(o.window_seconds)) || Number(o.age_tolerance_seconds)<0 || Number(o.age_tolerance_seconds)>7200) fail();
  }
  if (root.presentation !== undefined) {
    const p = exact(root.presentation, ['version', 'surface', 'view'], ['density', 'columns', 'time_zone']);
    if (p.version !== 1 || p.surface !== 'library' || !['grid', 'table'].includes(String(p.view))
      || scope.kind !== 'channel' || source.id !== videoSource) fail();
    if (p.density !== undefined && !['large', 'compact'].includes(String(p.density))) fail();
    if (p.columns !== undefined && (!Array.isArray(p.columns) || !p.columns.length || p.columns.length > 50 || new Set(p.columns).size !== p.columns.length
      || !p.columns.every(column => typeof column === 'string' && (q.select as string[]).includes(column)))) fail();
    if (p.time_zone !== undefined && (typeof p.time_zone !== 'string' || p.time_zone.length > 64 || !TIME_ZONE_PATTERN.test(p.time_zone))) fail();
  }
  const normalized = structuredClone(value) as SavedQueryDefinition;
  const query = normalized.query;
  query.where ??= null; query.time ??= { mode:'current', timezone:'UTC' };
  if(query.source.id===videoSource) query.observations ??= { version:1, window_seconds:3600, tolerance_seconds:1800, age_seconds:86400, age_tolerance_seconds:7200 };
  query.coverage ??= { mode:'available' }; query.coverage.required_fields ??= [];
  query.projection ??= { missing_capability:'error' }; query.consistency ??= 'current'; query.page ??= { size:50 };
  query.order_by = query.order_by.map(s => ({ ...s, nulls:'last' }));
  return normalized;
}
export function savedQueryFilters(definition: SavedQueryDefinition, channel: string | null): ResearchFilters {
  const q = parseSavedDefinition(definition).query;
  const scope = { ...q.scope, id: q.scope.kind === 'channel' ? channel ?? '' : q.scope.id };
  if (!validScope(scope) || q.select.some(id => !researchFields.includes(id)) || q.order_by.some(s => !researchFields.includes(s.field))) fail();
  return { source:q.source.id, scope, type:q.video_types?.[0]??'long', columns:q.select, where:q.where ?? null, order:q.order_by, complete:q.coverage?.mode==='require_complete',
    exactProjection:true, observations:q.observations, requiredFields:q.coverage?.required_fields, pageSize:q.page?.size };
}
export function canonicalSaved(value: unknown): string {
  const visit = (x: unknown): unknown => Array.isArray(x) ? x.map(visit) : x && typeof x === 'object'
    ? Object.fromEntries(Object.entries(x).sort(([a],[b]) => a < b ? -1 : a > b ? 1 : 0).map(([k,v]) => [k,visit(v)])) : x;
  return JSON.stringify(visit(value));
}
