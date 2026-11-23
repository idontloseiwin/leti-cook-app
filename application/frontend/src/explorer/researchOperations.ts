import { sourceExpired, sourceLifetime } from '../sourceLifetime';

export const summaryOperation = 'video.summary.v1';
export const calendarOperation = 'video.publication_calendar.v1';
export const historyOperation = 'channel.observed_history.v1';
export type OperationId = typeof summaryOperation | typeof calendarOperation | typeof historyOperation;
export const metrics = ['views','duration_seconds','likes','comments','title_length','tag_count'] as const;
export type MetricSummary = { definition_epoch:string|null; state:'known'|'no_known_values'|'mixed_definition_epochs'|'unknown_definition_epoch'; known_count:number; missing_count:number; total:string|null; mean:string|null; median:string|null; q1:string|null; q3:string|null; min:string|null; max:string|null; buckets:{key:string;label:string;count:number}[] };
export type SummaryResult = { population:number; metrics:Record<typeof metrics[number],MetricSummary> };
export type CalendarResult = { population:number; known_count:number; year:number; time_zone:string; days:{date:string;count:number}[]; hours:{hour:number;count:number}[]; weekdays:{weekday:number;count:number}[] };
export type HistoryResult = { metric:'views'|'subscribers'; definition_epoch:null; method:string; hours:number; bucket_hours:number; observations:number; points:{recorded_at:string;value:string;expires_at:string}[] };
export type OperationResult = { version:1; operation:OperationId; operation_fingerprint:string; context:{source_id:string;evaluated_at:string;valid_before:string;observation_age_limit_seconds?:number|null}; coverage:{status:'complete'|'partial';reasons:Record<string,number>}; result:SummaryResult|CalendarResult|HistoryResult };
const invalid = ():never=>{throw new Error('The calculation’s answer could not be verified. Try again.');};
function obj(v:unknown):Record<string,unknown>{if(!v||typeof v!=='object'||Array.isArray(v))return invalid();return v as Record<string,unknown>;}
function count(v:unknown):number{if(typeof v!=='number'||!Number.isSafeInteger(v)||v<0)return invalid();return v;}
function stamp(v:unknown):number{if(typeof v!=='string'||!/^\d{4}-\d\d-\d\dT/.test(v)||!Number.isFinite(Date.parse(v)))return invalid();return Date.parse(v);}
export function parseOperation(value:unknown,operation:OperationId,source:string):OperationResult{
  const root=obj(value),context=obj(root.context),coverage=obj(root.coverage),r=obj(root.result);
  if(root.version!==1||root.operation!==operation||typeof root.operation_fingerprint!=='string'||!/^[a-f0-9]{64}$/.test(root.operation_fingerprint)
    ||context.source_id!==source||context.consistency!=='current'||!sourceLifetime(value)||sourceExpired(value)
    ||stamp(root.source_expires_at)!==stamp(context.valid_before)||stamp(context.valid_before)<=stamp(context.evaluated_at)
    ||stamp(root.source_read_at)<stamp(context.evaluated_at)||!['complete','partial'].includes(String(coverage.status))
    ||context.observation_age_limit_seconds!==undefined&&context.observation_age_limit_seconds!==null
      &&!(Number.isSafeInteger(context.observation_age_limit_seconds)&&Number(context.observation_age_limit_seconds)>=0))return invalid();
  for(const n of Object.values(obj(coverage.reasons)))count(n);
  if(operation===summaryOperation){
    const population=count(r.population),m=obj(r.metrics);
    if(Object.keys(m).length!==metrics.length)return invalid();
    for(const id of metrics){const x=obj(m[id]);if(!['known','no_known_values','mixed_definition_epochs','unknown_definition_epoch'].includes(String(x.state))||(x.definition_epoch!==null&&(typeof x.definition_epoch!=='string'||x.definition_epoch.length>100)))return invalid();
      if((x.state!=='known'&&x.known_count!==0)||(id==='views'&&x.state==='known'&&x.definition_epoch===null))return invalid();
      if(count(x.known_count)+count(x.missing_count)!==population)return invalid();
      for(const key of ['total','mean','median','q1','q3','min','max'])if(x[key]!==null&&(typeof x[key]!=='string'||!/^\d{1,40}(?:\.\d{1,2})?$/.test(x[key] as string)))return invalid();
      if(!Array.isArray(x.buckets)||x.buckets.length>32)return invalid();let total=0;
      for(const b of x.buckets){const bucket=obj(b);if(typeof bucket.key!=='string'||typeof bucket.label!=='string'||bucket.label.length>100)return invalid();total+=count(bucket.count);}
      if(total!==x.known_count)return invalid();
    }
  }else if(operation===calendarOperation){
    if(count(r.known_count)>count(r.population)||count(r.year)<1||Number(r.year)>9999||typeof r.time_zone!=='string')return invalid();
    for(const [key,max] of [['days',366],['hours',24],['weekdays',7]] as const){if(!Array.isArray(r[key])||r[key].length>max)return invalid();let total=0;
      for(const item of r[key]){const p=obj(item);total+=count(p.count);
        if(key==='days'&&(typeof p.date!=='string'||!/^\d{4}-\d\d-\d\d$/.test(p.date)))return invalid();
        if(key==='hours'&&count(p.hour)>23||key==='weekdays'&&(count(p.weekday)<1||Number(p.weekday)>7))return invalid();
      }if(total>Number(r.known_count))return invalid();
    }
  }else{
    if(!['views','subscribers'].includes(String(r.metric))||r.definition_epoch!==null||r.method!=='last_recorded_channel_observation_per_bucket_v1'
      ||count(r.hours)>696||![1,6,24].includes(count(r.bucket_hours))||!Array.isArray(r.points)||r.points.length>697)return invalid();
    count(r.observations);let previous=-Infinity;
    for(const item of r.points){const p=obj(item),at=stamp(p.recorded_at);if(at<=previous||at>stamp(context.evaluated_at)||stamp(p.expires_at)<stamp(context.valid_before)
      ||typeof p.value!=='string'||!/^\d{1,20}$/.test(p.value))return invalid();previous=at;}
  }
  return value as OperationResult;
}
