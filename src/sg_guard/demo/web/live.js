/* Live UI only. All assistant output, tools, scores and edits come from the local API. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  // API offsets use Python Unicode code points, not JavaScript UTF-16 units.
  const textSlice = (text,start,end) => Array.from(text).slice(start,end).join('');
  const textLength = text => Array.from(text).length;
  const finite = x => typeof x === 'number' && Number.isFinite(x);
  const num = x => finite(x) ? x.toFixed(5) : '—';
  const money = x => finite(x) ? x.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}) : '—';
  const humanStatus = {idle:'等待输入',running:'正在运行',error:'运行出错',interrupted:'运行已中断'};
  const S = {health:null,presets:null,session:null,events:[],cursor:-1,source:null,request:null,submitting:false,creating:false,follow:true,selectedReadout:null,readouts:[],textView:'heat',pane:'readout',auditMode:'key',renderTimer:null,notice:null,connectionLost:false,pendingRefresh:false};
  let chartMeta = null, heartbeat = null, auditRevision = -1;
  function notify(message,kind='error') { $('notice').textContent=message; $('notice').hidden=!message; $('notice').dataset.kind=kind; }
  async function api(path,options={}) {
    let response;
    try { response=await fetch(path,{...options,headers:{'Content-Type':'application/json',...(options.headers||{})}}); }
    catch(e) { throw Object.assign(new Error('无法连接本地服务。会话不会自动重复提交。'),{network:true}); }
    const data=await response.json().catch(()=>({detail:response.statusText}));
    if(!response.ok)throw Object.assign(new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail||data)),{status:response.status});
    return data;
  }
  function sessionRunning() { return S.session?.status==='running'||S.submitting; }
  function ready() { return S.health?.status==='ready'; }
  function refreshControls() {
    const busy=sessionRunning(),blocked=S.session&&['error','interrupted'].includes(S.session.status);
    $('send').disabled=!ready()||busy||S.creating||!$('message').value.trim()||!!blocked;
    $('send').innerHTML=busy?'运行中 <span>···</span>':'发送 <span>↗</span>';
    $('new-session').disabled=!ready()||busy||S.creating||!!S.health?.busy;
    $('scenario').disabled=busy||S.creating; $('arm').disabled=busy||S.creating;
    $('saved').disabled=busy||S.creating; $('refresh-saved').disabled=busy;
    $('message').disabled=busy||!!blocked;
    $('composer-state').textContent=blocked?'此会话已停止；保留记录后可新建会话':busy?'正在真实生成与执行工具，请等待本轮完成':!ready()?'等待本地模型就绪':S.session?`第 ${Number(S.session.turns?.length||0)+1} 轮 · 保留此前对话与账本`:'首条消息会创建独立会话';
    $('health').className='health '+(S.health?.status==='error'?'error':S.health?.busy||busy?'running':ready()?'ready':'');
    $('health').querySelector('span').textContent=S.connectionLost?'连接恢复中 · 保留已收记录':S.health?.status==='error'?'模型加载失败':!S.health?'连接本地 Qwen3-4B':!ready()?'Qwen3-4B 正在加载':S.health.busy||busy?'Qwen3-4B 正在计算':'Qwen3-4B 已就绪';
    if(S.session){
      const label=(S.presets?.scenarios||[]).find(x=>x.id===S.session.scenario_id)?.name||S.session.scenario_id;
      $('session-title').textContent=`${S.session.arm==='guard'?'防护':'无防护'} · ${humanStatus[S.session.status]||S.session.status}`;
      $('session-title').title=label;
      $('session-id').textContent=S.session.id;
      $('download').classList.remove('disabled');$('download').setAttribute('aria-disabled','false');$('download').href=`/api/live/sessions/${encodeURIComponent(S.session.id)}/download`;
      const r=S.session.runtime||S.health?.runtime;
      $('run-summary').textContent=`${r?.model||'Qwen3-4B'} · ${S.session.arm==='guard'?'SilentGuard':'无防护对照'} · ${busy?'实时计算中':'保存会话，可继续对话'} · ${S.events.length} 事件`;
    } else {$('session-title').textContent='等待开始';$('session-id').textContent='新会话';}
    $('follow').classList.toggle('active',S.follow);$('follow').textContent=S.follow?'跟随最新 ↓':'回到最新 ↓';
  }
  async function getHealth() {
    try { S.health=await api('/api/health');if(S.health.status==='error')notify(S.health.error||'模型加载失败'); }
    catch(e) { S.health=null; }
    refreshControls();
  }
  function setPrompt(index) {
    const p=S.presets?.suggested_turns?.[index];
    if(!p)return;
    $('message').value=typeof p==='string'?p:p.content;
    document.querySelectorAll('[data-prompt]').forEach(b=>b.classList.toggle('selected',Number(b.dataset.prompt)===index));
    refreshControls();$('message').focus();
  }
  async function refreshSaved() {
    try {
      const data=await api('/api/live/sessions'), rows=Array.isArray(data)?data:(data.sessions||[]);
      $('saved').innerHTML='<option value="">选择运行记录</option>'+rows.map(row=>{
        const date=new Date(row.updated_at||row.created_at); const stamp=Number.isNaN(date.getTime())?'':date.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});
        return `<option value="${esc(row.id)}">${esc(stamp)} · ${row.arm==='guard'?'防护':'无防护'} · ${row.scenario_id==='payment_clean'?'正常':'攻击'} · ${esc(row.id.slice(0,8))}</option>`;
      }).join('');if(S.session)$('saved').value=S.session.id;
    } catch(e) { /* A transient listing failure never replaces the active session. */ }
  }
  function stopStream() { if(S.source){S.source.close();S.source=null;} }
  function eventKey(e) { return `${e.round??0}:${e.step??0}`; }
  function loadEvents(events) {
    const seen=new Map();for(const e of events||[])if(Number.isInteger(e.seq))seen.set(e.seq,e);
    S.events=[...seen.values()].sort((a,b)=>a.seq-b.seq); S.cursor=S.events.length?S.events.at(-1).seq:-1;
  }
  function attachSession(session,{reset=false}={}) {
    S.session=session;loadEvents(session.events||S.events);
    if(reset){S.selectedReadout=null;S.follow=true;S.request=null;S.textView='heat';auditRevision=-2;}
    $('scenario').value=session.scenario_id;$('arm').value=session.arm;
    history.replaceState(null,'',`/live.html?session=${encodeURIComponent(session.id)}`);
    render();
  }
  async function syncSession(id,reset=false) {
    const session=await api(`/api/live/sessions/${encodeURIComponent(id)}`);
    if(S.session&&S.session.id!==id&&!reset)return;
    attachSession(session,{reset});
    if(session.status==='running')connect();
    else {stopStream();S.submitting=false;refreshControls();}
    return session;
  }
  async function createSession() {
    if(S.creating)return null;S.creating=true;refreshControls();notify('');
    try {
      const session=await api('/api/live/sessions',{method:'POST',body:JSON.stringify({scenario_id:$('scenario').value,arm:$('arm').value})});
      stopStream();attachSession(session,{reset:true});await refreshSaved();return session;
    } catch(e){notify(e.message);return null;} finally{S.creating=false;refreshControls();}
  }
  async function send(event) {
    event?.preventDefault();const content=$('message').value.trim();if(!content||sessionRunning()||!ready())return;
    let session=S.session;
    if(!session){session=await createSession();if(!session)return;}
    if(session.scenario_id!==$('scenario').value||session.arm!==$('arm').value){notify('场景或策略已变更。请先新建会话，当前对话和账本保持原设置。');return;}
    if(!S.request||S.request.content!==content||S.request.session!==session.id)S.request={content,session:session.id,id:crypto.randomUUID?crypto.randomUUID():`${Date.now()}-${Math.random().toString(16).slice(2)}`};
    const request=S.request;S.submitting=true;S.follow=true;notify('');refreshControls();
    try {
      await api(`/api/live/sessions/${encodeURIComponent(session.id)}/turns`,{method:'POST',body:JSON.stringify({content,request_id:request.id})});
      S.session.status='running';S.request=null;$('message').value='';document.querySelectorAll('[data-prompt]').forEach(b=>b.classList.remove('selected'));
      connect();
    } catch(e) {
      notify(e.message);
      if(e.network){try{await syncSession(session.id);}catch{} }
    } finally{S.submitting=false;refreshControls();}
  }
  function connect() {
    if(!S.session)return; stopStream();
    const ident=S.session.id, source=new EventSource(`/api/live/sessions/${encodeURIComponent(ident)}/events?after=${S.cursor}`);S.source=source;
    source.onopen=()=>{S.connectionLost=false;refreshControls();};
    source.onmessage=message=>{
      if(S.session?.id!==ident)return;
      let e;try{e=JSON.parse(message.data);}catch{return;}
      if(!Number.isInteger(e.seq)||e.seq<=S.cursor)return;
      S.events.push(e);S.cursor=e.seq;
      if(e.ledger)S.session.ledger=e.ledger;
      if(e.result){S.session.result=e.result;if(e.result.balance!==undefined)S.session.balance=e.result.balance;}
      if(e.kind==='user'||e.kind==='thinking')S.session.status='running';
      if(e.kind==='error'){S.session.status='error';notify(e.message||e.error||'本轮运行失败，记录已保留。');}
      if(e.kind==='turn_done'){S.session.status=e.status||'idle';}
      scheduleRender();
      if(e.kind==='turn_done'||e.kind==='error'){
        stopStream();S.submitting=false;syncSession(ident).then(refreshSaved).catch(err=>notify(err.message));
      }
    };
    source.onerror=()=>{
      if(S.source!==source)return;source.close();S.source=null;S.connectionLost=true;refreshControls();
      setTimeout(async()=>{if(S.session?.id!==ident)return;try{await syncSession(ident);S.connectionLost=false;refreshControls();}catch{notify('连接已断开。刷新页面可恢复保存记录；不会自动重新执行付款。');}},1000);
    };
  }
  function scheduleRender() { if(!S.renderTimer)S.renderTimer=setTimeout(()=>{S.renderTimer=null;render();},65); }
  function preserveDetails(container,html) {
    const opened=new Set([...container.querySelectorAll('details[open][data-key]')].map(x=>x.dataset.key));
    const scroll=container.scrollTop;container.innerHTML=html;
    for(const d of container.querySelectorAll('details[data-key]'))if(opened.has(d.dataset.key))d.open=true;
    container.scrollTop=scroll;
  }
  function cleanText(text) {return String(text??'').replace(/<\|(?:im_end|endoftext)\|>$/g,'').trimEnd();}
  function timelineRows() {
    const rows=[],gen=new Map(),calls=new Map();
    const entry=(key,cls,role,tag,body,round)=>`<article class="entry ${cls}" data-entry="${esc(key)}"><div class="entry-head"><b>${esc(role)}</b><span>${esc(tag)}</span><span class="round">${round?'ROUND '+round:''}</span></div>${body}</article>`;
    for(const e of S.events){
      const key=eventKey(e);
      if(e.kind==='user')rows.push({html:entry(`user-${e.seq}`,'user','you','用户指令',`<pre class="entry-body">${esc(e.content||e.text)}</pre>`,e.round)});
      if(e.kind==='thinking'){
        if(!gen.has(key)){const row={key,kind:'generation',text:'',done:false,round:e.round};gen.set(key,row);rows.push(row);}
      }
      if(e.kind==='generation_delta'||e.kind==='generation'){
        let row=gen.get(key);if(!row){row={key,kind:'generation',text:'',round:e.round};gen.set(key,row);rows.push(row);}
        row.text=e.text??row.text;row.done=e.kind==='generation';row.call=e.call??row.call;row.tokens=e.generated_tokens??row.tokens;row.seconds=e.seconds??row.seconds;
      }
      if(e.kind==='tool_call')calls.set(key,e.call);
      if(e.kind==='tool_result'){
        calls.set(key,e.call);const call=e.call||calls.get(key);const fn=call?.function||'tool';
        rows.push({html:entry(`tool-${e.seq}`,'tool','tool',`${fn} 返回`, `<details data-key="return-${e.seq}"><summary>${esc(fn)} · ${String(e.text||'').length} 字符 · 查看原文</summary><pre>${esc(e.text)}</pre></details>`,e.round)});
      }
      if(e.kind==='readout'){
        const d=e.data,alert=d?.decision?.alert;rows.push({html:`<div class="trace-line ${alert?'alert':'ok'}"><span>${alert?'↗':'✓'}</span><span class="trace-main">${alert?'窗口超过阈值':'本条返回未触发'}</span><span class="mono">${num(d?.decision?.score)} / ${num(d?.decision?.threshold)}</span><button data-inspect="${esc(key)}">查看读出 ↗</button></div>`});
      }
      if(e.kind==='edit'){
        const deleted=(e.original||'').length-(e.filtered||'').length;
        rows.push({html:`<div class="trace-line ${deleted?'alert':''}"><span>↳</span><span class="trace-main">${deleted?`实际删除 ${deleted} 字符`:'原文完整保留'}</span><span class="mono">${(e.receipt?.edits||[]).map(x=>'['+x.original_char_span.join(', ')+')').join(' · ')}</span><button data-inspect="${esc(key)}" data-text-view="edit">查看删改 ↗</button></div>`});
      }
      if(e.kind==='turn_done')rows.push({html:`<div class="turn-end"><b>第 ${esc(e.round)} 轮结束</b><span>${finite(e.seconds)?e.seconds.toFixed(2)+' s · ':''}${esc(e.stop_reason||'')}</span><span>${(e.ledger||e.result?.ledger||[]).length} 笔模拟付款</span></div>`});
      if(e.kind==='error')rows.push({html:`<div class="trace-line error"><span>!</span><span>${esc(e.message||e.error||'本轮运行失败')}</span></div>`});
    }
    return rows.map(row=>{
      if(row.html)return row.html;
      const cls=row.call?'call':'assistant';const tag=row.call?'模型生成的原生工具调用':row.done?'模型实际回复':'正在流式生成';
      const text=row.done?cleanText(row.text):row.text;
      const body=text?`<pre class="entry-body">${esc(text)}${!row.done?'<span class="stream-cursor">▍</span>':''}</pre>`:'<div class="thinking-dots muted">模型正在计算</div>';
      return entry(`generation-${row.key}`,cls,row.call?'assistant → tool':'assistant',tag,body,row.round);
    }).join('');
  }
  function renderTimeline() {
    if(!S.session)return;
    const target=$('timeline'),scroll=target.scrollTop,html=timelineRows();
    preserveDetails(target,html||'<div class="welcome"><h2>会话已建立</h2><p>输入任务后，模型会真实生成回复和工具调用。</p></div>');
    if(S.follow)target.scrollTop=target.scrollHeight;else target.scrollTop=scroll;
    for(const b of target.querySelectorAll('[data-inspect]'))b.addEventListener('click',()=>{S.selectedReadout=b.dataset.inspect;if(b.dataset.textView)S.textView=b.dataset.textView;setPane('readout');renderReadout();});
  }
  function deriveReadouts() {
    const map=new Map(),functions=new Map();
    for(const e of S.events){
      const key=eventKey(e);
      if(e.kind==='tool_result'||e.kind==='tool_call')functions.set(key,e.call?.function||'tool');
      if(!['readout_start','readout_layer','readout_progress','readout'].includes(e.kind))continue;
      if(!map.has(key))map.set(key,{key,round:e.round,step:e.step,tokens:[],complete:false,layer:0,completed:0,original:'',seq:e.seq});
      const r=map.get(key);
      if(e.kind==='readout_start'){r.original=e.original;r.tokens=(e.tokens||[]).map(t=>({...t}));}
      if(e.kind==='readout_layer')r.layer=e.layer;
      if(e.kind==='readout_progress'){
        r.layer=e.layer;r.completed=e.completed;r.total=e.total;
        for(const t of e.tokens||[])r.tokens[t.index]={...t};
      }
      if(e.kind==='readout'){Object.assign(r,e.data,{complete:true,applied:e.applied??S.session?.arm==='guard'});r.layer=35;r.completed=r.tokens.length;r.total=r.tokens.length;}
    }
    for(const r of map.values())r.function=functions.get(r.key)||'tool';
    S.readouts=[...map.values()];if(!S.selectedReadout&&S.readouts.length)S.selectedReadout=S.readouts[0].key;
  }
  function partialWindows(r,width) {
    const q=Array.from({length:r.tokens.length},(_,i)=>r.tokens[i]?.score),values=[];
    for(let i=0;i+width<=q.length;i++){const slice=q.slice(i,i+width);values.push(slice.every(finite)?slice.reduce((a,b)=>a+b,0)/width:null);}
    return values;
  }
  function chart(r,threshold) {
    const width=r.decision?.effective_width||S.session?.runtime?.width||16;
    const values=r.decision?.window_scores||partialWindows(r,width);
    const valid=values.filter(finite);
    if(!valid.length){chartMeta=null;return '<div class="plot-empty"><span>16-token 窗口 · 35 层加权</span><p>正在读取完整层响应。<br>分数就绪后绘制真实窗口曲线。</p></div>';}
    let low=Math.min(0,...valid,threshold),high=Math.max(0,...valid,threshold);if(high-low<.005){low-=.0025;high+=.0025;}const pad=(high-low)*.16;low-=pad;high+=pad;
    const W=600,H=205,L=51,R=12,T=17,B=34,x=i=>L+i/Math.max(1,values.length-1)*(W-L-R),y=v=>T+(high-v)/(high-low)*(H-T-B);
    let path='',last=false;values.forEach((v,i)=>{if(finite(v)){path+=(last?'L':'M')+x(i).toFixed(2)+','+y(v).toFixed(2)+' ';last=true;}else last=false;});
    const ticks=Array.from({length:4},(_,i)=>low+(high-low)*(i+.35)/4);
    const shade=values.map((v,i)=>finite(v)&&v>threshold?`<rect x="${x(i)-1}" y="${T}" width="${Math.max(2,(W-L-R)/values.length)}" height="${H-T-B}" fill="#bb5c3b13"/>`:'').join('');
    chartMeta={r,values,width,W,H,L,R,T,B,x,y};
    return `<svg id="score-plot" viewBox="0 0 ${W} ${H}" role="img" aria-label="本条工具返回的窗口分数，冻结阈值 ${num(threshold)}"><rect x="${L}" y="${T}" width="${W-L-R}" height="${H-T-B}" fill="#fcfbf7"/>${ticks.map(v=>`<line x1="${L}" x2="${W-R}" y1="${y(v)}" y2="${y(v)}" stroke="#e5e2d9"/><text x="${L-8}" y="${y(v)+4}" text-anchor="end">${v.toFixed(3)}</text>`).join('')}${shade}<line x1="${L}" x2="${W-R}" y1="${y(threshold)}" y2="${y(threshold)}" stroke="#a38465" stroke-dasharray="5 4"/><text x="${W-R}" y="${y(threshold)-6}" text-anchor="end" class="threshold-label">阈值 ${num(threshold)}</text><path d="${path}" fill="none" stroke="#bb5c3b" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/><g class="hover-mark" id="plot-hover" visibility="hidden"><line x1="0" x2="0" y1="${T}" y2="${H-B}" stroke="#8e8577" stroke-dasharray="2 3"/><circle cx="0" cy="0" r="4" fill="#bb5c3b"/></g><text x="${L}" y="${H-10}">1</text><text x="${W-R}" y="${H-10}" text-anchor="end">${values.length} 个窗口</text></svg>`;
  }
  function highlightWindow(event) {
    const m=chartMeta;if(!m)return;const svg=$('score-plot'),rect=svg.getBoundingClientRect(),px=(event.clientX-rect.left)/rect.width*m.W;
    const i=Math.max(0,Math.min(m.values.length-1,Math.round((px-m.L)/(m.W-m.L-m.R)*Math.max(1,m.values.length-1))));
    if(!finite(m.values[i]))return;
    const tokens=m.r.tokens.slice(i,i+m.width),start=tokens[0]?.start,end=tokens.at(-1)?.end;
    $('chart-detail').textContent=`窗口 ${i+1} · ${num(m.values[i])} · ${textSlice(m.r.original,start,end)}`;
    $('chart-detail').title=$('chart-detail').textContent;
    const h=$('plot-hover');if(h){h.setAttribute('visibility','visible');const l=h.querySelector('line'),c=h.querySelector('circle');l.setAttribute('x1',m.x(i));l.setAttribute('x2',m.x(i));c.setAttribute('cx',m.x(i));c.setAttribute('cy',m.y(m.values[i]));}
  }
  function heatText(r) {
    const scores=r.tokens.map(t=>Math.abs(t?.score||0)),scale=Math.max(1e-8,...scores);
    const chars=Array.from(r.original);let cursor=0,html='';
    for(const token of r.tokens){if(!token)continue;const start=Math.max(cursor,token.start),end=Math.min(chars.length,token.end);if(end<=start)continue;
      if(start>cursor)html+=esc(chars.slice(cursor,start).join(''));
      const q=token.score,alpha=finite(q)?Math.asinh(Math.abs(q)/scale*6)/Math.asinh(6)*.57:0;
      const color=q<0?`rgba(116,148,157,${alpha.toFixed(3)})`:`rgba(201,113,65,${alpha.toFixed(3)})`;
      html+=`<span class="tok" data-token="${token.index}" tabindex="0" style="background:${color}" title="token ${token.index+1} · ${num(q)} · [${token.start}, ${token.end})">${esc(chars.slice(start,end).join(''))}</span>`;cursor=end;
    }
    return html+esc(chars.slice(cursor).join(''));
  }
  function editText(r) {
    const spans=r.decision?.spans||[],chars=Array.from(r.original);let html='',cursor=0;
    for(const span of spans){html+=esc(chars.slice(cursor,span.start).join(''))+`<del>${esc(chars.slice(span.start,span.end).join(''))}</del>`;cursor=span.end;}
    return html+esc(chars.slice(cursor).join(''));
  }
  function renderReadout() {
    deriveReadouts(); const options=S.readouts.map((r,i)=>`<option value="${esc(r.key)}">${i+1}. round ${r.round} / ${esc(r.function)}${r.complete?' · '+num(r.decision?.score):' · 读取中'}</option>`).join('');
    $('readout-select').innerHTML=options||'<option>尚无读出记录</option>';$('readout-select').disabled=!S.readouts.length;
    const r=S.readouts.find(r=>r.key===S.selectedReadout)||S.readouts[0];
    const runtime=S.session?.runtime||S.health?.runtime; const threshold=r?.decision?.threshold??runtime?.threshold;
    $('threshold').textContent=num(threshold);
    if(!r){
      chartMeta=null;$('ledger-count').textContent=(S.session?.ledger||[]).length;$('readout-status').textContent='等待工具返回';$('readout-status').className='status-label';$('progress').hidden=true;$('score').textContent='—';$('deleted').textContent='—';
      $('chart').innerHTML='<div class="plot-empty"><span>16-token 窗口 · 35 层加权</span><p>完整工具返回到达后，展示真实分数。<br>虚线表示冻结的处理阈值。</p></div>';
      $('chart-detail').textContent='移动到曲线上，可检查窗口原文。';$('readout-text').textContent='工具返回、逐 token 分数与文本编辑将在这里显示。';$('token-detail').textContent='点击词元，查看字符区间与分数。';$('readout-foot').textContent='尚无本会话读出记录。';return;
    }
    S.selectedReadout=r.key;$('readout-select').value=r.key;
    const alert=r.decision?.alert,baseline=S.session?.arm==='baseline';
    $('readout-status').textContent=r.complete?(baseline?'仅观察 · 不处理':alert?'已触发处理':'未触发'):'正在读取';
    $('readout-status').className='status-label '+(r.complete?(alert?'alert':'pass'):'');
    $('progress').hidden=r.complete;$('progress-label').textContent=`模型层 ${r.layer||0} / 35`;$('progress-count').textContent=`${r.completed||0} / ${r.tokens.length||r.total||0} tokens`;
    $('progress-bar').style.width=`${Math.max(r.layer/35*50,(r.completed||0)/Math.max(1,r.tokens.length)*100)}%`;
    $('score').textContent=r.complete?num(r.decision.score):'读取中';
    $('edit-caption').textContent=baseline?'实际删除':'实际删除';
    const removed=r.complete?(baseline?0:textLength(r.original)-textLength(r.filtered)):null;
    $('deleted').textContent=removed===null?'—':`${removed} 字符`;
    $('chart').innerHTML=chart(r,threshold||0);const svg=$('score-plot');if(svg){svg.addEventListener('mousemove',highlightWindow);svg.addEventListener('mouseleave',()=>{const h=$('plot-hover');if(h)h.setAttribute('visibility','hidden');});}
    $('chart-detail').textContent=r.complete?`窗口 ${r.decision.effective_width} tokens · stride 1 · 横轴为窗口起点`:'各词元的 35 层响应齐备后，才计入分数';
    document.querySelectorAll('[data-view]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.view===S.textView)));
    const text=$('readout-text'),scroll=text.scrollTop;
    if(S.textView==='heat')text.innerHTML=heatText(r);
    else if(S.textView==='edit')text.innerHTML=baseline?esc(r.original):editText(r);
    else text.textContent=r.complete?(baseline?r.original:r.filtered)||'（实际模型输入为空）':'等待实际处理结果';
    text.scrollTop=scroll;
    $('text-meta').innerHTML=`<span>${textLength(r.original)} 字符 · ${r.tokens.length} tokens${baseline?' · 无防护，不应用编辑':''}</span>${S.textView==='heat'?'<span class="heat-legend"><i></i>负分　0　正分</span>':'<span>'+ (S.textView==='edit'?'划线为实际删除范围':'原样送回 Agent')+'</span>'}`;
    $('token-detail').textContent=S.textView==='heat'?'点击词元，查看原始分数；色阶仅用于展示。':S.textView==='edit'?(r.complete?`删除字符区间：${baseline?'无':(r.decision.spans||[]).map(s=>`[${s.start}, ${s.end})`).join(' · ')||'无'}`:'等待最终编辑区间'):'模型继续推理时使用的完整工具文本。';
    for(const tok of text.querySelectorAll('[data-token]')){
      const show=()=>{const t=r.tokens.find(t=>t?.index===Number(tok.dataset.token));if(!t)return;$('token-detail').textContent=`token ${t.index+1} · 分数 ${num(t.score)} · 字符 [${t.start}, ${t.end})`;for(const n of text.querySelectorAll('.tok.focus'))n.classList.remove('focus');tok.classList.add('focus');};
      tok.addEventListener('click',show);tok.addEventListener('focus',show);
    }
    const count=S.readouts.length, latest=S.readouts.at(-1).key!==r.key;
    $('readout-foot').innerHTML=`${r.complete?`本条返回${baseline?'仅计算旁路分数':'按冻结阈值处理'}。${finite(r.seconds)?`读出耗时 ${r.seconds.toFixed(2)} s。`:''}`:'显示实际计算进度，不预填最终结果。'}${count>1?`<br>已记录 ${count} 条工具返回${latest?'；上方选择器可检查后续付款回执及其处理':''}。`:''}`;
  }
  function renderLedger() {
    const ledger=S.session?.ledger||S.session?.result?.ledger||[],result=S.session?.result||{};
    $('ledger-count').textContent=ledger.length;
    const total=ledger.reduce((sum,r)=>sum+(Number(r.amount)||0),0);
    let html=`<div class="ledger-headline"><strong>${money(total)}</strong><small>CNY</small><p>${ledger.length} 笔已执行付款 · 本会话独立模拟账本</p></div>`;
    if(!ledger.length)html+='<div class="empty-state">尚未执行付款。<br>查询与用户确认之后，模型生成的付款调用会登记在这里。</div>';
    for(const r of ledger)html+=`<article class="transaction"><div class="transaction-title"><strong>${esc(r.transaction_id)}</strong><span>${esc(r.status)}</span></div><dl><dt>收款账户</dt><dd>${esc(r.recipient_account)}</dd><dt>实际金额</dt><dd>${money(r.amount)} ${esc(r.currency)}</dd><dt>付款备注</dt><dd>${esc(r.reference)}</dd></dl></article>`;
    if(result.safe_completion===true)html+='<div class="ledger-verdict">预置任务验收通过：原发票付款完成，且没有额外或确认前付款。<span>依据真实工具账本和用户确认轮次判定。</span></div>';
    else if(result.safe_completion===false&&ledger.length)html+='<div class="ledger-verdict warn">当前记录未通过预置任务的安全完成核对。<span>检查付款对象、金额、重复付款与用户确认顺序。</span></div>';
    else html+='<div class="ledger-verdict">按实际调用记录核对账本。<span>自由输入的业务意图由使用者确认，不自动赋予预置任务的验收结论。</span></div>';
    if(finite(S.session?.balance??result.balance))html+=`<p class="audit-note">模拟账户余额：${money(S.session?.balance??result.balance)} CNY</p>`;
    $('ledger').innerHTML=html;
  }
  function renderAudit() {
    const keys=new Set(['user','generation','tool_call','tool_result','readout','edit','turn_done','error','session_start','session_created']);
    const events=S.auditMode==='all'?S.events:S.events.filter(e=>keys.has(e.kind));
    $('event-count').textContent=`${events.length} / ${S.events.length} 条`;
    if(S.pane!=='audit'||auditRevision===`${S.cursor}:${S.auditMode}`)return;
    auditRevision=`${S.cursor}:${S.auditMode}`;
    const html=events.length?events.map(e=>`<details class="audit-event" data-key="audit-${e.seq}"><summary><span>#${e.seq}</span><b>${esc(e.kind)}</b><span>${e.round?'R'+e.round:''}</span><span class="evt-time">${finite(e.elapsed)?e.elapsed.toFixed(2)+' s':''}</span></summary><pre>${esc(JSON.stringify(e,null,2))}</pre></details>`).join(''):'<p class="empty-state">开始会话后，事件按真实顺序出现。</p>';
    preserveDetails($('audit'),html);
  }
  function setPane(pane) {S.pane=pane;for(const b of document.querySelectorAll('[data-pane]'))b.setAttribute('aria-selected',String(b.dataset.pane===pane));for(const p of ['readout','ledger','audit'])$('pane-'+p).hidden=p!==pane;renderAudit();}
  function render() {refreshControls();renderTimeline();renderReadout();renderLedger();renderAudit();}
  $('composer').addEventListener('submit',send);$('message').addEventListener('input',()=>{document.querySelectorAll('[data-prompt]').forEach(b=>b.classList.remove('selected'));refreshControls();});
  $('message').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();if(!$('send').disabled)send();}});
  $('new-session').addEventListener('click',createSession);
  for(const b of document.querySelectorAll('[data-prompt]'))b.addEventListener('click',()=>setPrompt(Number(b.dataset.prompt)));
  for(const b of document.querySelectorAll('[data-pane]'))b.addEventListener('click',()=>setPane(b.dataset.pane));
  for(const b of document.querySelectorAll('[data-audit]'))b.addEventListener('click',()=>{S.auditMode=b.dataset.audit;for(const button of document.querySelectorAll('[data-audit]'))button.setAttribute('aria-pressed',String(button===b));renderAudit();});
  for(const b of document.querySelectorAll('[data-view]'))b.addEventListener('click',()=>{S.textView=b.dataset.view;renderReadout();});
  $('readout-select').addEventListener('change',()=>{S.selectedReadout=$('readout-select').value;renderReadout();});
  $('latest-return').addEventListener('click',()=>{if(S.readouts.length){S.selectedReadout=S.readouts.at(-1).key;renderReadout();}});
  $('timeline').addEventListener('wheel',()=>{S.follow=false;refreshControls();},{passive:true});
  $('timeline').addEventListener('touchmove',()=>{S.follow=false;refreshControls();},{passive:true});
  $('follow').addEventListener('click',()=>{S.follow=true;$('timeline').scrollTop=$('timeline').scrollHeight;refreshControls();});
  $('saved').addEventListener('change',async()=>{const id=$('saved').value;if(!id)return;stopStream();notify('');try{await syncSession(id,true);}catch(e){notify(e.message);}});
  $('refresh-saved').addEventListener('click',refreshSaved);
  for(const id of ['scenario','arm'])$(id).addEventListener('change',()=>{if(S.session&&($(id).value!==(id==='arm'?S.session.arm:S.session.scenario_id)))notify('新设置将在“新建会话”后生效；现有对话与账本保持原设置。','info');else notify('');});
  $('config-open').addEventListener('click',()=>{$('config-content').textContent=JSON.stringify({session_id:S.session?.id,arm:S.session?.arm,scenario_id:S.session?.scenario_id,runtime:S.session?.runtime||S.health?.runtime,limits:S.presets?.limits},null,2);$('config').showModal();});
  $('config-close').addEventListener('click',()=>$('config').close());
  $('config').addEventListener('click',e=>{if(e.target===$('config'))$('config').close();});
  window.addEventListener('beforeunload',stopStream);
  // Read-only diagnostics for integration and screenshot verification.
  window.SilentGuardLive={get state(){return {sessionId:S.session?.id,status:S.session?.status,eventCount:S.events.length,lastSeq:S.cursor,pane:S.pane,readoutKey:S.selectedReadout,readoutCount:S.readouts.length,ledger:S.session?.ledger||[],result:S.session?.result||{},connectionLost:S.connectionLost};},inspectReturn(key){S.selectedReadout=key;setPane('readout');renderReadout();},showPane:setPane};
  async function init(){
    await getHealth();
    try{S.presets=await api('/api/live/presets');if(S.presets.scenarios?.length)$('scenario').innerHTML=S.presets.scenarios.map(s=>`<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('');}
    catch(e){notify('实时多轮接口尚未就绪。请确认本地服务已更新并启动。');}
    const id=new URLSearchParams(location.search).get('session');
    if(id){try{await syncSession(id,true);}catch(e){notify(e.message);}}
    else setPrompt(0);
    await refreshSaved();render();heartbeat=setInterval(getHealth,5000);
  }
  init();
})();
