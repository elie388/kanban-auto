// Stage 3: deterministic, lightweight procedural reef environment.
// These are stylized procedural habitat meshes, not scanned real-world coral assets.
export function createSeabedEcosystem(T, scene, {seed=314159}={}) {
  let state=seed>>>0;
  const rnd=()=>{state=(1664525*state+1013904223)>>>0;return state/4294967296};
  const root=new T.Group();root.name='SeabedEcosystem';scene.add(root);
  const ownedGeometries=[],ownedMaterials=[],kelp=[],coral=[];
  const material=(opts)=>{const m=new T.MeshStandardMaterial(opts);ownedMaterials.push(m);return m};
  const rockGeo=new T.DodecahedronGeometry(1,1);ownedGeometries.push(rockGeo);
  const rockMat=material({color:0x3b6968,roughness:1,flatShading:true});
  const rocks=new T.InstancedMesh(rockGeo,rockMat,48);rocks.name='SeabedRocks';
  const dummy=new T.Object3D();
  for(let i=0;i<48;i++){
    const x=(rnd()-.5)*46,z=-2-rnd()*17;
    dummy.position.set(x,-5.65+rnd()*.12,z);
    dummy.rotation.set(rnd()*2,rnd()*6,rnd()*2);
    dummy.scale.set(.35+rnd()*1.25,.2+rnd()*.52,.35+rnd()*.95);
    dummy.updateMatrix();rocks.setMatrixAt(i,dummy.matrix);
  }
  rocks.instanceMatrix.needsUpdate=true;rocks.frustumCulled=false;root.add(rocks);
  // Branching corals: merged into two instanced cylinder batches for draw-call economy.
  const branchGeo=new T.CylinderGeometry(.065,.13,1,5);ownedGeometries.push(branchGeo);
  const coralMats=[material({color:0xe79b83,roughness:.82}),material({color:0x8f9ec9,roughness:.86})];
  const coralMeshes=coralMats.map(m=>new T.InstancedMesh(branchGeo,m,540));
  const branchCounts=[0,0];
  for(let i=0;i<64;i++){
    const cx=(rnd()-.5)*42,cz=-3-rnd()*15,group=i%2;
    const n=4+Math.floor(rnd()*5),height=.35+rnd()*.75;
    for(let j=0;j<n;j++){
      const angle=j/n*Math.PI*2+rnd()*.4,spread=.25+rnd()*.4;
      dummy.position.set(cx+Math.cos(angle)*spread*.5,-5.53+height*.45,cz+Math.sin(angle)*spread*.5);
      dummy.rotation.set(Math.sin(angle)*.55,0,-Math.cos(angle)*.55);
      dummy.scale.set(.6+rnd()*.6,height,.6+rnd()*.6);dummy.updateMatrix();
      coralMeshes[group].setMatrixAt(branchCounts[group]++,dummy.matrix);
    }
  }
  coralMeshes.forEach((mesh,i)=>{mesh.count=branchCounts[i];mesh.instanceMatrix.needsUpdate=true;mesh.frustumCulled=false;mesh.name='CoralBranches'+i;root.add(mesh)});
  // Kelp blades: vertex displacement in GPU shader, independent phases and current.
  const bladeGeo=new T.PlaneGeometry(.24,1,1,9);ownedGeometries.push(bladeGeo);
  bladeGeo.translate(0,.5,0);
  const kelpMaterial=new T.MeshStandardMaterial({color:0x317d54,side:T.DoubleSide,roughness:.9,transparent:true,opacity:.86});
  ownedMaterials.push(kelpMaterial);
  const uniforms={uTime:{value:0},uCurrent:{value:.17}};
  kelpMaterial.onBeforeCompile=shader=>{
    shader.uniforms.uTime=uniforms.uTime;shader.uniforms.uCurrent=uniforms.uCurrent;
    shader.vertexShader=shader.vertexShader.replace('#include <common>','#include <common>\nuniform float uTime; uniform float uCurrent; attribute float aPhase;');
    shader.vertexShader=shader.vertexShader.replace('#include <begin_vertex>',`#include <begin_vertex>
      float bend=pow(max(position.y,0.0),1.45);
      transformed.x+=bend*(sin(uTime*0.95+aPhase+position.y*1.8)*0.17+uCurrent);
      transformed.z+=bend*cos(uTime*.65+aPhase)*.09;`);
  };
  kelpMaterial.customProgramCacheKey=()=> 'seabed-kelp-v1';
  // Each blade is a mesh; capped at 100 and culled by renderer.
  for(let i=0;i<95;i++){
    const geom=bladeGeo.clone();ownedGeometries.push(geom);
    const phase=new Float32Array(geom.attributes.position.count).fill(rnd()*Math.PI*2);
    geom.setAttribute('aPhase',new T.BufferAttribute(phase,1));
    const blade=new T.Mesh(geom,kelpMaterial);
    blade.position.set((rnd()-.5)*42,-5.55,-2-rnd()*16);
    blade.rotation.y=rnd()*Math.PI*2;
    blade.scale.set(.75+rnd()*.9,.55+rnd()*1.8,1);
    root.add(blade);kelp.push(blade);
  }
  // Suspended plankton: one GPU points object; no per-frame CPU particle loops.
  const n=380,pos=new Float32Array(n*3),sizes=new Float32Array(n);
  for(let i=0;i<n;i++){pos[i*3]=(rnd()-.5)*48;pos[i*3+1]=-5+rnd()*10;pos[i*3+2]=-3-rnd()*22;sizes[i]=rnd()}
  const particleGeo=new T.BufferGeometry();particleGeo.setAttribute('position',new T.BufferAttribute(pos,3));ownedGeometries.push(particleGeo);
  const particleMat=new T.PointsMaterial({color:0xa4ebd8,size:.045,transparent:true,opacity:.38,depthWrite:false,sizeAttenuation:true});ownedMaterials.push(particleMat);
  const particles=new T.Points(particleGeo,particleMat);particles.name='Plankton';root.add(particles);
  const setHabitat=({coral=1,kelp:kelpRatio=1,stage=0}={})=>{
    for(let i=0;i<coralMeshes.length;i++)coralMeshes[i].count=Math.max(0,Math.min(branchCounts[i],Math.round(branchCounts[i]*coral)));
    for(let i=0;i<kelp.length;i++)kelp[i].visible=i<Math.round(kelp.length*kelpRatio);
    rockMat.color.setHex(stage>=17?0x45536b:stage>=10?0x3c606b:0x3b6968);
    coralMats[0].color.setHex(stage>=14?0x6f96c0:0xe79b83);
    coralMats[1].color.setHex(stage>=14?0x668db7:0x8f9ec9);
  };
  const update=(time,quality=1)=>{uniforms.uTime.value=time;uniforms.uCurrent.value=.10+.06*Math.sin(time*.14);particles.rotation.y=Math.sin(time*.035)*.008;particles.visible=quality>.55};
  const dispose=()=>{scene.remove(root);for(const g of ownedGeometries)g.dispose();for(const m of ownedMaterials)m.dispose()};
  return {root,update,setHabitat,dispose,counts:{rocks:48,coralBranches:branchCounts.reduce((a,b)=>a+b,0),kelp:95,plankton:n}};
}
