'use strict';
// No browser package required: syntax, saved-data boundaries and async selection.
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function script(file){const source=fs.readFileSync(path.join(__dirname,file),'utf8');const match=source.match(/<script>([\s\S]*?)<\/script>/);assert.ok(match,file+' has a script');return match[1];}
class Node {
  constructor(tag='div'){this.tag=tag;this.children=[];this.textContent='';this.value='';this.hidden=false;this.style={};this.dataset={};this.attributes={};this.classList={add(){},remove(){},toggle(){}};this.contentWindow={postMessage(){}};}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=[...items];}
  setAttribute(k,v){this.attributes[k]=v;}
  removeAttribute(k){delete this.attributes[k];}
  get firstChild(){return this.children[0];}
  addEventListener(){}
}
function context(file,fetcher=async()=>{throw Error('Unexpected request');}){
  const elements=new Map(),document={getElementById(id){if(!elements.has(id))elements.set(id,new Node());return elements.get(id);},createElement(tag){return new Node(tag);},querySelectorAll(){return[];}};
  const window={__TRACE_TEST__:true,addEventListener(){},devicePixelRatio:1};
  const sandbox={window,document,fetch:fetcher,AbortController,location:{origin:'http://127.0.0.1:54321'},parent:{postMessage(){}},navigator:{},console,setTimeout,clearTimeout,requestAnimationFrame(){},ResizeObserver:class{observe(){}}};
  vm.createContext(sandbox);new vm.Script(script(file),{filename:file}).runInContext(sandbox);return{api:window.__traceViewer||window.__traceGeometry,elements,sandbox};
}

for(const file of ['viewer.html','geometry.html'])test(file+' JavaScript parses',()=>{new vm.Script(script(file),{filename:file});});

test('resource routes accept only immutable SHA256 names',()=>{
  const{api}=context('viewer.html');assert.equal(api.assetURL({sha256:'a'.repeat(64)}),'/api/assets/'+'a'.repeat(64));
  for(const bad of ['../document.json','http://example.invalid','A'.repeat(64),'a'.repeat(65),'<script>'])assert.throws(()=>api.assetURL({sha256:bad}));
});

test('missing native depths and null end-marker remain distinguishable',()=>{
  const{api}=context('viewer.html');assert.equal(api.depth(0),'0');assert.equal(api.depth(undefined),'不可获取');assert.equal(api.depth('3'),'不可获取');
  assert.equal(api.markerOf({summary:{marker:null,timeline:[{id:3},{id:8}]}}),2);
  assert.equal(api.markerOf({summary:{marker:0,timeline:[{id:3}]}}),0);
  assert.equal(api.markerOf({summary:{timeline:[{id:3}]}}),null);
});

test('a delayed observation cannot overwrite a newer selection',async()=>{
  let resolveFirst;
  const first=new Promise(resolve=>resolveFirst=resolve);
  const records={1:{id:1,label:'O0001',summary:{name:'old',undo:1,redo:0,marker:null,timeline:[]},assets:{}},2:{id:2,label:'O0002',summary:{name:'new',undo:2,redo:0,marker:null,timeline:[]},assets:{}}};
  const{api,elements}=context('viewer.html',async url=>({ok:true,json:async()=>url.endsWith('/1')?await first:records[2]}));
  api.mergeEvents([{id:1,snapshot_id:1},{id:2,snapshot_id:2}]);
  const old=api.selectEvent(1),newer=api.selectEvent(2);await newer;resolveFirst(records[1]);await old;
  assert.equal(api.state.record.id,2);assert.match(elements.get('detail-title').textContent,/O0002/);
});

test('raw strings are rendered as text rather than executable HTML',async()=>{
  const attack='<img src=x onerror=alert(1)>';
  const{api,elements}=context('viewer.html');api.mergeEvents([{id:1,status:'gap',message:attack}]);await api.selectEvent(1);
  assert.equal(elements.get('detail-notice').textContent,attack);assert.equal(elements.get('detail-notice').innerHTML,undefined);
});

test('visible event cache stays bounded while retaining the selected observation',()=>{
  const{api}=context('viewer.html');api.state.selectedEvent=1;api.mergeEvents(Array.from({length:5010},(_,i)=>({id:i+1})));
  assert.equal(api.state.events.size,5000);assert.ok(api.state.events.has(1));assert.equal(api.state.cursor,5010);
});

test('archive geometry frame has no live network or polling code',()=>{
  const js=script('geometry.html');assert.doesNotMatch(js,/\bfetch\s*\(|\bsetInterval\s*\(|function schedulePoll\b|\/api\/refresh/);
  assert.match(js,/event\.origin!==location\.origin\|\|event\.source!==parent/);
});

test('sketch linework uses only native world points and explicit line connections',()=>{
  const{api}=context('geometry.html');const parsed=api.parseSketches({sketches:[{points:[{id:'a',world:[0,0,0]},{id:'b',world:[10,0,0]},{id:'bad',world:[Infinity,0,0]}],curves:[{type:'line',start:'a',end:'b'},{type:'line',start:'a',end:'bad'},{type:'circle',center_at:[0,0],radius:4}]}]});
  assert.equal(parsed.lines.length,1);assert.equal(parsed.unsupported,2);assert.deepEqual(Array.from(parsed.lines[0].b),[10,0,0]);
});

test('failed jobs expose immutable partial downloads without offering a completed stage',()=>{
  const{api,elements}=context('viewer.html');api.state.observation={id:2};
  api.state.jobs=[{id:'failed_job',snapshot_id:2,position:1,status:'failed',stage_id:'not_a_completed_stage',
    error:'<script>not executable</script>',partial_assets:{'inspect.json':{sha256:'b'.repeat(64),size:20},'invalid':{sha256:'../local.file',size:3}}}];
  api.renderJobs();const visit=n=>[n,...n.children.flatMap(visit)];const nodes=visit(elements.get('job-list'));
  const links=nodes.filter(n=>n.tag==='a');assert.equal(links.length,1);assert.equal(links[0].href,'/api/assets/'+'b'.repeat(64));
  assert.ok(nodes.some(n=>n.textContent.includes('任务未完成')));assert.ok(nodes.some(n=>n.tag==='pre'&&n.textContent.includes('failed_job')));
  assert.equal(nodes.filter(n=>n.tag==='button'&&n.textContent==='查看阶段').length,0);
});
