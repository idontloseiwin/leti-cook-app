import { researchLabels, type Expression, type FieldDefinition, type Predicate, type ResearchField } from './researchQuery';

const names: Record<string, string> = { eq: 'equals', in: 'is one of', gt: 'greater than', gte: 'at least', lt: 'less than', lte: 'at most',
  contains_casefold_v1: 'contains (ignore case)', has_any: 'contains any', has_all: 'contains all',
  is_known: 'is known', is_unknown: 'is unknown', state_is: 'has state', within_last_hours_v1: 'within the last (hours)' };
function predicate(field: FieldDefinition): Predicate {
  return { field: field.id, op: field.operators.includes('contains_casefold_v1') ? 'contains_casefold_v1' : 'is_known',
    ...(field.operators.includes('contains_casefold_v1') ? { value: '' } : {}) };
}
export function firstCondition(fields: FieldDefinition[]): Predicate {
  return predicate(fields.find(field => field.id === 'video.title') ?? fields[0]);
}
export function conditionCount(value: Expression | null): number {
  if (!value) return 0;
  if ('field' in value) return 1;
  if ('not' in value) return conditionCount(value.not);
  return ('all' in value ? value.all : value.any).reduce((n, child) => n + conditionCount(child), 0);
}
export function ResearchConditions({ value, fields, onChange, depth = 0, budget = 64 }: {
  value: Expression; fields: FieldDefinition[]; onChange: (value: Expression | null) => void; depth?: number; budget?: number;
}) {
  if ('field' in value) {
    const field = fields.find(item => item.id === value.field);
    const unary = value.op === 'is_known' || value.op === 'is_unknown';
    const list = ['in', 'has_any', 'has_all'].includes(value.op);
    const changeOperator = (op: string) => onChange({ field: value.field, op,
      ...(['is_known', 'is_unknown'].includes(op) ? {} : { value: op === 'state_is' ? 'unknown' : ['in', 'has_any', 'has_all'].includes(op) ? [] : field?.value_type === 'boolean' ? true : '' }) });
    return <div className="research-condition">
      <select aria-label="Condition field" value={value.field} onChange={e => onChange(predicate(fields.find(f => f.id === e.target.value)!))}>
        {!field && <option value={value.field}>{Object.hasOwn(researchLabels, value.field) ? researchLabels[value.field] + ' (disabled)' : value.field + ' (not available on this server)'}</option>}
        {fields.map(f => <option key={f.id} value={f.id}>{researchLabels[f.id]}</option>)}
      </select>
      <select aria-label="Condition operator" value={value.op} onChange={e => changeOperator(e.target.value)}>
        {(field?.operators ?? [value.op]).map(op => <option key={op} value={op}>{names[op] ?? op}</option>)}
      </select>
      {!unary && (value.op === 'state_is'
        ? <select aria-label="Value state" value={String(value.value)} onChange={e => onChange({ ...value, value: e.target.value })}>{['known', 'unknown', 'expired', 'unavailable'].map(s => <option key={s}>{s}</option>)}</select>
        : field?.value_type === 'boolean'
        ? <select aria-label="Condition value" value={String(value.value)} onChange={e => onChange({ ...value, value: e.target.value === 'true' })}><option value="true">Yes</option><option value="false">No</option></select>
        : list
        ? <textarea aria-label="Condition values, one per line" rows={2} maxLength={30000} placeholder="One exact value per line" value={Array.isArray(value.value) ? value.value.join('\n') : ''} onChange={e => onChange({ ...value, value: e.target.value.split('\n') })} />
        : <input aria-label="Condition value" type="text" inputMode={field?.value_type === 'uint_decimal' ? 'numeric' : 'text'}
            maxLength={field?.value_type === 'uint_decimal' ? 20 : 1000} value={String(value.value ?? '')}
            placeholder={field?.value_type === 'utc_instant' ? '2026-01-01T00:00:00Z (UTC)' : field?.value_type === 'local_date' ? '2026-01-31 (study zone)'
              : field?.id === 'observation.growth_28d_interval_seconds' ? 'Seconds (whole number)' : field?.value_type === 'uint_decimal' ? 'Whole number' : 'Value'}
            onChange={e => onChange({ ...value, value: e.target.value })} />)}
      {depth < 5 && <button type="button" onClick={() => onChange({ not: value })} title="Exclude this condition">NOT</button>}
      <button type="button" onClick={() => onChange(null)} aria-label="Remove condition">Remove</button>
    </div>;
  }
  if ('not' in value) return <fieldset className="research-group"><legend>NOT</legend>
    <ResearchConditions value={value.not} fields={fields} depth={depth + 1} budget={budget} onChange={child => onChange(child ? { not: child } : null)} />
    <button type="button" onClick={() => onChange(value.not)}>Remove NOT</button>
  </fieldset>;
  const mode = 'all' in value ? 'all' : 'any', children = 'all' in value ? value.all : value.any;
  const update = (next: Expression[]) => onChange(next.length ? mode === 'all' ? { all: next } : { any: next } : null);
  return <fieldset className="research-group"><legend>Condition group</legend>
    <select aria-label="Match conditions" value={mode} onChange={e => onChange(e.target.value === 'all' ? { all: children } : { any: children })}>
      <option value="all">Match ALL (AND)</option><option value="any">Match ANY (OR)</option>
    </select>
    {children.map((child, i) => <ResearchConditions key={i} value={child} fields={fields} depth={depth + 1} budget={budget}
      onChange={next => update(children.flatMap((item, n) => n !== i ? [item] : next ? [next] : []))} />)}
    <div className="research-actions">
      <button type="button" disabled={budget <= 0 || !fields.length} onClick={() => update([...children, firstCondition(fields)])}>Add condition</button>
      <button type="button" disabled={budget <= 0 || depth >= 4 || !fields.length} onClick={() => update([...children, { all: [firstCondition(fields)] }])}>Add group</button>
      <button type="button" onClick={() => onChange(null)}>Remove group</button>
    </div>
  </fieldset>;
}
