// Exercise the generated interface's truthfulness with a small DOM fixture.
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(require('node:path').join(__dirname,'live-viewer.html'),'utf8');
const names=['exactLabel','mappingLabel','nativeRecordMatches','updateCatalog','explain','addProperty','addAdjacency','updateDetail','updateWarnings','edgeDrawable','canFollowSelection'];
const source=html.split('\n').filter(line=>names.some(name=>line.startsWith(`function ${name}(`))).join('\n');
const context=vm.createContext({});
vm.runInContext(`
class Node {constructor(tag='',text=''){this.tag=tag;this.children=[];this._text=text;this.dataset={};this.classList={toggle:()=>{}};}append(...nodes){this.children.push(...nodes);}replaceChildren(...nodes){this._text='';this.children=nodes;}setAttribute(){}set textContent(text){this._text=String(text);this.children=[];}get textContent(){return this._text+this.children.map(c=>typeof c==='string'?c:c.textContent).join(' ');}}
const nodes={},$=id=>nodes[id]||(nodes[id]=new Node()),make=(tag,cls,text='')=>new Node(tag,String(text));
const document={querySelectorAll:()=>[]},fmt=n=>String(n??'—'),xyz=p=>JSON.stringify(p??null),typeLabel=t=>t;
const arrayFor=(s,t)=>s[t==='face'?'faces':t==='edge'?'edges':'points'],getEntity=(s,t,i)=>arrayFor(s,t).find(e=>e.index===i);
const localId=(t,e)=>(t==='face'?'f':'e')+(e.originIndex??e.index),shortId=localId,persistentName=(t,e)=>e.name;
const raw={index:0,name:'F2:side:c1#1',exact:false,area:10,point:[0,0,0]};
const face={...raw,nativeRecord:{...raw},nameSource:'model.faces',displaySource:'native-face-tag'};
const rawEdge={index:7,name:'faceA|faceB',start:[0,0,0],mid:[1,0,0],end:[2,0,0],length:2};
const edge={...rawEdge,nativeRecord:{...rawEdge},nameSource:'model.edges',displaySource:'unresolved',displayMapping:'unresolved',displayApproximate:true,points:[],faceIndices:[]};
const body={id:'body0',bodyName:'Flange',faces:[face],edges:[edge],points:[],warnings:['Edge 7: display mapping unresolved']};
const EMPTY={},DATA={stages:[body]},stage=()=>body,hasSnapshot=true,state={kind:'face',search:'',selected:{type:'face',index:0}},select=()=>{};
${source}
globalThis.api={nodes,face,edge,raw,rawEdge,state,body,updateCatalog,updateDetail,updateWarnings,edgeDrawable:typeof edgeDrawable==='function'?edgeDrawable:null,canFollowSelection:typeof canFollowSelection==='function'?canFollowSelection:null};
`,context);
const a=context.api;
a.updateCatalog();
assert(a.nodes['entity-list'].textContent.includes('SolidCraft 原生名称'),'catalog must explicitly identify native API names');
assert(!a.nodes['entity-list'].textContent.includes('几何命名 · 未验证'),'ambiguous old label must disappear');
a.updateDetail();
let text=a.nodes.detail.textContent;
assert(text.includes('历史来源未验证')&&text.includes('model.faces'),'exact:false must describe native historical provenance');
assert(text.includes('SolidCraft 原始查询记录')&&text.includes('"exact": false'),'raw native record must be inspectable');
assert.equal(JSON.stringify(a.face.nativeRecord),JSON.stringify(a.raw),'detail must not change native data');
delete a.face.exact;delete a.face.nativeRecord.exact;
a.updateDetail();text=a.nodes.detail.textContent;
assert(!text.includes('exact: true')&&text.includes('未返回'),'missing exact must never be displayed as true');
a.state.kind='edge';a.state.selected={type:'edge',index:7};a.updateCatalog();a.updateDetail();
assert(a.nodes['entity-list'].textContent.includes('未确定'),'unresolved edge mapping must be visible in catalog');
assert(a.nodes.detail.textContent.includes('不绘制'),'unresolved edge must explain why named geometry is hidden');
a.updateWarnings();
assert(a.nodes['warning-list'].textContent.includes('display mapping unresolved'),'adapter display warnings must reach the UI');
assert(a.edgeDrawable,'renderer needs an explicit named-edge display gate');
assert.equal(a.edgeDrawable(a.edge),false,'unresolved edges must not be drawn or labeled');
assert.equal(a.edgeDrawable({...a.edge,displaySource:'tagged-mesh-boundary',displayApproximate:false,points:[[0,0,0],[2,0,0]]}),true);
assert.equal(a.edgeDrawable({...a.edge,displaySource:'query-points-fallback',points:[[0,0,0],[1,0,0],[2,0,0]]}),false,'old fallback snapshots must not draw named edges');
if(a.canFollowSelection){
  const old={document_id:'docA',revision:1},next={document_id:'docA',revision:2};
  assert(!a.canFollowSelection({type:'face',exact:false},old,next),'unverified face identity must not follow a rebuild');
  assert(!a.canFollowSelection({type:'edge'},old,next),'edge without native exact status must not follow a rebuild');
  assert(!a.canFollowSelection({type:'point'},old,next),'display points must not imply persistent identity');
  assert(a.canFollowSelection({type:'face',exact:true},old,next),'native exact face may match the same verified name');
  assert(a.canFollowSelection({type:'face',exact:false},old,old),'same snapshot refresh can retain selection');
}
console.log('Passed native-name provenance, tri-state exact, raw-record, warning and edge-visibility checks');
