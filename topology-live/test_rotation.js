// Exercise the generated viewer's real input handlers and projection, without a browser.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, 'live-viewer.html'), 'utf8');
const functionSource = ['worldToCamera', 'cameraVector', 'sceneGeometry', 'setupScene'].map(name => {
  const line = html.split('\n').find(item => item.startsWith(`function ${name}(`));
  assert(line, `generated viewer contains ${name}`);
  return line;
}).join('\n');
const context = vm.createContext({});
vm.runInContext(`
const state={yaw:.72,pitch:.60,zoom:1,pan:[0,0]};
const handlers={};
const canvas={addEventListener:(name,handler)=>handlers[name]=handler,setPointerCapture:()=>{},getBoundingClientRect:()=>({left:0,top:0,width:800,height:600})};
const $=()=>canvas,scenes=[];
const vec={sub:(a,b)=>a.map((v,i)=>v-b[i]),len:a=>Math.hypot(...a)};
const hideTooltip=()=>{},queueRender=()=>{};
${functionSource}
setupScene();
globalThis.api={state,handlers,sceneGeometry};
`, context);
const {state, handlers, sceneGeometry} = context.api;
const scene = {width:800,height:600};
const body = {bounds:{min:[-10,-10,-10],max:[10,10,10]}};
function frontSurfacePoint() {
  return [Math.sin(state.yaw)*Math.cos(state.pitch)*8,
    Math.cos(state.yaw)*Math.cos(state.pitch)*8, Math.sin(state.pitch)*8];
}
function reset(yaw,pitch) { Object.assign(state,{yaw,pitch,zoom:1,pan:[0,0]}); }
function drag(dx,dy,shiftKey=false) {
  handlers.pointerdown({clientX:400,clientY:300,pointerId:1,button:0,shiftKey});
  handlers.pointermove({clientX:400+dx,clientY:300+dy});
  handlers.pointerup({clientX:400+dx,clientY:300+dy});
}
let checks = 0;
for(const [view,yaw,pitch] of [['iso',.72,.60],['front',Math.PI,0],['back',0,0]]) {
  for(const delta of [-20,20]) {
    reset(yaw,pitch);
    const point=frontSurfacePoint();
    const before=sceneGeometry(scene,body).project(point);
    drag(delta,0);
    const after=sceneGeometry(scene,body).project(point);
    assert((after[0]-before[0])*delta>0,`${view}: front surface must follow horizontal drag ${delta}`);
    checks++;
  }
  reset(yaw,pitch);
  const point=frontSurfacePoint(),before=sceneGeometry(scene,body).project(point);
  drag(0,20);
  const after=sceneGeometry(scene,body).project(point);
  assert(after[1]>before[1],`${view}: front surface must follow downward drag`);
  checks++;
  reset(yaw,pitch);
  const keyboardPoint=frontSurfacePoint(),keyBefore=sceneGeometry(scene,body).project(keyboardPoint);
  handlers.keydown({key:'ArrowRight',preventDefault:()=>{}});
  assert(sceneGeometry(scene,body).project(keyboardPoint)[0]>keyBefore[0],`${view}: ArrowRight follows drag direction`);
  checks++;
}
reset(.72,.60);
const point=frontSurfacePoint(),before=sceneGeometry(scene,body).project(point);
drag(20,10,true);
const after=sceneGeometry(scene,body).project(point);
assert(Math.abs(after[0]-before[0]-20)<1e-9 && Math.abs(after[1]-before[1]-10)<1e-9,'Shift drag preserves pixel-following pan');
assert.equal(state.yaw,.72); assert.equal(state.pitch,.60); checks++;
console.log(`Passed ${checks} projected rotation/pan checks`);
