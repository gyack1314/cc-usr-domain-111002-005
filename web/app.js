const $=(s,root=document)=>root.querySelector(s);
const $$=(s,root=document)=>[...root.querySelectorAll(s)];
const data=await fetch('/data.json').then(r=>r.json());
const runtime=()=>fetch('/api/runtime').then(r=>r.json());
const action=(name,fields={})=>fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,...fields})}).then(r=>r.json());
const tick=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
const html=(value)=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const show=(s)=>{$('#status').textContent=s;};

let req=0;
$('#region').innerHTML+=data.regions.map(r=>`<option value="${r.id}">${r.label}</option>`).join('');
async function kinds(){const parent=$('#region').value;$('#kind').disabled=true;$('#kind').dataset.loading='true';$('#project').innerHTML='';$('#project').disabled=true;await tick();let state=await runtime();if(state.phase==='out-of-order'&&parent==='r-n'){while(state.phase==='out-of-order'&&state.version===1){await tick();state=await runtime();}}if(state.phase==='failed-options'){$('#kind').dataset.loading='error';show('类型加载失败');return;}$('#kind').innerHTML='<option value="">请选择</option>'+(data.kinds[parent]||[]).map(r=>`<option value="${r.id}" data-parent="${parent}">${r.label}</option>`).join('');$('#kind').dataset.parent=parent;$('#kind').dataset.loading='false';$('#kind').disabled=false;show('类型已加载');}
async function projects(){const parent=$('#kind').value;$('#project').disabled=true;$('#project').dataset.loading='true';await tick();$('#project').innerHTML='<option value="">请选择</option>'+(data.projects[parent]||[]).map(r=>`<option value="${r.id}" data-parent="${parent}">${r.label}</option>`).join('');$('#project').dataset.parent=parent;$('#project').dataset.loading='false';$('#project').disabled=false;show('项目已加载');}
$('#region').onchange=kinds;$('#kind').onchange=projects;
$('#query').onclick=async()=>{const sig=[$('#region').value,$('#kind').value,$('#project').value].join('|');const id='Q-'+(++req);$('#signature').textContent=sig;$('#signature').dataset.requestId=id;$('#results').setAttribute('aria-busy','true');await tick();$('#results').dataset.requestId=id;$('#results').dataset.signature=sig;$('#results').innerHTML=$('#project').value?`<p data-id="SAME-17" data-version="1">${sig} 检测资料</p>`:'<p data-empty="true">没有资料</p>';$('#results').setAttribute('aria-busy','false');await action('query',{signature:sig,request_id:id});show('查询完成');};show('请选择区域');
