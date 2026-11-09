"""Adapt the existing verified topology canvas into an archive-only frame.

This build helper reads topology-live but writes only this directory. The shipped
geometry.html is standalone; normal users do not need the source viewer.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent / "topology-live" / "live-viewer.html"
page = SOURCE.read_text(encoding="utf-8")
page = page.replace("实时拓扑命名 · 查看器", "历史拓扑与草图")
page = page.replace("SOLIDCRAFT / LIVE TOPOLOGY", "SAVED GEOMETRY / READ ONLY")
page = page.replace("当前实体", "此状态的实体")
page = page.replace("实时三维模型", "历史三维模型")
page = page.replace("实时观察器 · 所有更新均为已提交模型的快照", "档案几何 · 不查询或修改当前 CAD")
page = page.replace("当前文档没有实体。完成拉伸等造型后会自动显示。", "此状态没有实体；可用草图线框见画布。")
page = page.replace("等待第一次模型快照。", "等待父页面选取档案。")
page = page.replace("连接后显示特征与显示映射警告。", "选取档案后显示特征与显示映射警告。")
page = page.replace("在 SolidCraft 中完成造型后会自动更新。", "选择一条观察或离线阶段，查看该时刻保存的几何。")
page = page.replace("自动读取本机 SolidCraft 已求值的实体。页面只观察几何与名称，不向 SolidCraft 提交建模命令。", "此页面仅显示父页面传入的历史资源，不建立 CAD 连接。")
page = page.replace("</style></head>", """
.status-actions{display:none}.shell{padding:10px;gap:10px;grid-template-columns:205px minmax(280px,1fr) 245px}header{padding:12px 16px}header h1{font-size:17px}.statusbar{padding:9px 15px}.catalog-list{height:330px}canvas{height:420px}.detail{max-height:480px;overflow:auto}.right-sidebar .notice{font-size:10px}.body-list{max-height:160px}.main{gap:10px}footer{padding:10px;font-size:10px}.header-meta{font-size:10px}.legend{font-size:10px;gap:9px}@media(max-width:1100px){.shell{grid-template-columns:180px minmax(220px,1fr)}.right-sidebar{grid-column:1/-1;display:grid;grid-template-columns:1fr 1fr}.detail{max-height:240px}}@media(max-width:650px){.shell{display:flex;flex-direction:column}.left-sidebar,.main,.right-sidebar{width:100%}.left-sidebar{display:grid;grid-template-columns:1fr 1fr;gap:8px;order:2}.catalog-list{height:140px}.main{order:1}.right-sidebar{display:block;order:3}.left-sidebar .catalog-panel{display:block}canvas{height:370px}.header-meta{max-width:40%}}
</style></head>""")

script_match = re.search(r"<script>(.*?)</script>", page, re.S)
if not script_match:
    raise RuntimeError("The source renderer has no script")
script = script_match.group(1)
# The source is one function per line. Do not retain a dormant live network path.
removed = ("async function sessionFetch(", "function schedulePoll(", "async function refresh(", "function setPaused(", "function init(", "window.__liveTopology=")
script = "\n".join(line for line in script.splitlines() if not line.startswith(removed))
script = script.replace("function draw(scene)", "function drawBody(scene)")
script = re.sub(r"^function updateStatus\(.*$", """function updateStatus(){
$('status-title').textContent=archiveLabel||'等待选择历史记录';$('status-dot').className='status-dot '+(hasSnapshot?'ready':'');
$('status-extra').textContent=sketchMessage||'保存的几何资源；不连接当前 CAD。';
$('meta-version').textContent=DATA.meta?.version?'SolidCraft '+DATA.meta.version:'历史资源';
$('meta-design').textContent=DATA.meta?.design||archiveLabel||'尚未选择';$('footer-meta').textContent=archiveLabel;
}""", script, flags=re.M)
script = re.sub(r"^function updateHeader\(.*$", """function updateHeader(){
const s=stage(),all=state.stage===ALL_ID;$('stage-title').textContent=DATA.stages.length?(all?'全部可见实体':s.bodyName||s.title):'草图与空状态';
$('stage-subtitle').textContent=DATA.stages.length?'保存于选定历史记录的几何，可点击面、边与展示点。':sketchMessage||'此记录无实体或可确认的草图线框。';
$('stage-stats').replaceChildren();if(s!==EMPTY){for(const[label,value]of[['面',s.faces.length],['边',s.edges.length],['展示点',s.points.length]]){const n=make('span',null,label);n.append(make('strong',null,String(value)));$('stage-stats').append(n);}}
$('xray-button').setAttribute('aria-pressed',state.xray);$('selection-note').textContent=state.selectionNote;$('selection-note').classList.toggle('visible',!!state.selectionNote);$('png-button').disabled=!DATA.stages.length&&!sketchLines.length;
}""", script, flags=re.M)

addon = r"""
let archiveLabel='',sketchLines=[],sketchMessage='',sketchBounds=null;
function finitePoint(p){return Array.isArray(p)&&p.length===3&&p.every(Number.isFinite);}
function parseSketches(raw){
const list=Array.isArray(raw)?raw:Array.isArray(raw?.sketches)?raw.sketches:[];const lines=[];let unsupported=0,total=0;
for(const item of list){const sk=item?.result||item?.data||item;if(!sk||typeof sk!=='object')continue;const pts=new Map((Array.isArray(sk.points)?sk.points:[]).filter(p=>finitePoint(p.world)).map(p=>[String(p.id),p.world]));
for(const curve of Array.isArray(sk.curves)?sk.curves:[]){total++;const a=pts.get(String(curve.start)),b=pts.get(String(curve.end));if(curve.type==='line'&&finitePoint(a)&&finitePoint(b)){lines.push({a,b,construction:curve.construction===true});}else unsupported++;if(lines.length>=50000)return{lines,unsupported:unsupported+1,total,capped:true};}}
return{lines,unsupported,total,capped:false};}
function drawSketch(scene){
if(DATA.stages.length)return;const canvas=scene.canvas,dpr=Math.min(window.devicePixelRatio||1,2),rect=canvas.getBoundingClientRect(),w=rect.width,h=rect.height;
if(canvas.width!==Math.round(w*dpr)||canvas.height!==Math.round(h*dpr)){canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);}scene.width=w;scene.height=h;const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);ctx.fillStyle='#0d1926';ctx.fillRect(0,0,w,h);
if(!sketchLines.length){ctx.fillStyle='#a5b9ca';ctx.textAlign='center';ctx.font='13px Segoe UI,Microsoft YaHei,sans-serif';ctx.fillText('此状态没有可绘制的实体或草图线段',w/2,h/2-5);ctx.font='11px Segoe UI,Microsoft YaHei,sans-serif';ctx.fillText('完整草图数据可在父页面“原始数据”中查看',w/2,h/2+21);return;}
const g=sceneGeometry(scene,{bounds:sketchBounds});ctx.lineWidth=1.6;for(const l of sketchLines){const a=g.project(l.a),b=g.project(l.b);ctx.strokeStyle=l.construction?'#88959f':'#91d2ed';ctx.setLineDash(l.construction?[5,4]:[]);ctx.beginPath();ctx.moveTo(a[0],a[1]);ctx.lineTo(b[0],b[1]);ctx.stroke();}ctx.setLineDash([]);ctx.fillStyle='#a2bacd';ctx.font='11px Segoe UI,Microsoft YaHei,sans-serif';ctx.fillText('原生 world 点坐标 · 线段草图 · 拖动旋转',14,h-16);
}
function draw(scene){if(DATA.stages.length)drawBody(scene);else drawSketch(scene);}
function receiveGeometry(payload){
if(!payload||payload.type!=='trace-geometry')return false;const geometry=payload.data&&typeof payload.data==='object'?payload.data:{meta:{},stages:[]};
const parsed=parseSketches(payload.sketches);sketchLines=parsed.lines;archiveLabel=String(payload.label||'历史资源');sketchBounds=null;if(sketchLines.length){const points=sketchLines.flatMap(l=>[l.a,l.b]);sketchBounds={min:[0,1,2].map(k=>Math.min(...points.slice(0,5000).map(p=>p[k]))),max:[0,1,2].map(k=>Math.max(...points.slice(0,5000).map(p=>p[k])))};for(const p of points)for(let k=0;k<3;k++){sketchBounds.min[k]=Math.min(sketchBounds.min[k],p[k]);sketchBounds.max[k]=Math.max(sketchBounds.max[k],p[k]);}}
sketchMessage=geometry.stages?.length?'历史实体拓扑；原始名称与 exact 状态按快照保存。':sketchLines.length?'已绘制 '+sketchLines.length+' 条原生草图线段。':'没有取得可确认 world 坐标的草图线段。';
if(parsed.unsupported)sketchMessage+=' '+parsed.unsupported+' 条曲线未绘制；圆、弧与样条请查看原始 JSON。';if(parsed.capped)sketchMessage+=' 草图显示已达到线段预算。';
state.selected=null;state.viewMode='all';state.zoom=1;state.pan=[0,0];applyState({status:'ready',data:geometry,generation:state.generation+1,source:{document_id:archiveLabel,revision:0,name:archiveLabel}});updateAll();return true;
}
function init(){
document.querySelectorAll('[data-kind]').forEach(b=>b.onclick=()=>{state.kind=b.dataset.kind;updateCatalog();});$('search').oninput=e=>{state.search=e.target.value;updateCatalog();};$('xray-button').onclick=()=>{state.xray=!state.xray;updateHeader();queueRender();};$('fit-button').onclick=fit;document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>setView(b.dataset.view));for(const[k,id]of[['face','label-faces'],['edge','label-edges'],['point','label-points']])$(id).onchange=e=>{state.labels[k]=e.target.checked;queueRender();};$('label-format').onchange=e=>{state.labelFormat=e.target.value;queueRender();};$('label-hidden').onchange=e=>{state.hiddenLabels=e.target.checked;queueRender();};$('label-overlap').onchange=e=>{state.overlap=e.target.checked;queueRender();};$('png-button').onclick=savePng;setupScene();new ResizeObserver(queueRender).observe($('canvas-grid'));window.addEventListener('resize',queueRender);
window.addEventListener('message',event=>{if(event.origin!==location.origin||event.source!==parent||event.data?.type!=='trace-geometry')return;try{receiveGeometry(event.data);}catch(error){archiveLabel=String(event.data.label||'历史资源');sketchMessage='该几何资源未通过校验：'+String(error.message||error);DATA={meta:{},stages:[]};stageById.clear();allStage=null;sketchLines=[];updateAll();}});
updateAll();if(parent!==window)parent.postMessage({type:'trace-geometry-ready'},location.origin);
}
window.__traceGeometry={receiveGeometry,parseSketches,finitePoint,get data(){return DATA;},get state(){return state;}};
"""
script = script.replace("\ninit();", "\n" + addon + "\nif(!window.__TRACE_TEST__)init();")
if "trace-geometry-ready" not in script or "function drawBody(scene)" not in script:
    raise RuntimeError("The source renderer shape changed; inspect before rebuilding")
page = page[:script_match.start(1)] + script + page[script_match.end(1):]
(ROOT / "geometry.html").write_text(page, encoding="utf-8", newline="\n")
print("Wrote archive-only geometry.html")
