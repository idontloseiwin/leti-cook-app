import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from '../application/frontend/node_modules/typescript/lib/typescript.js';
const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
function compiled(path, imports) {
  const exports = {};
  const { outputText } = ts.transpileModule(read(path), { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  }});
  new Function('require', 'exports', outputText)(name => { assert.ok(name in imports, name); return imports[name]; }, exports);
  return exports;
}
const source = { transferSourceLifetime: (_old, row) => row, captureSourceLifetime: value => value };
const reviews = compiled('../application/frontend/src/videoTypeReviews.ts', {
  './sourceLifetime': source, './serverText.ts': { serverErrorMessage: () => '' },
});
const base = {case_id:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',video_id:'aaaaaaaaaaa',channel_id:'UC'+'a'.repeat(22),
  source_detached_at:null,canonical_type:'long',observed_types:['long','short'],state:'open',revision:1,
  first_seen_at:'2026-10-17T00:00:00Z',last_seen_at:'2026-10-17T00:00:00Z',can_decide:false,
  blocked_reason:'source_unavailable',decision:null,review_status:'awaiting_refresh'};
assert.equal(reviews.parseVideoTypeCase(base).review_status,'awaiting_refresh');
assert.throws(() => reviews.parseVideoTypeCase({...base,review_status:'made_up'}),/unknown review status/);
const waiting = reviews.parseVideoTypeCase(base);
const conflict = reviews.parseVideoTypeCase({...base,observed_types:['long','stream'],review_status:'needs_review',can_decide:true,blocked_reason:null});
const jsx = (type, props) => ({type,props});
const flatten = node => !node ? [] : Array.isArray(node) ? node.flatMap(flatten) : typeof node !== 'object' ? [node] : [node,...flatten(node.props?.children)];
const text = node => flatten(node).filter(value => typeof value === 'string').join(' ');
function render(states, name = 'VideoTypeReviewDisclosure', props = {}) {
  const hooks = { useState: value => [states.length ? states.shift() : value, () => {}],
    useEffect: () => {}, useRef: value => ({current:value}), useCallback: callback => callback };
  const components = compiled('../application/frontend/src/components/VideoTypeReviewPanel.tsx', {
    react:hooks, 'react/jsx-runtime':{jsx,jsxs:jsx,Fragment:'fragment'}, '../videoTypeReviews':reviews,
    './HiddenVideoIdentities':{HiddenVideoIdentities:() => null}, '../sourceLifetime':{sourceExpired:row => row.expired === true},
    '../sourceLifetimeReact':{useSourceExpiry:() => {}},
  });
  return {tree:components[name](props),components};
}
let {tree,components} = render([false,{cases:[],next_cursor:null},false,0]);
assert.doesNotMatch(text(tree),/need attention|manual decision|No open/);
assert.equal(flatten(tree).some(node => node.type === components.VideoTypeReviewPanel),false,'An empty form does not mount by default.');
({tree} = render([false,{cases:[conflict],next_cursor:null},false,0]));
assert.match(text(tree),/Type checks need attention/);
({tree} = render([false,{cases:[{...conflict,expired:true}],next_cursor:null},false,0]));
assert.doesNotMatch(text(tree),/need attention/,'Expired evidence does not keep the alarm alive.');
({tree,components} = render([true,undefined,false,0]));
assert.ok(flatten(tree).some(node => node.type === components.VideoTypeReviewPanel),'Checks/history can still be opened.');
({tree,components} = render([false,undefined,false,0,true]));
const retained = flatten(tree).find(node => node.props?.id === 'video-type-checks-content');
assert.equal(retained.props.hidden,true,'Collapse hides an opened panel.');
assert.ok(flatten(retained).some(node => node.type === components.VideoTypeReviewPanel),'Collapse preserves the mounted decision owner and pending request ID.');
({tree,components} = render([true,undefined,false,0,true]));
assert.equal(flatten(tree).find(node => node.props?.id === 'video-type-checks-content').props.hidden,false,'Reopening shows that same panel slot.');
const panelStates = row => ['awaiting',[undefined],{page:{cases:[row],next_cursor:null},reviewChanged:false},false,undefined,0,undefined,0];
({tree} = render(panelStates(waiting),'VideoTypeReviewPanel'));
assert.match(text(tree),/Awaiting automatic recheck/);
const review = flatten(tree).find(node => node.type === 'button' && node.props.children === 'Review');
assert.equal(review.props.disabled,true,'Automatic recheck is not a manual-decision action.');
({tree} = render(panelStates(conflict),'VideoTypeReviewPanel'));
assert.equal(flatten(tree).find(node => node.type === 'button' && node.props.children === 'Review').props.disabled,false);
const expiredCase = {...conflict,expired:true};
const detailStates = pending => [{case:expiredCase,history:[],next_before_revision:null},'edit',false,false,undefined,'long','Reason',pending,false,undefined,false,undefined,0];
({tree} = render(detailStates(undefined),'VideoTypeReviewDetails',{initialCase:expiredCase,initialMode:'edit',onCase:()=>{},onClose:()=>{}}));
assert.equal(flatten(tree).find(node => node.type === 'select').props.disabled,true,'An open form loses eligibility when its evidence expires.');
assert.equal(flatten(tree).find(node => node.type === 'button' && node.props.type === 'submit').props.disabled,true);
({tree} = render(detailStates({request_id:base.case_id,expected_revision:1,video_type:'long',reason:'Reason'}),'VideoTypeReviewDetails',{initialCase:expiredCase,initialMode:'edit',onCase:()=>{},onClose:()=>{}}));
assert.equal(flatten(tree).find(node => node.type === 'button' && node.props.type === 'submit').props.disabled,false,'The same pending command may still reconcile an existing receipt after expiry.');
console.log('Type review presentation: quiet default, current exception indicator, automatic waiting, history access and edit eligibility passed.');
