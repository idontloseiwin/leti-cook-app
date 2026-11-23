export const optionalFields = ['video.description','video.paid_promotion'] as const;
export type OptionalField = typeof optionalFields[number];
export type Mode = 'off' | 'current';
export type Modes = Partial<Record<OptionalField,Mode>>;
export type FieldPreferences = {version:1; defaults_profile:'public-current-v1'; lifecycle_profile:'public-696h-v1';
 workspace:Modes; sources:Partial<Record<'public.videos.current.v1',{fields:Modes;types:Partial<Record<'long'|'short'|'stream',Modes>>}>>;
 description_setup:'pending'|'later'|'chosen'};
const fail=():never=>{throw new Error('Unsupported field settings.');};
function object(value:unknown):Record<string,unknown>{if(!value||typeof value!=='object'||Array.isArray(value))return fail();return value as Record<string,unknown>;}
function exact(value:unknown,keys:string[]){const obj=object(value);if(Object.keys(obj).sort().join()!==[...keys].sort().join())return fail();return obj;}
function modes(value:unknown):Modes{const row=object(value);for(const [field,mode] of Object.entries(row))if(!optionalFields.includes(field as OptionalField)||(mode!=='off'&&mode!=='current'))return fail();return {...row} as Modes;}
export function parseFieldPreferences(value:unknown):FieldPreferences{
 const row=exact(value,['version','defaults_profile','lifecycle_profile','workspace','sources','description_setup']);
 if(row.version!==1||row.defaults_profile!=='public-current-v1'||row.lifecycle_profile!=='public-696h-v1'||!['pending','later','chosen'].includes(String(row.description_setup)))return fail();
 const sources=object(row.sources);if(Object.keys(sources).some(key=>key!=='public.videos.current.v1'))return fail();
 const source=sources['public.videos.current.v1'];let parsed:FieldPreferences['sources']={};
 if(source!==undefined){const item=exact(source,['fields','types']);const types=object(item.types);if(Object.keys(types).some(key=>!['long','short','stream'].includes(key)))return fail();
 parsed={'public.videos.current.v1':{fields:modes(item.fields),types:Object.fromEntries(Object.entries(types).map(([kind,value])=>[kind,modes(value)]))}};}
 return {version:1,defaults_profile:'public-current-v1',lifecycle_profile:'public-696h-v1',workspace:modes(row.workspace),sources:parsed,description_setup:row.description_setup as FieldPreferences['description_setup']};
}
export function fieldMode(doc:FieldPreferences,kind:'long'|'short'|'stream',field:OptionalField):Mode{
 const source=doc.sources['public.videos.current.v1'];return source?.types[kind]?.[field]??source?.fields[field]??doc.workspace[field]??'off';
}
