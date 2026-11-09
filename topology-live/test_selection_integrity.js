// Run the generated applyState implementation against accepted/rejected snapshots.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, 'live-viewer.html'), 'utf8');
const names = ['nativeRecordMatches', 'canFollowSelection', 'selectionIdentity', 'validDataset', 'applyState'];
const functions = names.map(name => {
  const line = html.split('\n').find(value => value.startsWith(`function ${name}(`));
  assert(line, `generated viewer contains ${name}`);
  return line;
}).join('\n');

function fixture(exact) {
  const context = vm.createContext({});
  vm.runInContext(`
const raw={index:4,name:'F2:side:c1#1',exact:${JSON.stringify(exact)}};
const body={id:'body0',bodyName:'Flange',faces:[{...raw,nativeRecord:{...raw},nameSource:'model.faces',displaySource:'native-face-tag'}],edges:[],points:[],mesh:{vertices:[],triangles:[]},bounds:{min:[0,0,0],max:[1,1,1]},stats:{}};
let DATA={meta:{},stages:[body]},allStage=null,allAllowed=true,lastContact=null,hasSnapshot=true;
const ALL_ID='__all__',EMPTY=body,stageById=new Map([['body0',body]]),scenes=[];
const acceptedSource={document_id:'docA',revision:1};
const state={stage:'body0',viewMode:'single',snapshotDocumentId:'docA',snapshotSource:{...acceptedSource},source:{...acceptedSource},selected:{type:'face',index:4,stage:'body0'},selectionNote:''};
const stage=()=>stageById.get(state.stage)||body;
const arrayFor=(s,t)=>s[t==='face'?'faces':t==='edge'?'edges':'points'];
const getEntity=(s,t,i)=>arrayFor(s,t).find(e=>e.index===i);
const mergeBodies=bodies=>bodies[0]||body,updateAll=()=>{},updateStatus=()=>{};
${functions}
function payload(revision,exactValue=${JSON.stringify(exact)}){
  const data=JSON.parse(JSON.stringify({meta:{},stages:[body]}));
  data.stages[0].faces[0].exact=exactValue;
  data.stages[0].faces[0].nativeRecord.exact=exactValue;
  return {status:'ready',generation:revision,source:{document_id:'docA',revision},data};
}
globalThis.api={state,payload,applyState};
`, context);
  return context.api;
}

let checks = 0;
{
  const api = fixture(false);
  const invalid = api.payload(2);
  delete invalid.data.stages[0].faces[0].nativeRecord;
  assert.throws(() => api.applyState(invalid), /原生|记录|快照/, 'inconsistent native records must be rejected');
  assert.equal(api.state.snapshotSource.revision, 1, 'a rejected payload must not advance accepted-snapshot identity');
  api.applyState(api.payload(2));
  assert.equal(api.state.selected, null, 'an unverified rev1 selection must clear when valid rev2 follows rejected rev2');
  checks++;
}
{
  const api = fixture(false);
  api.applyState(api.payload(1));
  assert(api.state.selected && api.state.selected.index === 4, 'a valid refresh of the same accepted revision may retain selection');
  checks++;
}
{
  const api = fixture(true);
  api.applyState(api.payload(2, true));
  assert(api.state.selected && api.state.selected.index === 4, 'the same native exact name may follow a rebuild when both snapshots are exact');
  checks++;
}
{
  const api = fixture(true);
  api.applyState(api.payload(2, false));
  assert.equal(api.state.selected, null, 'a formerly exact selection cannot follow a name whose new history is unverified');
  checks++;
}
console.log(`Passed ${checks} accepted-snapshot selection integrity checks`);
