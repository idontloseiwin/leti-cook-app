import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';
import * as lifetime from '../application/frontend/src/sourceLifetime.ts';
const text = readFileSync(new URL('../application/frontend/src/explorer/researchQuery.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(text, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } });
const exports = {};
new Function('require', 'exports', outputText)(name => name === '../sourceLifetime' ? lifetime : { ExplorerApiError: class extends Error {} }, exports);
const filters = { scope: {kind:'folder',id:'11111111-1111-1111-1111-111111111111'}, type:'short',
  columns:exports.researchFields.filter(field => field!=="channel.id" && !field.startsWith("observation.")), where:{ all:[{field:'video.title',op:'contains_casefold_v1',value:'Straße'},
  {field:'video.views',op:'gte',value:'9007199254740993'}]}, order:[{field:'video.views',direction:'desc'},{field:'video.published_at',direction:'asc'}], complete:false };
const request = exports.researchQuery(filters);
assert.deepEqual(request.video_types,['short']);
assert.equal(request.where.all[1].value,'9007199254740993');
assert.equal(request.scope.include_descendants,false);
assert.deepEqual(exports.researchQuery(filters,'opaque').page,{size:25,after:'opaque'});
for(const n of ['-1','1.5','01','18446744073709551616',42]) assert.throws(()=>exports.uint(n));
assert.equal(exports.uint('18446744073709551615'),'18446744073709551615');
for(const scope of [{kind:'channel',id:'UC'+'a'.repeat(22)},{kind:'folder',id:filters.scope.id},
  {kind:'label',id:'monetized'},{kind:'label',id:'custom:11111111-1111-4111-8111-111111111111'},
  {kind:'collection',id:'11111111-1111-4111-8111-111111111111'}]) assert.equal(exports.researchQuery({...filters,scope}).scope.kind,scope.kind);
for(const scope of [{kind:'channel',id:'bad'},{kind:'collection',id:filters.scope.id},{kind:'label',id:'bad'},{kind:'other',id:filters.scope.id}])
  assert.throws(()=>exports.researchQuery({...filters,scope}));
assert.throws(()=>exports.researchQuery({...filters,order:[...filters.order,filters.order[0]]}));
const now=Date.now(),evaluated=new Date(now).toISOString(),deadline=new Date(now+120000).toISOString();
const values = {
 'video.description':'Synthetic description','video.paid_promotion':false,
 'video.id':'abcdefghijk','video.channel_id':'UC'+'a'.repeat(22),'video.type':'short',
 'video.title':'Do not execute this <script> title','video.published_at':evaluated,'video.duration_seconds':'0',
 'video.views':'9007199254740993','video.likes':'0','video.comments':'18446744073709551615',
 'video.tags':[],'video.topic_urls':['https://en.wikipedia.org/wiki/Film'],'video.category_id':'22',
 'video.audio_language':'en','video.metadata_language':'','video.caption_available':false,
 'video.scheduled_start_at':evaluated,'video.actual_end_at':evaluated,
 'video.thumbnail_url':'https://img.youtube.com/vi/abcdefghijk/hqdefault.jpg','channel.title':'Straße',
 'channel.avatar_url':'https://yt3.ggpht.com/example','channel.subscribers':'42','channel.views':'53',
 'channel.video_count':'2','channel.country_code':'UA','channel.created_at':evaluated,
 // Calculated and local-date fields: Library hands them to the workshop.
 'video.title_length':'34','video.tag_count':'0','video.like_rate':'4.299504','video.comment_rate':'0',
 'video.published_date':evaluated.slice(0,10),'video.published_year':evaluated.slice(0,4),'video.lifetime_views_per_day':'1234.5'
};
const cells=Object.fromEntries(request.select.map(field=>[field,{state:'known',value:values[field],provenance_ref:'youtube',observed_at:evaluated,expires_at:deadline}]));
const valid={query_version:1,query_fingerprint:'a'.repeat(64),columns:request.select,
 context:{source_id:'public.videos.current.v1',consistency:'current',evaluated_at:evaluated,valid_before:deadline},
 source_read_at:evaluated,source_expires_at:deadline,rows:[{id:values['video.id'],cells}],
 provenance:{youtube:{origin:'youtube_reported',source_paths:['videos.statistics.viewCount'],method:'reported_current_v1'}},
 coverage:{status:'complete',basis:'channel_inventory',channels_complete:1,channels_in_scope:1,reasons:{}},
 page:{has_more:false,next_cursor:null}};
function tagged(change=()=>{}){const data=structuredClone(valid);change(data);return lifetime.captureSourceLifetime(data,undefined,performance.now());}
const parsed=exports.parseResearchPage(tagged(),filters);
assert.equal(parsed.rows[0].cells['video.views'].value,'9007199254740993');
assert.equal(parsed.rows[0].cells['video.caption_available'].value,false);
assert.deepEqual(Array.from(parsed.rows[0].cells['video.tags'].value),[]);
assert.ok(lifetime.sourceLifetime(parsed));
for(const change of [
 data=>{data.rows[0].cells['video.views'].value=9007199254740993;},
 data=>{data.rows[0].cells['video.title_length'].value='3.5';},
 data=>{data.rows[0].cells['video.like_rate'].value='-1';},
 data=>{data.rows[0].cells['video.like_rate'].value=4.3;},
 data=>{data.rows[0].cells['video.published_date'].value=evaluated;},
 data=>{data.rows[0].cells['video.published_year'].value='MMXXVI';},
 data=>{data.rows[0].cells['video.views'].state='unknown';},
 data=>{data.rows[0].cells['video.type'].value='long';},
 data=>{data.rows.push(data.rows[0]);},
 data=>{data.page.has_more=true;},
 data=>{data.rows[0].id='unsafe/id';},
 data=>{data.rows[0].cells['video.views'].expires_at=evaluated;},
 data=>{data.context.valid_before=evaluated;},
 data=>{data.coverage.channels_complete=2;},
 data=>{data.source_read_at='invalid';},
 data=>{data.rows[0].cells['video.caption_available'].value='false';},
 data=>{data.rows[0].cells['video.tags'].value=[null];},
 data=>{data.rows[0].cells['video.tags'].value=Array(501).fill('x');},
 data=>{data.rows[0].cells['video.thumbnail_url'].value='https://example.com/track';},
 data=>{data.coverage.basis='collection_members';},
 data=>{data.coverage.reasons={missing:1};},
 data=>{data.provenance.youtube.method='automatic_long_short_v1';},
]) assert.throws(()=>exports.parseResearchPage(tagged(change),filters));
assert.throws(()=>exports.parseResearchPage(structuredClone(valid),filters));
const unknown=tagged(data=>{data.rows[0].cells['video.views'].state='unknown';data.rows[0].cells['video.views'].value=null;});
assert.equal(exports.parseResearchPage(unknown,filters).rows[0].cells['video.views'].value,null);
const rule=tagged(data=>{data.provenance.youtube.method='automatic_long_short_v1';data.provenance.youtube.type_rule={id:'long_short_overlap_duration',version:1,max_short_seconds:180};});
assert.equal(exports.parseResearchPage(rule,filters).provenance.youtube.type_rule.max_short_seconds,180);
const collection=tagged(data=>{data.coverage.basis='collection_members';data.coverage.channels_complete=0;});
assert.equal(exports.parseResearchPage(collection,{...filters,scope:{kind:'collection',id:'11111111-1111-4111-8111-111111111111'}}).coverage.status,'complete');
for(const input of [null,'{bad',JSON.stringify({version:2,columns:['video.views'],view:'table'}),
 JSON.stringify({version:1,columns:['private.password'],view:'table'})]) assert.deepEqual(exports.parsePresentation(input).columns,exports.defaultColumns);
assert.deepEqual(exports.parsePresentation(JSON.stringify({version:1,view:'gallery',columns:['video.title'],rows:['do not persist']})),
 {version:1,view:'gallery',columns:['video.title']});
assert.equal(exports.safeImage('https://yt3.ggpht.com.example.com/tracker'),false);
assert.equal(exports.safeImage('https://user@yt3.ggpht.com/a'),false);
assert.equal(exports.safeImage('javascript:alert(1)'),false);
assert.equal(exports.safeImage('https://yt3.ggpht.com/a'),true);
console.log('Catalogue UI contract: all 25 fields, scopes, precision, clocks, arrays, false/empty, coverage, provenance, image allowlist, presentation-only storage passed.');

// Library addresses and the shell: tests/test_library_navigation.mjs.
const alternativeImage = tagged();
alternativeImage.rows[0].cells['channel.avatar_url'].value='https://example.test/image.png';
assert.equal(exports.parseResearchPage(alternativeImage,filters).rows.length,1);
assert.equal(exports.safeImage('https://example.test/image.png'),false);
console.log('Image-reference isolation passes.');
