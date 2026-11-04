'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const state = {session: null, busy: false, controller: null, messageCount: 0, offset: 0, total: 0, sources: new Map(), editing: null, status: null, token: sessionStorage.getItem('accessToken') || ''};
const historyState = {items: [], query: '', request: 0, opening: false, deleting: null, pending: false};
const el = (tag, className, text) => { const n = document.createElement(tag); if (className) n.className = className; if (text !== undefined) n.textContent = text; return n; };
let toastTimer;
function toast(text) { $('#toast').textContent = text; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').hidden = true, 5000); }
async function api(path, options = {}) {
  let response;
  try {response = await fetch(path, {...options, headers: {'Content-Type': 'application/json', ...(state.token ? {Authorization: 'Bearer ' + state.token} : {}), ...options.headers}});}
  catch (error) {if (error.name === 'AbortError') throw error; throw new Error('连接服务失败，请确认本机服务已启动，然后重试。');}
  if (!response.ok) { let detail; try { detail = (await response.json()).detail; } catch {} if (response.status === 401) $('#auth-dialog').showModal(); throw new Error(typeof detail === 'string' ? detail : '请求失败，请重试'); }
  return response;
}
async function json(path, options) { return (await api(path, options)).json(); }
function view(name) {
  $('#chat-view').hidden = name !== 'chat'; $('#library-view').hidden = name !== 'library';
  $('#nav-chat').classList.toggle('active', name === 'chat'); $('#nav-library').classList.toggle('active', name === 'library');
  $('#page-label').replaceChildren(document.createTextNode(name === 'chat' ? '知识问答 ' : '我的知识库 '), el('span', '', name === 'chat' ? '/ 有来源的回答' : '/ 持续积累与修订'));
  if (name === 'library') loadLibrary().catch(e => toast(e.message));
}
async function refreshStatus() {
  const status = await json('/api/status'); state.status = status;
  $('#stat-count').textContent = status.knowledge.count; $('#nav-count').textContent = status.knowledge.count;
  $('#stat-weeks').textContent = status.knowledge.dates.filter(x => x.date >= '2026-07-12' && x.date <= '2026-11-01').length;
  const select = $('#category'), value = select.value;
  select.replaceChildren(new Option('全部类别', ''));
  status.knowledge.categories.forEach(c => select.add(new Option(c.category + ' · ' + c.count, c.category)));
  select.value = value;
  if (!status.configured) toast('请在服务端配置 DeepSeek 密钥后使用问答；知识库可正常浏览和编辑。');
}
async function sessions() {
  const request = ++historyState.request;
  const items = await json('/api/sessions');
  if (request !== historyState.request) return;
  historyState.items = items;
  renderSessions();
}
function renderSessions() {
  const query = historyState.query.trim().toLowerCase();
  const items = historyState.items.filter(session => session.title.toLowerCase().includes(query));
  for (const selector of ['#session-list', '#mobile-session-list']) {
    const list = $(selector);
    list.replaceChildren();
    if (!items.length) list.append(el('p', 'history-empty', query ? '没有找到匹配的对话。' : '暂无对话。发出第一个问题后，对话会出现在这里。'));
    for (const session of items) {
      const row = el('div', 'session-row');
      const selected = session.id === state.session;
      const open = el('button', 'session' + (selected ? ' selected' : ''), session.title);
      open.title = session.title;
      if (selected) open.setAttribute('aria-current', 'true');
      open.onclick = () => {
        $('#history-dialog').close();
        openSession(session.id).catch(e => toast(e.message));
      };
      const remove = el('button', 'session-delete', '删除');
      remove.setAttribute('aria-label', '删除对话 ' + session.title);
      remove.onclick = () => requestDelete(session);
      row.append(open, remove);
      list.append(row);
    }
  }
}
function showSession(id, messages) {
  state.session = id;
  state.messageCount = messages.length;
  if (id) localStorage.setItem('lastSession', id);
  else localStorage.removeItem('lastSession');
  state.sources.clear();
  $('#conversation').replaceChildren();
  $('#welcome').hidden = messages.length > 0;
  messages.forEach(m => renderMessage(m.role, m.content, m.refs, m.steps));
  view('chat');
  renderSessions();
}
async function openSession(id) {
  if (state.busy) return toast('请等待当前回答完成');
  if (historyState.opening) return;
  historyState.opening = true;
  try {
    showSession(id, await json('/api/sessions/' + id + '/messages'));
  } finally {
    historyState.opening = false;
  }
}
function newChat() {
  if (state.busy) return toast('请等待当前回答完成');
  if (historyState.opening) return toast('正在打开对话，请稍候');
  showSession(null, []);
  $('#question').focus({preventScroll: true});
}
function requestDelete(session) {
  historyState.deleting = session;
  $('#delete-name').textContent = session.title;
  $('#delete-error').textContent = '';
  $('#delete-dialog').showModal();
}
async function confirmDelete() {
  if (historyState.pending) return;
  if (historyState.opening) {
    $('#delete-error').textContent = '正在打开对话，请稍候再试。';
    return;
  }
  const {id} = historyState.deleting;
  if (state.busy && state.session === id) {
    $('#delete-error').textContent = '这段对话正在回答，请先停止生成或等回答完成后再删除。';
    return;
  }
  const confirm = $('#confirm-delete'), cancel = $('#cancel-delete');
  historyState.pending = confirm.disabled = cancel.disabled = true;
  confirm.textContent = '正在删除…';
  $('#delete-error').textContent = '';
  try {
    await api('/api/sessions/' + id, {method: 'DELETE'});
    historyState.request++;
    historyState.items = historyState.items.filter(session => session.id !== id);
    const wasCurrent = state.session === id;
    if (wasCurrent) showSession(null, []);
    else renderSessions();

    let notice = '已删除对话';
    try {
      await sessions();
      if (wasCurrent && historyState.items.length) await openSession(historyState.items[0].id);
    } catch {
      notice = '已删除对话。其余对话暂未加载，请稍后重试。';
    }
    $('#delete-dialog').close();
    if (wasCurrent) {
      $('#history-dialog').close();
      $('#question').focus({preventScroll: true});
    } else {
      const focusTarget = $('#history-dialog').open ? '#history-dialog .close-dialog' : '#history-search';
      $(focusTarget).focus({preventScroll: true});
    }
    if ($('#history-dialog').open) $('#history-status').textContent = notice;
    else toast(notice);
  } catch (e) {
    $('#delete-error').textContent = e.message;
  } finally {
    historyState.pending = confirm.disabled = cancel.disabled = false;
    confirm.textContent = '确认删除';
    if ($('#delete-dialog').open) confirm.focus();
  }
}
// Render text and citation controls with DOM APIs; never interpret model or imported HTML.
function inline(parent, text) {
  const parts = text.split(/(\[(?:K\d{4}|U[a-f0-9]{12})\]|\*\*[^*]+\*\*|`[^`]+`)/g);
  parts.forEach(part => { if (/^\[(K\d{4}|U[a-f0-9]{12})\]$/.test(part)) {const id = part.slice(1,-1), source = state.sources.get(id), b = el('button','citation',source?.name || id); b.title = id + (source ? ' · ' + source.date : ''); b.onclick = () => detail(id); parent.append(b);} else if (part.startsWith('**') && part.endsWith('**')) parent.append(el('strong','',part.slice(2,-2))); else if (part.startsWith('`') && part.endsWith('`')) parent.append(el('code','',part.slice(1,-1))); else parent.append(document.createTextNode(part)); });
}
function answerText(content) {
  const wrap = el('div', 'message-text');
  const lines = content.split('\n');
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]; if (!line.trim()) continue;
    if (line.trim().startsWith('```')) {
      const code = []; while (++i < lines.length && !lines[i].trim().startsWith('```')) code.push(lines[i]);
      const pre = el('pre'); pre.append(el('code','',code.join('\n'))); wrap.append(pre); continue;
    }
    if (/^\s*(?:---+|\*\*\*+)\s*$/.test(line)) {wrap.append(el('hr')); continue;}
    const list = /^\s*(?:([-*])\s+|(\d+)[.)、]\s*)(.+)$/.exec(line);
    if (list) {const tag = list[2] ? 'ol' : 'ul'; let group = wrap.lastElementChild; if (group?.tagName.toLowerCase() !== tag) {group = el(tag); if (list[2]) group.start = Number(list[2]); wrap.append(group);} const li = el('li'); inline(li,list[3]); group.append(li); continue;}
    if (line.trim().startsWith('|') && /^\s*\|?[\s:|\-]+\|\s*$/.test(lines[i+1] || '')) {
      const scroll = el('div','table-scroll'), table = el('table');
      const cells = text => text.trim().replace(/^\|/,'').replace(/\|$/,'').split('|');
      const head = el('tr'); cells(line).forEach(c => {const th = el('th'); inline(th,c.trim()); head.append(th);}); table.append(head); i += 2;
      while (i < lines.length && lines[i].trim().startsWith('|')) {const row = el('tr'); cells(lines[i]).forEach(c => {const td = el('td'); inline(td,c.trim()); row.append(td);}); table.append(row); i++;}
      i--; scroll.append(table); wrap.append(scroll); continue;
    }
    const heading = /^(#{1,4})\s+(.+)$/.exec(line); const node = el(heading ? 'h3' : 'p'); inline(node, heading ? heading[2] : line); wrap.append(node);
  }
  return wrap;
}
function renderMessage(role, content, refs = [], steps = []) {
  const wrap = el('article', 'message ' + role); const sources = refs || [];
  sources.forEach(r => state.sources.set(r.id, r));
  if (role === 'user') wrap.append(el('div','message-text',content));
  else {
    const heading = el('div','message-heading'); heading.append(el('span','avatar','小'), document.createTextNode('小小信息 · 知识库助手')); wrap.append(heading, answerText(content));
    if (sources.length) {
      wrap.append(el('p','sources-title','本次引用 ' + sources.length + ' 条资料 · 点击查看详情和链接'));
      const cards = el('div','sources'); sources.forEach((r,i) => {const b = el('button','source-card'); b.hidden = i >= 4; b.append(el('strong','',r.name),el('span','',r.date + ' / ' + r.category + ' / v' + r.version)); b.onclick = () => detail(r.id); cards.append(b);}); wrap.append(cards);
      if (sources.length > 4) {const more = el('button','sources-more','展开其余 ' + (sources.length-4) + ' 条来源'); more.setAttribute('aria-expanded','false'); more.onclick = () => {const expand = more.getAttribute('aria-expanded') !== 'true'; [...cards.children].forEach((b,i) => b.hidden = !expand && i >= 4); more.setAttribute('aria-expanded',String(expand)); more.textContent = expand ? '收起来源' : '展开其余 ' + (sources.length-4) + ' 条来源';}; wrap.append(more);}
    }
    if (steps.length) {const d = el('details','trace'); d.append(el('summary','','查看检索过程 · ' + steps.length + ' 步')); steps.forEach(s => d.append(el('p','',s.label))); wrap.append(d);}
    const copy = el('button','copy-answer','复制回答'); copy.onclick = async () => {try {await navigator.clipboard.writeText(content); toast('已复制回答');} catch {toast('浏览器未允许复制，请手动选择文字。');}}; wrap.append(copy);
  }
  $('#conversation').append(wrap); return wrap;
}
async function ask(question) {
  question = question.trim(); if (!question || state.busy) return;
  if (historyState.opening || historyState.pending) return toast('正在处理对话，请稍候');
  if (question.length > 5000) return toast('问题最多 5000 字，请缩短后重试');
  state.busy = true; state.controller = new AbortController(); $('#send').textContent = '■'; $('#send').setAttribute('aria-label','停止生成'); $('#send').title = '停止生成'; $('#welcome').hidden = true;
  const controller = state.controller, baseline = state.messageCount, started = Date.now();
  const userNode = renderMessage('user',question), pending = el('article','message assistant pending');
  pending.setAttribute('aria-live','off');
  const phase = el('p','pending-phase','正在连接知识库…'), elapsed = el('span','elapsed',''); pending.append(phase,elapsed); $('#conversation').append(pending);
  $('#question').value = ''; $('#question').placeholder = '可以先写下一条问题，回答完成后发送…'; pending.scrollIntoView({block:'nearest',behavior:'smooth'});
  const updatePhase = label => {phase.textContent = label; $('#run-status').textContent = label;}; updatePhase('正在连接知识库…');
  const timer = setInterval(() => elapsed.textContent = '已等待 ' + Math.floor((Date.now()-started)/1000) + ' 秒 · 可随时停止',1000);
  let completed = false;
  try {
    if (!state.session) {const s = await json('/api/sessions',{method:'POST'}); state.session = s.id; localStorage.setItem('lastSession',s.id);}
    controller.signal.throwIfAborted();
    const response = await api('/api/chat',{method:'POST',signal:controller.signal,body:JSON.stringify({session_id:state.session,question})});
    const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '';
    while (true) {const {done,value} = await reader.read(); buffer += done ? decoder.decode() : decoder.decode(value,{stream:true});
      let lineEnd; while ((lineEnd = buffer.indexOf('\n')) !== -1) {const line = buffer.slice(0,lineEnd); buffer = buffer.slice(lineEnd+1); if (!line.trim()) continue; const event = JSON.parse(line);
        if (['start','step','phase'].includes(event.type)) updatePhase(event.label);
        if (event.type === 'error') throw new Error(event.message);
        if (event.type === 'done' && !completed) {pending.remove(); renderMessage('assistant', event.answer,event.sources,event.steps).scrollIntoView({block:'start',behavior:'smooth'}); state.messageCount = baseline + 2; completed = true;}
      }
      if (done) break;
    }
    if (!completed) throw new Error('连接中断，请重试。');
  } catch (e) {
    if (!completed) {
      // The server may have committed a complete turn just before the connection closed.
      let saved = [];
      if (state.session) {try {saved = await json('/api/sessions/' + state.session + '/messages');} catch {}}
      if (saved.length > baseline && saved[baseline]?.content === question && saved[baseline+1]?.role === 'assistant') {
        pending.remove(); const reply = saved[baseline+1]; renderMessage('assistant',reply.content,reply.refs,reply.steps); state.messageCount = saved.length; completed = true;
      } else {
        const stopped = controller.signal.aborted; pending.classList.remove('pending'); pending.classList.add('failed'); pending.setAttribute('aria-live','polite');
        pending.replaceChildren(el('p','',stopped ? '已停止生成。可以重试，或修改问题后再发送。' : e instanceof TypeError ? '连接中断，请检查本机服务后重试。' : e.message));
        const actions = el('div','retry-actions'), retry = el('button','outline','重试这个问题'), edit = el('button','text-button','放回输入框');
        retry.onclick = () => {if (state.busy || historyState.opening) return toast('请先完成当前操作'); if ($('#question').value.trim()) return toast('输入框里有草稿，请先发送或保存草稿再重试'); userNode.remove(); pending.remove(); ask(question);};
        edit.onclick = () => {if ($('#question').value.trim() && $('#question').value !== question) return toast('输入框里已有草稿，请先保存草稿'); $('#question').value = question; userNode.remove(); pending.remove(); if (!$('#conversation').children.length) $('#welcome').hidden = false; $('#question').focus();};
        actions.append(retry,edit); pending.append(actions);
      }
    }
  } finally {
    clearInterval(timer); state.busy = false; state.controller = null; $('#send').textContent = '↑'; $('#send').setAttribute('aria-label','发送问题'); $('#send').title = '发送问题'; $('#question').placeholder = '问问已有资料里的项目、用途和发布动态…'; $('#run-status').textContent = ''; $('#question').focus({preventScroll:true});
    sessions().catch(() => toast('对话列表暂未刷新，已完成的回答仍保存在本机。'));
  }
}
function queryParams() {return new URLSearchParams({q:$('#search').value,start_date:$('#start-date').value,end_date:$('#end-date').value,category:$('#category').value,limit:20,offset:state.offset});}
let libraryRequest = 0;
async function loadLibrary() {
  const request = ++libraryRequest; const result = await json('/api/knowledge?' + queryParams()); if (request !== libraryRequest) return;
  state.total = result.total; $('#results-count').textContent = '共 ' + result.total + ' 条资料'; $('#knowledge-list').replaceChildren();
  if (!result.items.length) $('#knowledge-list').append(el('p','empty-state','没有找到相关资料。试试其他关键词，或补充一条新资料。'));
  result.items.forEach(item => {const card = el('button','knowledge-card'); const meta = el('div','card-meta'); meta.append(el('span','category-tag',item.category),el('span','',item.date)); const footer = el('div','card-footer'); footer.append(el('span','',item.organization || '查看来源与核对情况'),el('span','','查看资料 ↗')); card.append(meta,el('h2','',item.name),el('p','',item.function),footer); card.onclick = () => detail(item.id); $('#knowledge-list').append(card);});
  $('#page-number').textContent = (Math.floor(state.offset/20)+1) + ' / ' + Math.max(1,Math.ceil(state.total/20)); $('#previous').disabled = state.offset === 0; $('#next').disabled = state.offset + 20 >= state.total;
}
function block(root, title, text) {if (!text) return; const section = el('section','detail-block'); section.append(el('h3','',title),el('p','',text)); root.append(section);}
function linkLabel(url) {
  const u = new URL(url);
  if (u.hostname === 'github.com') {
    const project = u.pathname.split('/').slice(1,3).join('/');
    if (/\/blob\/.*\/.*license/i.test(u.pathname)) return '许可证 · ' + project;
    if (u.pathname.includes('/blob/')) return '项目说明（固定版本） · ' + project;
    if (u.pathname.includes('/releases')) return '版本发布记录 · ' + project;
    return 'GitHub 仓库 · ' + project;
  }
  let path = u.pathname; try {path = decodeURI(path);} catch {}
  return u.hostname + (path === '/' ? '' : ' · ' + path);
}
async function detail(id) {
  try {const item = await json('/api/knowledge/' + id); state.sources.set(id,item); const root = el('div','detail-content');
    root.append(el('div','detail-meta',item.date + ' / ' + item.category + ' / ' + item.id + ' / 版本 ' + item.version),el('h2','',item.name));
    if (item.organization) root.append(el('p','detail-meta',item.organization));
    block(root,'具体用途',item.function); block(root,'适用场景（示例）',item.scenario); block(root,'使用方式',item.workflow); block(root,'限制与注意',item.limitations);
    block(root,'作者当期摘要（请结合核对说明）',item.reported_claim); block(root,'资料状态',item.status);
    root.append(el('p','detail-notice',item.verification || '尚未独立核实'));
    const urls = [...(item.links||[]),...(item.additional_links||[])]; if (item.video) urls.push(item.video); if (item.comment) urls.push(item.comment);
    if (urls.length) {const section = el('section','detail-block'); section.append(el('h3','','原始链接与补充来源')); const links = el('div','detail-links'); [...new Set(urls)].forEach(url => {try {const u = new URL(url); if (!['http:','https:'].includes(u.protocol)) return; const a = el('a','',linkLabel(url)); a.href = url; a.title = url; a.target = '_blank'; a.rel = 'noopener noreferrer'; links.append(a);} catch {}}); section.append(links); root.append(section);}
    block(root,'补充说明',item.notes); const actions = el('div','detail-actions'), edit = el('button','primary','编辑这条资料'), history = el('button','outline','查看修订记录'); edit.onclick = () => {$('#detail-dialog').close(); openEditor(item);}; history.onclick = async () => {try {const rows = await json('/api/knowledge/' + id + '/revisions'); history.disabled = true; const section = el('section','detail-block'); section.append(el('h3','','修订记录')); rows.forEach(r => {const d = el('details','revision'); d.append(el('summary','','v' + r.version + ' · ' + r.created_at.slice(0,10) + ' · ' + r.reason),el('pre','',JSON.stringify(r.body,null,2))); section.append(d);}); root.append(section);} catch(e) {toast(e.message);}}; actions.append(edit,history); root.append(actions);
    const askProject = el('button','outline','问问这个项目'); askProject.onclick = () => {if ($('#question').value.trim()) return toast('输入框里已有草稿，请先保存或发送草稿'); $('#detail-dialog').close(); view('chat'); $('#question').value = item.name + ' 能做什么，适合什么场景，使用时有哪些限制？'; $('#question').focus();}; actions.prepend(askProject);
    $('#detail-body').replaceChildren(root); if (!$('#detail-dialog').open) $('#detail-dialog').showModal(); $('#detail-dialog').scrollTop = 0;
  } catch(e) {toast(e.message);}
}
function openEditor(item = null) {
  state.editing = item; const f = $('#entry-form'); f.reset(); $('#save-error').textContent = ''; $('#edit-title').textContent = item ? '修订资料 · ' + item.name : '补充资料'; $('#reason-label').hidden = !item;
  for (const input of f.elements) {if (!input.name) continue; let value = item?.[input.name] ?? ''; if (Array.isArray(value)) value = value.join('\n'); input.value = value;}
  if (!item) {f.elements.date.value = new Date().toLocaleDateString('en-CA'); f.elements.verification.value = '用户补充，尚未独立核实';}
  $('#edit-dialog').showModal();
}
async function saveEntry(event) {
  event.preventDefault(); const f = $('#entry-form'); const raw = Object.fromEntries(new FormData(f)); raw.links = raw.links.split(/\s+/).filter(Boolean);
  if (state.editing) {Object.assign(raw,{version:Number(raw.version)}); for (const k of ['type','status','reported_claim','additional_links','video','comment']) raw[k] = state.editing[k];}
  else {delete raw.id; delete raw.version;}
  $('#save-entry').disabled = true; $('#save-error').textContent = '';
  try {const item = await json('/api/knowledge' + (state.editing ? '/' + state.editing.id : ''), {method:state.editing?'PUT':'POST',body:JSON.stringify(raw)}); $('#edit-dialog').close(); toast('已保存，后续提问将检索新版本'); await refreshStatus(); if (!$('#library-view').hidden) await loadLibrary(); await detail(item.id);}
  catch(e) {$('#save-error').textContent = e.message;} finally {$('#save-entry').disabled = false;}
}
$('#nav-chat').onclick = () => view('chat'); $('#nav-library').onclick = () => view('library'); $('#new-chat').onclick = newChat;
$('#mobile-history').onclick = async () => {try {await sessions(); $('#history-status').textContent = ''; $('#history-dialog').showModal();} catch(e) {toast(e.message);}};
$('#mobile-new-chat').onclick = () => {$('#history-dialog').close(); newChat();};
const historySearches = document.querySelectorAll('[data-history-search]');
historySearches.forEach(input => input.oninput = () => {
  historyState.query = input.value;
  historySearches.forEach(other => {if (other !== input) other.value = input.value;});
  renderSessions();
});
$('#cancel-delete').onclick = () => $('#delete-dialog').close();
$('#confirm-delete').onclick = confirmDelete;
$('#delete-dialog').oncancel = e => {if (historyState.pending) e.preventDefault();};
$('#add-top').onclick = () => openEditor(); $('#entry-form').onsubmit = saveEntry;
document.querySelectorAll('.close-dialog').forEach(b => b.onclick = () => b.closest('dialog').close());
document.querySelectorAll('dialog').forEach(d => d.addEventListener('click',e => {if (e.target === d && !(d.id === 'delete-dialog' && historyState.pending)) {const r = d.getBoundingClientRect(); if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) d.close();}}));
$('#chat-form').onsubmit = e => {e.preventDefault(); if (state.busy) state.controller?.abort(); else ask($('#question').value);}; $('#question').onkeydown = e => {if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {e.preventDefault(); if (state.busy) toast('草稿已保留，回答完成后按 Enter 发送'); else ask($('#question').value);}};
document.querySelectorAll('[data-prompt]').forEach(b => b.onclick = () => ask(b.dataset.prompt));
$('#filters').onsubmit = e => {e.preventDefault(); state.offset = 0; loadLibrary().catch(e => toast(e.message));};
$('#reset-filters').onclick = () => {$('#filters').reset(); state.offset = 0; loadLibrary().catch(e => toast(e.message));};
$('#previous').onclick = () => {state.offset = Math.max(0,state.offset-20); loadLibrary().catch(e => toast(e.message));}; $('#next').onclick = () => {state.offset += 20; loadLibrary().catch(e => toast(e.message));};
$('#import-button').onclick = () => $('#import-file').click(); $('#import-file').onchange = async e => {const file = e.target.files[0]; if (!file) return; try {if (file.size > 2_000_000) throw new Error('单次最多 2MB，请拆分文件。'); const result = await json('/api/import',{method:'POST',body:JSON.stringify({filename:file.name,content:await file.text()})}); toast('导入完成：新增 ' + result.added + ' 条，跳过同日同名资料 ' + result.skipped + ' 条'); await refreshStatus(); state.offset = 0; await loadLibrary();} catch(error) {toast(error.message);} finally {e.target.value = '';}};
$('#export-button').onclick = async () => {try {const blob = await (await api('/api/export')).blob(); const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = '小小信息_知识库备份.json'; a.click(); setTimeout(() => URL.revokeObjectURL(a.href),1000); toast('已导出当前全部资料；对话和修订历史请备份 runtime 文件夹。');} catch(e) {toast(e.message);}};
$('#auth-form').onsubmit = async e => {e.preventDefault(); state.token = $('#access-token').value; sessionStorage.setItem('accessToken',state.token); $('#auth-dialog').close(); await init();};
async function init() {try {await refreshStatus(); const previous = localStorage.getItem('lastSession'); if (previous) {try {await openSession(previous);} catch {localStorage.removeItem('lastSession');}} await sessions();} catch(e) {toast(e.message);}}
init();
