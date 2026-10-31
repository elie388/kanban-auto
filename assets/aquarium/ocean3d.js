/* Ocean3D: 100 locally bundled, individually generated GLB meshes.
   Models are procedural prototypes, NOT professionally sculpted high-poly assets. */
const GLTF_URL='three/addons/loaders/GLTFLoader.js';
const THREE_URL='three';
const NAMES=`公子小丑鱼,霓虹灯鱼,蓝色孔雀鱼,兰寿金鱼,蓝魔鬼鱼,黄尾蓝魔鬼鱼,蝶鱼,蓝曼龙鱼,礼服孔雀鱼,箱鲀,蓝唐王鱼,雪花小丑鱼,黑冰小丑鱼,珍珠马甲鱼,海星,皇帝神仙鱼,火焰神仙鱼,铜带蝴蝶鱼,月光蝶鱼,蓝鲨,绿海龟,蓝色龙睛金鱼,蝶尾金鱼,黄箱鲀,花斑尖嘴鱼,宽吻海豚,紫火鱼,火焰雷达鱼,法国神仙鱼,皇冠斗鱼,黑鳍礁鲨,柠檬鲨,斑马鲨,狗头鱼,皇冠狗头鱼,半月斗鱼,白金斗鱼,鸽子血七彩神仙鱼,红松石七彩神仙鱼,花枝,巨型太平洋章鱼,椰子章鱼,蓝环章鱼,刺鲀,角箱鲀,蓝宝石七彩神仙鱼,黄金七彩神仙鱼,女王神仙鱼,毕加索小丑鱼,玻璃猫鱼,虎鲸,双髻鲨,虎鲨,鹰鳐,玳瑁,长尾鲨,大白鲨,抹香鲸,飞旋海豚,棱皮龟,月亮水母,彩色海刺水母,狮鬃水母,海兔,海天使,麒麟鱼,五彩青蛙鱼,叶海龙,草海龙,拟态章鱼,白鲸,独角鲸,冰霜雪灵鱼,月影幻蝶鱼,翻车鱼,哥布林鲨,巨型乌贼,深海幽灵鱼,鹦鹉螺,电鳐,烈焰朱雀鱼,翡翠森林鱼,黄金锦鲤王,斑马狮子鱼,狮子鱼,星河龙鱼,极光凤凰鱼,琉璃水晶鱼,彩虹幻梦鱼,蓑鲉,蓝鲸,座头鲸,鲸鲨,露脊鲸,中华白海豚,皇后神仙鱼,小飞象章鱼,帝王蟹,蝠鲼,花园鳗`.split(',');
const clamp=(x,a,b)=>Math.min(b,Math.max(a,x));
const hash=(a,b)=>((a*1103515245+b*2654435761)>>>0)/4294967296;
const safePoints=value=>{const n=Number(value);return Number.isFinite(n)?Math.max(0,Math.floor(n)):0};
const level=p=>clamp(Math.floor(Math.max(0,p)/50),0,19);
// Five species per 50-point stage. Each 10-point milestone reveals a stable,
// seeded random member of that stage; no storage or species encyclopedia needed.
const stageOrder=(stage,owner)=>Array.from({length:5},(_,i)=>stage*5+i)
 .sort((a,b)=>hash(a+1009*stage,owner+37)-hash(b+1009*stage,owner+37));
const selection=(p,owner)=>{const milestones=clamp(Math.floor(Math.max(0,p)/10)+1,1,100),pool=[];
 for(let k=0;k<milestones;k++){const stage=Math.floor(k/5);pool.push(stageOrder(stage,owner)[k%5]);}
 return pool;};
