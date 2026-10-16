"""A dependency-free layout preview, explicitly not a game-rendered screenshot."""
from __future__ import annotations

import json


def render(objects: dict) -> bytes:
    data = json.dumps(list(objects.values()), allow_nan=False).replace("<", "\\u003c").replace("&", "\\u0026")
    html = '''<!doctype html><html lang="en"><meta charset="utf-8"><title>RecFlare build layout</title>
<style>body{margin:0;background:#111827;color:#e5e7eb;font:14px system-ui}header{padding:18px 24px;background:#1f2937}h1{font-size:20px;margin:0 0 6px}p{margin:0;color:#cbd5e1}canvas{display:block;width:100vw;height:calc(100vh - 90px);touch-action:none}label{margin-left:18px}</style>
<header><h1>RecFlare build layout</h1><p>Bounding boxes of MCP-created pieces. Not a game screenshot. Drag to orbit; scroll to zoom.<label><input id="names" type="checkbox"> Names</label></p></header><canvas></canvas>
<script>
const objects=DATA,canvas=document.querySelector('canvas'),ctx=canvas.getContext('2d');
let yaw=.65,pitch=.55,zoom=25,drag=false,last=[0,0];
const center=objects.length?[0,1,2].map(i=>objects.reduce((s,o)=>s+o.position[i],0)/objects.length):[0,0,0];
function rotate(v,r){let a=v.map(Number),angles=r.map(n=>n*Math.PI/180);for(const axis of [2,0,1]){const c=Math.cos(angles[axis]),s=Math.sin(angles[axis]);let[x,y,z]=a;if(axis===0)a=[x,y*c-z*s,y*s+z*c];if(axis===1)a=[x*c+z*s,y,-x*s+z*c];if(axis===2)a=[x*c-y*s,x*s+y*c,z];}return a;}
function project(p){let[x,y,z]=p.map((v,i)=>v-center[i]);let u=x*Math.cos(yaw)-z*Math.sin(yaw),d=x*Math.sin(yaw)+z*Math.cos(yaw);return [canvas.width/2+u*zoom,canvas.height/2-(y*Math.cos(pitch)-d*Math.sin(pitch))*zoom,y*Math.sin(pitch)+d*Math.cos(pitch)];}
const faces=[[0,1,3,2],[4,6,7,5],[0,4,5,1],[2,3,7,6],[0,2,6,4],[1,5,7,3]];
function draw(){let scale=devicePixelRatio||1;canvas.width=canvas.clientWidth*scale;canvas.height=canvas.clientHeight*scale;ctx.clearRect(0,0,canvas.width,canvas.height);let polys=[];
for(const o of objects){let verts=[];for(let x of [-1,1])for(let y of [-1,1])for(let z of [-1,1]){let p=rotate([x*o.size[0]/2,y*o.size[1]/2,z*o.size[2]/2],o.rotation);verts.push(project(p.map((v,i)=>v+o.position[i])));}
for(let i=0;i<faces.length;i++){let pts=faces[i].map(n=>verts[n]);polys.push({pts,z:pts.reduce((s,p)=>s+p[2],0)/4,color:typeof o.color==='string'?o.color:'#5b9bd5',shade:i});}}
polys.sort((a,b)=>b.z-a.z);for(let f of polys){ctx.beginPath();f.pts.forEach((p,i)=>i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1]));ctx.closePath();ctx.fillStyle=f.color;ctx.globalAlpha=.72+f.shade*.045;ctx.fill();ctx.globalAlpha=1;ctx.strokeStyle='#172033';ctx.stroke();}
if(document.querySelector('#names').checked){ctx.font='12px system-ui';ctx.fillStyle='white';for(let o of objects){let p=project(o.position);ctx.fillText(o.name,p[0]+4,p[1]-4);}}
ctx.fillStyle='#94a3b8';ctx.font='12px system-ui';ctx.fillText(objects.length+' pieces; dimensions use the registered template measurements',18,24);}
canvas.addEventListener('pointerdown',e=>{drag=true;last=[e.clientX,e.clientY];canvas.setPointerCapture(e.pointerId);});canvas.addEventListener('pointerup',()=>drag=false);canvas.addEventListener('pointermove',e=>{if(!drag)return;yaw+=(e.clientX-last[0])*.008;pitch=Math.max(-1.45,Math.min(1.45,pitch+(e.clientY-last[1])*.008));last=[e.clientX,e.clientY];draw();});canvas.addEventListener('wheel',e=>{e.preventDefault();zoom=Math.max(.1,Math.min(1000,zoom*Math.exp(-e.deltaY*.001)));draw();},{passive:false});addEventListener('resize',draw);document.querySelector('#names').onchange=draw;draw();
</script></html>'''
    return html.replace("DATA", data, 1).encode("utf-8")
