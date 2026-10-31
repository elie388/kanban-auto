// Stage 5: 20 deterministic 50-point ocean habitats, 1000+ dragon realm.
// Shared environment is determined by max(childA, childB); species remain per child.
export const HABITATS=[
 ['初生浅海',0x2388a0,0x71b8a9,0x8acbd2,.028,.06,.04],
 ['珊瑚萌芽',0x237e9c,0x6aaf9d,0x80c4d1,.031,.14,.08],
 ['热带浅礁',0x197d9b,0x6db29a,0x80cad6,.034,.24,.13],
 ['珊瑚花园',0x19789b,0x7cb39e,0x89c6d4,.035,.34,.18],
 ['海草秘境',0x186f88,0x4b9d85,0x7cbbbc,.039,.38,.43],
 ['蓝色海湾',0x176b91,0x5d9e98,0x70bad0,.043,.35,.34],
 ['海底峡谷',0x175c81,0x658e96,0x65a5c5,.047,.35,.35],
 ['海洋森林',0x145e79,0x498d78,0x70abb6,.050,.43,.70],
 ['远洋入口',0x125a85,0x518a95,0x68a4c4,.053,.48,.48],
 ['深蓝远洋',0x124b75,0x487a92,0x6296bb,.056,.50,.43],
 ['鲨鱼海域',0x103f6a,0x4b7588,0x5a8eaa,.060,.52,.40],
 ['巨型礁群',0x174e6d,0x547f8a,0x65a0b4,.060,.78,.49],
 ['鲸歌海域',0x0e4569,0x52768d,0x6094ad,.063,.55,.43],
 ['深海边缘',0x103653,0x44687c,0x5188a1,.068,.54,.35],
 ['荧光秘境',0x143652,0x4b638d,0x508aaf,.072,.65,.45],
 ['深海花园',0x18354e,0x5b628c,0x5687a8,.074,.75,.52],
 ['远古海域',0x15374e,0x586c79,0x608c99,.074,.78,.60],
 ['神秘海沟',0x10283d,0x44576e,0x50788c,.079,.68,.35],
 ['海洋圣域',0x16384c,0x697d8d,0x7d9cad,.073,.88,.72],
 ['龙宫前夜',0x173c59,0x858a9e,0x94b8c4,.070,1,1],
 ['神龙领域',0x213b64,0xb6a59c,0xa7c8d3,.063,1,1]
];
export function createHabitatController(T,scene,{ecosystem,seabed,waterSurface,causticPlane,sun,fill,shafts}={}){
 const target=new T.Color(),fogColor=new T.Color();let index=-1,active=0;
 const landmarkRoot=new T.Group();landmarkRoot.name='HabitatLandmarks';scene.add(landmarkRoot);
 const geometry=new T.CylinderGeometry(.22,.46,1,7), material=new T.MeshStandardMaterial({color:0x6a8e9c,roughness:.8,metalness:.12});
 const markers=new T.InstancedMesh(geometry,material,36);markers.name='AncientRuinsAndCanyonPillars';landmarkRoot.add(markers);
 const d=new T.Object3D();for(let i=0;i<36;i++){const angle=i*2.39996323,r=5+Math.sqrt(i)*2.1;d.position.set(Math.cos(angle)*r,-5.35,-7+Math.sin(angle)*r*.7);d.rotation.set(0,angle,0);d.scale.set(.65+(i%3)*.22,.9+(i%7)*.56,.65+(i%4)*.12);d.updateMatrix();markers.setMatrixAt(i,d.matrix)}markers.instanceMatrix.needsUpdate=true;
 const orbGeo=new T.SphereGeometry(.095,8,6),orbMat=new T.MeshBasicMaterial({color:0x7bdcff,transparent:true,opacity:.0,depthWrite:false});
 const orbs=new T.InstancedMesh(orbGeo,orbMat,80);orbs.name='Bioluminescence';landmarkRoot.add(orbs);
 for(let i=0;i<80;i++){const angle=i*2.39996323,r=2+Math.sqrt(i)*1.9;d.position.set(Math.cos(angle)*r,-4+(i%13)*.66,-5+Math.sin(angle)*r*.6);d.scale.setScalar(.45+(i%5)*.16);d.updateMatrix();orbs.setMatrixAt(i,d.matrix)}orbs.instanceMatrix.needsUpdate=true;
 function setStage(stage){active=Math.max(0,Math.min(20,Math.floor(Number(stage)||0)));if(index===active)return;index=active;const h=HABITATS[active];markers.count=active<6?0:active<12?12:active<18?23:36;orbs.count=active<14?0:Math.min(80,10+(active-13)*12);ecosystem?.setHabitat?.({coral:h[5],kelp:h[6],stage:active});}
 function update(dt,time){const h=HABITATS[active],alpha=1-Math.exp(-Math.min(.1,dt)*.75);fogColor.setHex(h[1]);scene.fog.color.lerp(fogColor,alpha);scene.fog.density+=(h[4]-scene.fog.density)*alpha;target.setHex(h[2]);seabed.material.color.lerp(target,alpha);target.setHex(h[3]);waterSurface.material.color.lerp(target,alpha);sun.intensity+=(Math.max(.8,3.3-active*.105)-sun.intensity)*alpha;fill.intensity+=(Math.max(.6,1.25-active*.025)-fill.intensity)*alpha;causticPlane.material.opacity+=(Math.max(.06,.23-active*.008)-causticPlane.material.opacity)*alpha;orbMat.opacity+=(active>=14?.45:0-orbMat.opacity)*Math.min(1,dt*.5);orbs.rotation.y=Math.sin(time*.08)*.025;}
 function dispose(){scene.remove(landmarkRoot);geometry.dispose();material.dispose();orbGeo.dispose();orbMat.dispose()}
 return {setStage,update,dispose,get stage(){return active},get name(){return HABITATS[active][0]}};
}