(async()=>{
let T;try{T=await import(THREE_URL)}catch(e){console.warn('Ocean3D unavailable: Canvas fallback retained');return}
let loader=null;let skeletonClone=null;try{const mod=await import(GLTF_URL);loader=new mod.GLTFLoader()}catch(e){console.warn('GLB loader unavailable; using geometry fallback',e)}
try{const skeletonModule=await import('three/addons/utils/SkeletonUtils.js');skeletonClone=skeletonModule.clone}catch(e){console.warn('Skinned GLB clone utility unavailable; skinned assets will use procedural fallback',e)}
const assetCache=new Map();const assetRoot=new URL('./',import.meta.url);
function fetchSpecies(index){if(!loader)return Promise.resolve(null);if(!assetCache.has(index)){const url=new URL('models/'+String(index).padStart(3,'0')+'.glb',assetRoot).href;assetCache.set(index,new Promise(resolve=>loader.load(url,g=>resolve({scene:g.scene,animations:g.animations||[]}),undefined,e=>{console.warn('Model missing',index,e);assetCache.delete(index);resolve(null)})))}return assetCache.get(index)}

// 100 reproducible per-species procedural texture sets. These are NOT hand-painted
// high-resolution zoologically accurate textures; they are GPU-friendly 512px maps.
const textureCache=new Map();
function speciesTextures(index){
 if(textureCache.has(index))return textureCache.get(index);
 const n=512, color=document.createElement('canvas'), bump=document.createElement('canvas');
 color.width=color.height=bump.width=bump.height=n;
 const c=color.getContext('2d'),b=bump.getContext('2d');
 const hue=(index*137.508)%360, name=NAMES[index]||'';
 const base=c.createLinearGradient(0,0,n,n);
 base.addColorStop(0,`hsl(${hue},62%,66%)`);
 base.addColorStop(.48,`hsl(${(hue+19)%360},58%,43%)`);
 base.addColorStop(1,`hsl(${(hue+195)%360},42%,22%)`);
 c.fillStyle=base;c.fillRect(0,0,n,n);
 b.fillStyle='#777';b.fillRect(0,0,n,n);
 // Fish scale arcs, shark fine denticles, cetacean smooth skin and jelly transparency.
 const smooth=/鲸|海豚|水母|章鱼|乌贼|花枝/.test(name);
 const shark=/鲨/.test(name);
 const rows=smooth?0:shark?75:36, step=rows?n/rows:0;
 for(let row=0;row<rows;row++)for(let col=0;col<Math.ceil(n/step)+1;col++){
  const x=(col+(row%2)*.5)*step,y=row*step;
  const phase=Math.sin(index*1.73+row*.67+col*.41);
  c.strokeStyle=`hsla(${(hue+phase*15+360)%360},75%,88%,${shark?.09:.30})`;
  b.strokeStyle=`rgba(225,225,225,${shark?.10:.32})`;
  c.lineWidth=shark?.5:1.25;b.lineWidth=shark?.5:1.1;
  for(const ctx of [c,b]){ctx.beginPath();ctx.arc(x,y,step*.57,.05,Math.PI-.05);ctx.stroke();}
 }
 // Stable species-specific bars, dots, or ripple markings.
 for(let k=0;k<70;k++){
  const x=hash(index*79+k,17)*n,y=hash(index*97+k,23)*n;
  const r=2+hash(index+k,41)*11;
  c.fillStyle=`hsla(${(hue+110+k*7)%360},75%,80%,${.08+hash(k,index)*.20})`;
  c.beginPath();c.ellipse(x,y,r,r*(.5+hash(k,9)),0,0,Math.PI*2);c.fill();
 }
 const map=new T.CanvasTexture(color),bumpMap=new T.CanvasTexture(bump);
 map.colorSpace=T.SRGBColorSpace;
 for(const tex of [map,bumpMap]){tex.wrapS=tex.wrapT=T.RepeatWrapping;tex.anisotropy=Math.min(8,renderer?.capabilities?.getMaxAnisotropy?.()||4)}
 const out={map,bumpMap};textureCache.set(index,out);return out;
}
// Release per-instance materials. GLB geometry and texture cache belong to the loader.
function releaseCreature(root){
 if(!root)return;
 root.traverse(node=>{
  if(!node.isMesh)return;
  const materials=Array.isArray(node.material)?node.material:[node.material];
  for(const material of materials){if(material?.userData?.oceanInstanceMaterial)material.dispose();}
  // Procedural fallback meshes own their geometries; GLB meshes share cached geometry.
  if(!root.userData?.asset3d)node.geometry?.dispose();
 });
}
function applySpeciesMaterial(root,index){
 const {map,bumpMap}=speciesTextures(index);
 root.traverse(node=>{if(!node.isMesh)return;
  const materials=Array.isArray(node.material)?node.material:[node.material];
  const converted=materials.map(m=>{
   const color=m?.color?.clone?.()||new T.Color(0xffffff);
   const mat=new T.MeshPhysicalMaterial({color, map, bumpMap,bumpScale:.035,
    metalness:/龙|神仙|锦鲤/.test(NAMES[index])?.22:.04,roughness:/鲨|鲸/.test(NAMES[index])?.58:.35,
    clearcoat:/水母|章鱼/.test(NAMES[index])?.4:.85,clearcoatRoughness:.18,
    transparent:/水母|玻璃猫|琉璃/.test(NAMES[index]),
    opacity:/水母|玻璃猫|琉璃/.test(NAMES[index])?.78:1,side:T.DoubleSide});
   mat.userData.oceanInstanceMaterial=true;return addWaterOptics(mat);
  });
  node.material=Array.isArray(node.material)?converted:converted[0];
 });
}

// Procedural articulated motion: independent mesh-part movement; species classes have distinct gaits.
function prepareGLBAnimation(root,index){
  const name=NAMES[index]||'';let kind='fish';
  if(/鲨/.test(name))kind='shark';else if(/鲸|海豚/.test(name))kind='whale';
  else if(/章鱼|乌贼|花枝/.test(name))kind='octopus';else if(/水母/.test(name))kind='jelly';
  else if(/海龟|玳瑁|棱皮龟/.test(name))kind='turtle';else if(/鳐|蝠鲼/.test(name))kind='ray';
  else if(/龙/.test(name))kind='fantasy';
  const parts=[];root.traverse(node=>{if(!node.isMesh)return;
    const label=node.name||node.parent?.name||'';
    const m=label.match(/_(\d+)$/);const ordinal=m?Number(m[1]):parts.length;
    parts.push({node,ordinal,rotation:node.rotation.clone(),position:node.position.clone()});
    const materials=Array.isArray(node.material)?node.material:[node.material];for(const m of materials){if(m){m.side=T.DoubleSide;if('roughness' in m)m.roughness=Math.min(m.roughness??.6,.55);if('metalness' in m)m.metalness=Math.max(m.metalness??0,.08);}}
  });
  root.userData.kind=kind;root.userData.parts=parts;
}
function animateGLB(mesh,time,seed){
  const parts=mesh.userData.parts;if(!parts)return;
  const kind=mesh.userData.kind;
  const speed=kind==='whale'?1.15:kind==='shark'?2.3:kind==='octopus'?1.6:kind==='jelly'?1.8:4;
  for(let i=0;i<parts.length;i++){
    const p=parts[i],phase=time*speed+seed+p.ordinal*.38;
    if(i===0){p.node.rotation.y=p.rotation.y+Math.sin(phase)*.035;continue}
    if(kind==='octopus'){
      p.node.rotation.z=p.rotation.z+Math.sin(phase)*.13;
      p.node.rotation.x=p.rotation.x+Math.cos(phase*.8)*.09;
    }else if(kind==='jelly'){
      p.node.position.y=p.position.y+Math.sin(phase)*.065;
      p.node.rotation.z=p.rotation.z+Math.sin(phase)*.045;
    }else if(kind==='turtle'||kind==='ray'){
      p.node.rotation.x=p.rotation.x+Math.sin(phase)*.10;
    }else{
      p.node.rotation.y=p.rotation.y+Math.sin(phase)*(.08+Math.min(.12,p.ordinal*.006));
    }
  }
}

const card=document.getElementById('aquariumCard'),original=document.getElementById('tank');if(!card||!original)return;
let renderer;try{renderer=new T.WebGLRenderer({alpha:true,antialias:true,powerPreference:'high-performance'});}catch{return}
const layer=renderer.domElement;layer.id='ocean3d-layer';layer.setAttribute('aria-hidden','true');Object.assign(layer.style,{position:'absolute',inset:'0',width:'100%',height:'100%',zIndex:'1',pointerEvents:'none'});card.append(layer);
const scene=new T.Scene();scene.fog=new T.FogExp2(0x164c65,.055);renderer.outputColorSpace=T.SRGBColorSpace;renderer.toneMapping=T.ACESFilmicToneMapping;renderer.toneMappingExposure=1.22;
const camera=new T.PerspectiveCamera(40,2,.1,160);camera.position.set(0,3,25);camera.lookAt(0,0,0);
scene.add(new T.HemisphereLight(0xc5faff,0x0b2a48,2.5));const sun=new T.DirectionalLight(0xfff0cb,3.3);sun.position.set(-6,12,8);scene.add(sun);
const fill=new T.DirectionalLight(0x62d8ff,1.25);fill.position.set(9,2,-8);scene.add(fill);
const seabed=new T.Mesh(new T.PlaneGeometry(80,40),new T.MeshStandardMaterial({color:0x164f57,roughness:1}));seabed.rotation.x=-Math.PI/2;seabed.position.y=-5.7;scene.add(seabed);
// Stage 3: real geometry for rocks, branching corals, swaying kelp and plankton.
let ecosystem=null;
try {const {createSeabedEcosystem}=await import('./seabed-ecosystem.js');ecosystem=createSeabedEcosystem(T,scene)} catch(e){console.warn('Ecosystem unavailable; base seabed retained',e)}
// Stage 5 habitat controller is initialized after lighting and water layers.
// Stage 2: real-time water optics, using wavelength-dependent Beer-Lambert
// attenuation on the actual rendered objects (not only on a backdrop).
// This is single-scattering-inspired real-time shading, NOT path-traced water.
const waterOptics={sigma:new T.Vector3(.105,.046,.020),ambient:new T.Color(0x1b8caa),density:.72};
function addWaterOptics(material){
 if(!material||material.userData?.waterOptics)return material;
 material.userData.waterOptics=true;
 const previous=material.onBeforeCompile;
 material.onBeforeCompile=function(shader,rendererInstance){
  if(previous)previous.call(this,shader,rendererInstance);
  shader.uniforms.uWaterSigma={value:waterOptics.sigma};
  shader.uniforms.uWaterTint={value:waterOptics.ambient};
  shader.uniforms.uWaterDensity={value:waterOptics.density};
  shader.vertexShader=shader.vertexShader.replace('#include <common>',`#include <common>
varying vec3 vWaterWorldPosition;`)
   .replace('#include <worldpos_vertex>',`#include <worldpos_vertex>
vWaterWorldPosition=(modelMatrix*vec4(transformed,1.0)).xyz;`);
  shader.fragmentShader=shader.fragmentShader.replace('#include <common>',`#include <common>
varying vec3 vWaterWorldPosition;
uniform vec3 uWaterSigma;
uniform vec3 uWaterTint;
uniform float uWaterDensity;`)
   .replace('#include <opaque_fragment>',`#include <opaque_fragment>
float waterTravel=min(35.0,length(cameraPosition-vWaterWorldPosition));
float waterDepth=max(0.0,4.0-vWaterWorldPosition.y);
vec3 waterT=exp(-uWaterSigma*(waterTravel+waterDepth*.24)*uWaterDensity);
gl_FragColor.rgb=mix(uWaterTint,gl_FragColor.rgb,waterT);`);
 };
 material.customProgramCacheKey=()=> 'water-optics-stage2-v1';
 return material;
}
// Animated refractive-looking water surface. Normals vary continuously;
// rendered as a thin transparent surface, without extra render targets.
const surfaceGeometry=new T.PlaneGeometry(55,30,80,40);
const surfaceMaterial=new T.MeshPhysicalMaterial({color:0x6fd9e9,metalness:0,roughness:.14,
 transparent:true,opacity:.10,depthWrite:false,side:T.DoubleSide,clearcoat:1,clearcoatRoughness:.12});
const waterSurface=new T.Mesh(surfaceGeometry,surfaceMaterial);
waterSurface.rotation.x=-Math.PI/2;waterSurface.position.set(0,5.35,-4);
scene.add(waterSurface);
const surfaceBase=Float32Array.from(surfaceGeometry.attributes.position.array);
function updateWaterSurface(time){
 const pos=surfaceGeometry.attributes.position;
 for(let i=0;i<pos.count;i++){
  const j=i*3,x=surfaceBase[j],y=surfaceBase[j+1];
  pos.array[j+2]=.13*Math.sin(x*.41+time*.75)*Math.cos(y*.32-time*.46)
    +.06*Math.sin(x*.93+y*.59+time*1.1);
 }
 pos.needsUpdate=true;surfaceGeometry.computeVertexNormals();
}
// Animated underwater caustics: lightweight projected-light approximation, no extra render pass.
const causticCanvas=document.createElement('canvas');causticCanvas.width=256;causticCanvas.height=256;const ctx=causticCanvas.getContext('2d');const causticTexture=new T.CanvasTexture(causticCanvas);causticTexture.wrapS=causticTexture.wrapT=T.RepeatWrapping;causticTexture.repeat.set(5,3);const causticPlane=new T.Mesh(new T.PlaneGeometry(75,40),new T.MeshBasicMaterial({map:causticTexture,transparent:true,opacity:.23,depthWrite:false,blending:T.AdditiveBlending}));causticPlane.rotation.x=-Math.PI/2;causticPlane.position.y=-5.67;scene.add(causticPlane);
function updateCaustics(time){ctx.clearRect(0,0,256,256);ctx.strokeStyle='rgba(145,233,255,.42)';ctx.lineWidth=2;for(let j=0;j<12;j++){ctx.beginPath();for(let i=0;i<=64;i++){const x=i*4,y=12+j*21+7*Math.sin(i*.32+time*.75+j*.8)+3*Math.cos(i*.19-time*.5);if(!i)ctx.moveTo(x,y);else ctx.lineTo(x,y)}ctx.stroke()}causticTexture.needsUpdate=true;causticTexture.offset.x=(time*.008)%1;}
// Approximate volumetric underwater light shafts: view-aligned translucent planes.
// This is a realtime scattering approximation, not physically exact volumetric path tracing.
const shaftMaterial=new T.ShaderMaterial({transparent:true,depthWrite:false,side:T.DoubleSide,
 blending:T.AdditiveBlending,uniforms:{uTime:{value:0},uDensity:{value:.22}},
 vertexShader:`varying vec2 vUv; void main(){vUv=uv;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}`,
 fragmentShader:`varying vec2 vUv;uniform float uTime;uniform float uDensity;
 void main(){float x=vUv.x+0.018*sin(uTime*.16+vUv.y*4.0);
 float a=pow(max(0.0,sin(x*30.0+sin(x*9.0)*1.7)),10.0);
 float depth=pow(1.0-vUv.y,1.4);float edge=smoothstep(0.0,.12,vUv.y)*smoothstep(1.0,.76,vUv.y);
 gl_FragColor=vec4(.34,.78,.92,a*depth*edge*uDensity);}`});
const shafts=[];for(let i=0;i<3;i++){const plane=new T.Mesh(new T.PlaneGeometry(29,13),shaftMaterial.clone());
 plane.position.set((i-1)*2,1.0,-4-i*3);plane.rotation.y=(i-1)*.09;scene.add(plane);shafts.push(plane)}
let habitats=null;
try {const {createHabitatController}=await import('./habitat-stages.js');habitats=createHabitatController(T,scene,{ecosystem,seabed,waterSurface,causticPlane,sun,fill,shafts});habitats.setStage(0)} catch(e){console.warn('Habitat stage system unavailable',e)}
// Depth-color gradient gives more convincing attenuation without an extra framebuffer.
const hazeMaterial=new T.ShaderMaterial({transparent:true,depthWrite:false,side:T.DoubleSide,
 uniforms:{uTime:{value:0}},vertexShader:`varying vec2 vUv;void main(){vUv=uv;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}`,
 fragmentShader:`varying vec2 vUv;uniform float uTime;void main(){float opticalDepth=2.0+7.0*(1.0-vUv.y);vec3 sigma=vec3(.26,.085,.035);vec3 transmittance=exp(-sigma*opticalDepth);vec3 scattered=vec3(.025,.24,.33)*(vec3(1.0)-transmittance);float a=.12+.10*(1.0-vUv.y);gl_FragColor=vec4(scattered,a);}`});
const haze=new T.Mesh(new T.PlaneGeometry(35,16),hazeMaterial);haze.position.z=-14;scene.add(haze);
const bubbles=[];for(let i=0;i<48;i++){const b=new T.Mesh(new T.SphereGeometry(.025+(i%4)*.012,8,6),new T.MeshPhysicalMaterial({color:0xc6faff,transparent:true,opacity:.18,roughness:.05}));b.position.set(hash(i,3)*25-12,hash(i,7)*11-5,hash(i,11)*-12);scene.add(b);bubbles.push(b)}
function mat(c,metal=0){return addWaterOptics(new T.MeshPhysicalMaterial({color:c,metalness:metal,roughness:.35,clearcoat:.8,clearcoatRoughness:.17,side:T.DoubleSide}))}
const finMat=mat(0x83d6e2,.12),eyeMat=mat(0x081827);
function fin(g,points,color){const shape=new T.Shape();shape.moveTo(...points[0]);for(let i=1;i<points.length;i++)shape.lineTo(...points[i]);shape.closePath();const m=new T.Mesh(new T.ShapeGeometry(shape),color);g.add(m);return m}
function makeCreature(index,owner){const name=NAMES[index]||NAMES[0],root=new T.Group(),tone=new T.Color().setHSL((index*.618033+owner*.13)%1,.68,.54);const skin=mat(tone,.16);let kind='fish';if(/鲸|海豚|鲨/.test(name))kind='cetacean';if(/章鱼|乌贼|花枝/.test(name))kind='octopus';if(/水母|海天使/.test(name))kind='jelly';if(/海龟|玳瑁|棱皮龟/.test(name))kind='turtle';if(/鳐|蝠鲼/.test(name))kind='ray';if(/海星|帝王蟹/.test(name))kind='star';
const body=new T.Mesh(new T.SphereGeometry(1,20,14),skin);body.scale.set(kind==='cetacean'?1.8:kind==='jelly'?.75:1.12,kind==='ray'?.22:kind==='jelly'?.62:.57,kind==='ray'?1.4:.48);root.add(body);
const eye=new T.Mesh(new T.SphereGeometry(.085,10,8),eyeMat);eye.position.set(.67,.13,.43);root.add(eye);const eye2=eye.clone();eye2.position.z=-.43;root.add(eye2);
const tail=new T.Group();tail.position.x=-1.02;root.add(tail);
if(kind==='fish'||kind==='cetacean'){fin(tail,[[0,0],[-.72,.68],[-.55,0],[-.72,-.68]],finMat);fin(root,[[-.4,.35],[-.1,1.04],[.35,.39]],finMat);const fin2=fin(root,[[.2,-.22],[-.28,-.78],[-.12,-.22]],finMat);fin2.position.z=.36;
if(kind==='cetacean'){body.scale.set(1.85,.56,.5);root.scale.setScalar(1.22)}}
else if(kind==='jelly'){eye.visible=false;eye2.visible=false;body.scale.set(.83,.43,.83);for(let i=0;i<9;i++){const tentacle=new T.Mesh(new T.CylinderGeometry(.012,.007,1.25,5),finMat);const a=i*Math.PI*2/9;tentacle.position.set(Math.cos(a)*.46,-.85,Math.sin(a)*.46);root.add(tentacle)}root.userData.pulse=body;}
else if(kind==='octopus'){body.scale.set(.75,.65,.68);for(let i=0;i<8;i++){const a=i*Math.PI/4;const arm=new T.Mesh(new T.CapsuleGeometry(.095,1.4,5,8),skin);arm.rotation.z=Math.PI/2;arm.position.set(Math.cos(a)*.9,-.35,Math.sin(a)*.8);arm.rotation.y=-a;root.add(arm)}}
else if(kind==='ray'){body.scale.set(1,.20,1.1);fin(root,[[.4,0],[-1.1,1.15],[-.7,0],[-1.1,-1.15]],skin);}
else if(kind==='turtle'){body.scale.set(.95,.5,.9);for(let i=0;i<4;i++){const limb=new T.Mesh(new T.SphereGeometry(.4,10,8),skin);limb.scale.set(.9,.22,.42);limb.position.set(i<2?.5:-.6,-.1,i%2?.95:-.95);root.add(limb)}}
else if(kind==='star'){body.scale.set(.6,.2,.6)}
root.userData={name,kind,tail,owner,index};return root}
function makeDragon(owner){const root=new T.Group(),gold=mat(owner?0x80c7ed:0xe9b75b,.65),segments=[];for(let i=0;i<18;i++){const part=new T.Mesh(new T.SphereGeometry(.28-i*.008,12,10),gold);part.position.x=2.5-i*.3;root.add(part);segments.push(part)}const head=new T.Mesh(new T.SphereGeometry(.43,16,12),gold);head.position.x=2.7;root.add(head);for(const sign of [-1,1]){const horn=new T.Mesh(new T.ConeGeometry(.11,.68,8),gold);horn.position.set(2.7,.52,sign*.24);root.add(horn)}root.userData={kind:'dragon',segments,owner};return root}
const schools=[[],[]];const retiring=[];const geometryCache=new Map();let current=[-1,-1],frame=0,last=0,active=true,ready=false,raf=0;
// Stage 6: bounded render resolution, conservative adaptive quality and lifecycle management.
const mobileDevice=/iPhone|iPad|iPod|Android/i.test(navigator.userAgent);
const maxDpr=Math.min(window.devicePixelRatio||1,mobileDevice?1.65:2);
let adaptiveDpr=maxDpr,lastSize='',fpsEma=60,lastAdjust=0;
let targetFps=60,lastFrameTs=0,frameSamples=0,slowWindows=0,fastWindows=0;
let lastCaustics=0,contextLost=false,disposed=false;
const reduceMotion=matchMedia('(prefers-reduced-motion: reduce)').matches;
if(reduceMotion)targetFps=30;

let lastKnownScores=[0,0];function getScores(){try{const entries=window.AquariumScores?.getStatus()?.entries;if(Array.isArray(entries)&&entries.length>=2){lastKnownScores=entries.slice(0,2).map(e=>{const n=Number(e.points);return Number.isFinite(n)?Math.max(0,n):0});}return lastKnownScores;}catch{return lastKnownScores;}}
// Stage 4: deterministic, bounded flocking in 3D. Only nearby members of the
// same child's school interact; this does not mix ownership or score tiers.
function swimProfile(name,kind,seed){
  const slow=/鲸|海龟|玳瑁|水母|海天使|章鱼|海马/.test(name);
  const predator=/鲨|梭鱼|旗鱼|剑鱼/.test(name);
  return {speed:slow?.20:predator?.57:.35, maxSpeed:slow?.33:predator?.82:.58,
    turn:slow?.65:predator?1.25:1.65, bob:slow?.12:.22,
    tailHz:/水母|海天使/.test(name)?.65:slow?1.1:predator?2.5:3.7,
    group:/水母|章鱼|鲸|海龟|海马/.test(name)?.15:1, seed};
}
function steerSchool(group,dt,time){
  const members=group.filter(a=>!a.dragon&&a.mesh?.parent===scene);
  const cellSize=3.2,grid=new Map();
  const key=(x,y,z)=>`${x},${y},${z}`;
  for(const a of members){const p=a.mesh.position;a.cell=[Math.floor(p.x/cellSize),Math.floor(p.y/cellSize),Math.floor(p.z/cellSize)];const k=key(...a.cell);if(!grid.has(k))grid.set(k,[]);grid.get(k).push(a)}
  for(const a of members){const p=a.mesh.position,pr=a.profile;let ax=0,ay=0,az=0,sepX=0,sepY=0,sepZ=0,alignX=0,alignY=0,alignZ=0,n=0;
    for(let x=-1;x<=1;x++)for(let y=-1;y<=1;y++)for(let z=-1;z<=1;z++){
      const others=grid.get(key(a.cell[0]+x,a.cell[1]+y,a.cell[2]+z));if(!others)continue;
      for(const b of others){if(a===b)continue;const q=b.mesh.position,dx=q.x-p.x,dy=q.y-p.y,dz=q.z-p.z,d2=dx*dx+dy*dy+dz*dz;
        if(d2>11||d2<.0001)continue;n++;ax+=dx;ay+=dy;az+=dz;alignX+=b.heading.x;alignY+=b.heading.y;alignZ+=b.heading.z;
        if(d2<2.2){const inv=1/(d2+.12);sepX-=dx*inv;sepY-=dy*inv;sepZ-=dz*inv;}
      }
    }
    let fx=0,fy=0,fz=0;
    if(n){fx+=(ax/n*.22+alignX/n*.38)*pr.group;fy+=(ay/n*.20+alignY/n*.24)*pr.group;fz+=(az/n*.22+alignZ/n*.32)*pr.group;}
    fx+=sepX*.75;fy+=sepY*.75;fz+=sepZ*.75;
    // Soft volume containment and seabed / surface avoidance; no hard teleports.
    fx+=p.x>8.5?-(p.x-8.5)*.8:p.x< -8.5?(-8.5-p.x)*.8:0;
    fy+=p.y>3.6?-(p.y-3.6)*1.2:p.y< -3.3?(-3.3-p.y)*1.2:0;
    fz+=p.z>1.8?-(p.z-1.8)*.85:p.z< -8.5?(-8.5-p.z)*.85:0;
    // Gentle deterministic current and independent wandering.
    fx+=Math.sin(time*.21+a.seed*5)*.10+.05;
    fy+=Math.sin(time*.43+a.seed*7)*.08;
    fz+=Math.cos(time*.25+a.seed*9)*.11;
    const h=a.heading,rate=Math.min(1,dt*pr.turn);
    const desired=new T.Vector3(h.x+fx*dt*2,h.y+fy*dt*2,h.z+fz*dt*2);
    if(desired.lengthSq()<.001)desired.set(1,0,0);desired.normalize();h.lerp(desired,rate).normalize();
    const speed=pr.speed*(.82+.18*Math.sin(time*.7+a.seed*3));
    p.addScaledVector(h,Math.min(pr.maxSpeed,speed)*dt);
    // GLB assets and procedural fish have their forward axis along local +X.
    const yaw=Math.atan2(-h.z,h.x);const d=((yaw-a.mesh.rotation.y+Math.PI*3)%(2*Math.PI))-Math.PI;
    a.mesh.rotation.y+=d*Math.min(1,dt*pr.turn*2);
    a.mesh.rotation.z+=(Math.max(-.13,Math.min(.13,h.y*.2))-a.mesh.rotation.z)*Math.min(1,dt*2);
  }
}
function refresh(scores){const sharedPoints=Math.max(safePoints(scores?.[0]),safePoints(scores?.[1]));habitats?.setStage(sharedPoints>=1000?20:Math.floor(sharedPoints/50));for(let owner=0;owner<2;owner++){const p=safePoints(scores?.[owner]),stage=p>=1000?20:level(p),count=Math.min(11,Math.max(2,Math.floor(p/80)+2));const key=stage+'|'+Math.floor(Math.min(999,Math.max(0,p))/10)+'|'+count;if(current[owner]===key)continue;current[owner]=key;const pool=selection(Math.min(p,999),owner);for(const item of schools[owner]){item.mixer?.stopAllAction();item.mixer?.uncacheRoot(item.mesh);item.mixer=null;item.retireAt=performance.now();item.retireScale=item.mesh.scale.clone();retiring.push(item);}schools[owner]=[];for(let j=0;j<count;j++){const pick=pool[Math.floor(hash(j+stage*17,owner+3)*pool.length)],mesh=makeCreature(pick,owner);mesh.scale.multiplyScalar(.28+(pick>=50?.09:0));mesh.position.set(hash(j,owner*3+1)*20-10,hash(j,owner*5+4)*6-3,-hash(j,owner*7+5)*8);scene.add(mesh);const slot={mesh,j,bornAt:performance.now(),speed:.18+hash(j,owner)*.36,seed:hash(j+3,owner)*10,baseY:mesh.position.y,baseZ:mesh.position.z,phase:hash(j+47,owner)*Math.PI*2,dir:j%2?-1:1,turn:0,profile:swimProfile(NAMES[pick],mesh.userData.kind,hash(j+3,owner)),heading:new T.Vector3(j%2?-1:1,(hash(j+11,owner)-.5)*.2,(hash(j+13,owner)-.5)*.5).normalize()};schools[owner].push(slot);fetchSpecies(pick).then(model=>{if(disposed||!model||!scene.children.includes(mesh)||slot.mesh!==mesh)return;const hasSkin=!!model.scene.getObjectByProperty('isSkinnedMesh',true);if(hasSkin&&!skeletonClone){console.warn('Skipping skinned asset without SkeletonUtils',pick);return;}const replacement=hasSkin?skeletonClone(model.scene):model.scene.clone(true);replacement.scale.copy(slot.baseScale||mesh.scale);slot.baseScale=replacement.scale.clone();replacement.position.copy(mesh.position);replacement.rotation.copy(mesh.rotation);replacement.userData={...mesh.userData,asset3d:true,kind:(model.scene.userData.kind||mesh.userData.kind)};applySpeciesMaterial(replacement,pick);prepareGLBAnimation(replacement,pick);if(model.animations.length){const mixer=new T.AnimationMixer(replacement);const clip=model.animations.find(c=>/swim|idle|loop/i.test(c.name))||model.animations[0];mixer.clipAction(clip).play();slot.mixer=mixer;replacement.userData.hasAuthoredAnimation=true;}scene.remove(mesh);releaseCreature(mesh);scene.add(replacement);slot.mesh=replacement})}
if(p>=1000){const dragon=makeDragon(owner);dragon.scale.set(.85,.85,.85);dragon.position.set(owner?-5:5,2,-7);scene.add(dragon);schools[owner].push({mesh:dragon,j:99,speed:.13,seed:owner*3,dragon:true})}}}
function resize(){const r=card.getBoundingClientRect();if(r.width<1||r.height<1)return false;const size=`${Math.round(r.width)}x${Math.round(r.height)}@${adaptiveDpr.toFixed(2)}`;if(size===lastSize)return true;lastSize=size;renderer.setPixelRatio(adaptiveDpr);renderer.setSize(r.width,r.height,false);camera.aspect=r.width/r.height;camera.position.z=r.width<500?30:25;camera.updateProjectionMatrix();return true}
function animate(ts){
 if(disposed)return;
 raf=requestAnimationFrame(animate);
 if(document.hidden||!active||contextLost||!card.isConnected){last=ts;return;}
 if(!resize()){last=ts;return;}
 const rawDt=Math.max(.001,Math.min(.1,(ts-last)/1000||.016));
 const dt=Math.min(.05,rawDt);last=ts;
 // Exclude background-tab gaps and first frames from FPS estimates.
 if(rawDt<.09){fpsEma=fpsEma*.94+(1/rawDt)*.06;frameSamples++;}
 if(ts-lastAdjust>4000&&frameSamples>=30){
   lastAdjust=ts;frameSamples=0;
   const low=fpsEma<targetFps*.82,high=fpsEma>targetFps*.96;
   slowWindows=low?slowWindows+1:0;fastWindows=high?fastWindows+1:0;
   if(slowWindows>=2){adaptiveDpr=Math.max(mobileDevice?.7:.75,adaptiveDpr-.12);slowWindows=0;fastWindows=0;}
   else if(fastWindows>=3){adaptiveDpr=Math.min(maxDpr,adaptiveDpr+.06);fastWindows=0;}
 }

if(reduceMotion&&ts-lastFrameTs<30)return;lastFrameTs=ts;frame++;if(frame%18===1)refresh(getScores());if(frame%120===0)window.__ocean3dStats={engine:"Three.js WebGL2/WebGL",version:"stage10-regression-20261031",mobileDevice,contextLost,renderBudget:'adaptive DPR and visibility-aware scheduling',habitat:habitats?.name,habitatStage:habitats?.stage,ecosystem:ecosystem?.counts||null,fps:Math.round(fpsEma),pixelRatio:+adaptiveDpr.toFixed(2),activeCreatures:schools.reduce((n,g)=>n+g.length,0),targetFps,waterScattering:'wavelength-dependent Beer-Lambert on meshes + animated surface + scattering approximation',modelQuality:'procedural GLB'};if(ts-lastCaustics>(adaptiveDpr<1?160:90)){lastCaustics=ts;updateCaustics(ts*.001);}const time=ts*.001;for(const sh of shafts){sh.material.uniforms.uTime.value=time;sh.material.uniforms.uDensity.value=adaptiveDpr<1?.13:.22;}hazeMaterial.uniforms.uTime.value=time;if(frame%(adaptiveDpr<1?6:3)===0)updateWaterSurface(time);if(ecosystem&&frame%(adaptiveDpr<.9?2:1)===0)ecosystem.update(time,adaptiveDpr);habitats?.update(dt,time);
// Crossfade school changes by scaling exiting creatures down and arriving creatures up.
// This avoids abrupt removal and does not modify shared GLB geometry/materials.
for(let i=retiring.length-1;i>=0;i--){const item=retiring[i],t=clamp((ts-item.retireAt)/1100,0,1);item.mesh.scale.copy(item.retireScale).multiplyScalar(Math.max(.001,1-t));item.mesh.position.z-=dt*1.3;if(t>=1){scene.remove(item.mesh);releaseCreature(item.mesh);retiring.splice(i,1);}}
for(const group of schools)for(const item of group){const {mesh,j,seed,speed}=item;
if(item.bornAt){const age=clamp((ts-item.bornAt)/900,0,1);if(!item.baseScale)item.baseScale=mesh.scale.clone();mesh.scale.copy(item.baseScale).multiplyScalar(Math.max(.001,age));if(age>=1)item.bornAt=0;}
// Flocking steering is applied once per owner below; species-specific body motion here.
const tailRate=item.profile?.tailHz||1.5;
mesh.userData.tail?.rotation.set(0,Math.sin(time*tailRate*2*Math.PI+seed)*.32,0);
if(item.mixer)item.mixer.update(dt);else if(mesh.userData.asset3d)animateGLB(mesh,time*(tailRate/2.5),seed);
if(mesh.userData.segments)mesh.userData.segments.forEach((seg,k)=>{seg.position.y=Math.sin(time*1.6-k*.43+seed)*.25;seg.position.z=Math.cos(time*1.3-k*.43+seed)*.17});if(mesh.userData.pulse)mesh.userData.pulse.scale.y=.43+Math.sin(time*2+seed)*.05;}
for(const group of schools)steerSchool(group,dt,time);
for(let i=0;i<bubbles.length;i++){const b=bubbles[i];b.position.y+=dt*(.17+i%5*.045);if(b.position.y>6)b.position.y=-5.5}renderer.render(scene,camera);if(!ready){ready=true;original.style.opacity='0';layer.style.opacity='1'}}
layer.style.opacity='0';
// Restore the original 2D tank if WebGL is lost; the browser may later restore it.
layer.addEventListener('webglcontextlost',event=>{event.preventDefault();contextLost=true;original.style.opacity='1';layer.style.opacity='0';});
layer.addEventListener('webglcontextrestored',()=>{contextLost=false;lastSize='';last=performance.now();layer.style.opacity='1';original.style.opacity='0';});
const onVisibility=()=>{last=performance.now();frameSamples=0;};
document.addEventListener('visibilitychange',onVisibility);
const ro=new ResizeObserver(resize);const observer=new IntersectionObserver(entries=>{active=entries[0]?.isIntersecting??true},{threshold:0});observer.observe(card);ro.observe(card);window.addEventListener('pagehide',()=>{disposed=true;active=false;cancelAnimationFrame(raf);document.removeEventListener('visibilitychange',onVisibility);ro.disconnect();observer.disconnect();for(const item of [...schools.flat(),...retiring]){item.mixer?.stopAllAction();item.mixer?.uncacheRoot(item.mesh);scene.remove(item.mesh);releaseCreature(item.mesh);}assetCache.clear();causticTexture.dispose();for(const t of textureCache.values()){t.map.dispose();t.bumpMap.dispose()}for(const sh of shafts){sh.geometry.dispose();sh.material.dispose()}haze.geometry.dispose();hazeMaterial.dispose();surfaceGeometry.dispose();surfaceMaterial.dispose();habitats?.dispose();ecosystem?.dispose();renderer.dispose()},{once:true});refresh(getScores());animate(performance.now());
})();
