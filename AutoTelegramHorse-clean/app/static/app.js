const app = {
  page: 'dashboard',
  heroVisible: true,
  titles: {dashboard:['Огляд','Панель керування'],auth:['Підключення облікового запису','Авторизація'],chats:['Список Telegram-чатів','Чати'],pch:['Незалежне розсилання','ПЧ'],pb:['Незалежне розсилання','ПБ'],farm:['Періодичне надсилання','Ферма'],ps:['Періодичне надсилання','ПС'],messages:['Історія надсилань і видалення','Повідомлення'],scheduler:['Стан фонових завдань','Планувальник'],logs:['Журнал системи в реальному часі','Журнал'],settings:['Параметри роботи','Налаштування'],system:['Надійність і зберігання','Система']},
  sse: null,
};

const $ = (selector, root=document) => root.querySelector(selector);
const $$ = (selector, root=document) => [...root.querySelectorAll(selector)];
const escapeHtml = (value='') => String(value).replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
const date = value => value ? new Intl.DateTimeFormat('uk-UA',{dateStyle:'short',timeStyle:'medium'}).format(new Date(value)) : '—';
const kindLabel = value => ({PCH:'ПЧ',PB:'ПБ',FARM:'Ферма',PS:'ПС',GEMINI:'Gemini',REPLY:'Вкрасти'}[value] || value || '—');
const statusClass = value => /AUTHORIZED|COMPLETED|DELETED|SENT|ONLINE|RUNNING/.test(value) ? 'good' : /FAILED|NOT_FOUND|ERROR|STOPPED|UNAVAILABLE/.test(value) ? 'bad' : /WAIT|PENDING|PAUSED/.test(value) ? 'warn' : 'accent';
const statusLabel = value => ({AUTHORIZED:'Авторизовано',CONFIGURATION_REQUIRED:'Потрібні налаштування',PHONE_OR_QR_REQUIRED:'Потрібен вхід',CONNECTION_ERROR:'Помилка з’єднання',IDLE:'Не запущено',RUNNING:'Виконується',WAITING:'Очікування',CYCLE_WAITING:'Очікування циклу',SENDING:'Надсилання',PAUSED:'Призупинено',PAUSED_CYCLE:'Цикл призупинено',STOPPED:'Зупинено',COMPLETED:'Завершено',DELETE_PENDING:'Очікує видалення',CHECKING:'Перевірка',DELETING:'Видалення',DELETED:'Видалено',ALREADY_DELETED:'Вже видалено',FAILED:'Помилка',SENT:'Надіслано',PENDING:'Очікування',PROCESSING:'Обробка',CANCELLED:'Скасовано',GEMINI_READY:'Gemini готовий',GEMINI_KEY_REQUIRED:'Потрібен ключ Gemini'}[value] || value || '—');
const chip = value => `<span class="status ${statusClass(value)}">${escapeHtml(statusLabel(value))}</span>`;

async function api(path, options={}) {
  const response = await fetch(path, {headers:{'Content-Type':'application/json', ...(options.headers||{})}, ...options});
  const contentType = response.headers.get('content-type') || '';
  const payload = contentType.includes('application/json') ? await response.json() : null;
  if (!response.ok) throw new Error(payload?.detail || `Помилка запиту (${response.status})`);
  return payload;
}
function toast(message, type='') {
  const el = document.createElement('div'); el.className=`toast ${type}`; el.textContent=message;
  $('#toast-area').append(el); setTimeout(()=>el.remove(), 4400);
}
function confirmAction(title, text, onConfirm) {
  const dialog = $('#confirm-dialog');
  dialog.innerHTML=`<div class="modal-inner"><h3>${escapeHtml(title)}</h3><p>${escapeHtml(text)}</p><div class="form-actions"><button class="button danger" data-confirm>Підтвердити</button><button class="button secondary" data-cancel>Скасувати</button></div></div>`;
  dialog.querySelector('[data-cancel]').addEventListener('click',()=>dialog.close());
  dialog.querySelector('[data-confirm]').addEventListener('click',()=>{dialog.close();onConfirm();});
  dialog.showModal();
}
function setContent(html) { $('#page-content').innerHTML=html; }
async function loadAccounts() {
  const data = await api('/api/accounts');
  const select = $('#account-select');
  select.innerHTML = data.accounts.map(account => `<option value="${account.id}" ${account.selected?'selected':''}>${escapeHtml(account.label)}${account.telegram.authorized?' ✓':''}</option>`).join('');
  $('#add-account').classList.toggle('hide', data.accounts.length >= data.max_accounts);
  const selectedAccount = data.accounts.find(a => a.selected);
  let renameBtn = $('#rename-account');
  if (!renameBtn) {
    renameBtn = document.createElement('button');
    renameBtn.id = 'rename-account';
    renameBtn.className = 'button secondary';
    renameBtn.style.cssText = 'padding: 6px 10px; font-size: 14px;';
    renameBtn.textContent = '✎';
    renameBtn.title = 'Перейменувати';
    $('.header-actions').insertBefore(renameBtn, $('#add-account'));
  }
  renameBtn.onclick = async()=>{const newLabel=window.prompt('Нова назва облікового запису', selectedAccount.label);if(!newLabel)return;try{await api(`/api/accounts/${selectedAccount.id}`,{method:'PUT',body:JSON.stringify({label:newLabel})});toast('Назву змінено.','success');await loadAccounts();}catch(e){toast(e.message,'error')}};
}
function empty(title, caption) { return `<div class="empty"><strong>${escapeHtml(title)}</strong>${escapeHtml(caption)}</div>`; }
function button(label, action, style='secondary', extra='') { return `<button class="button ${style}" data-action="${action}" ${extra}>${label}</button>`; }

function setPage(page) {
  app.page = page;
  app.heroVisible = false;
  $$('.nav').forEach(el=>el.classList.toggle('active', el.dataset.page===page));
  const [kicker,title] = app.titles[page]; $('#page-kicker').textContent=kicker; $('#page-title').textContent=title;
  $('#sidebar').classList.remove('open');
  const hero = $('#hero-section');
  if (hero) hero.style.display = 'none';
  renderPage();
}
async function refreshConnection() {
  try {
    const state=await api('/api/auth/status');
    const label=state.authorized ? 'Telegram підключено' : state.configured ? 'Потрібна авторизація' : 'Потрібен ключ API';
    $('#connection-label').textContent=label; $('.dot').classList.toggle('online', !!state.authorized);
  } catch (_) { $('#connection-label').textContent='Немає з’єднання'; $('.dot').classList.remove('online'); }
}
async function renderPage() {
  setContent('<div class="empty"><strong>Завантаження…</strong>Отримуємо актуальний стан системи.</div>');
  try {
    await ({dashboard:renderDashboard,auth:renderAuth,chats:renderChats,pch:()=>renderMailing('PCH'),pb:()=>renderMailing('PB'),farm:renderFarm,ps:renderPS,messages:renderMessages,scheduler:renderScheduler,logs:renderLogs,settings:renderSettings,system:renderSystem}[app.page])();
  } catch (error) { setContent(`<div class="card"><div class="notice warn">Не вдалося завантажити сторінку: ${escapeHtml(error.message)}</div></div>`); }
  refreshConnection();
}

function schedulerCard(state) {
  const groups=state.plan||[];
  const progress=state.total_groups ? Math.min(100, Math.max(0, ((state.current_group-(state.status==='COMPLETED'?0:1))/state.total_groups)*100)) : 0;
  return `<article class="card stack"><div class="card-title-row"><h3 class="card-title">${kindLabel(state.mailing_type)}</h3>${chip(state.status)}</div>
    <div class="grid three"><div><span class="label">Чатів</span><div class="metric-value">${state.eligible_chats ?? 0}</div></div><div><span class="label">Поточна група</span><div class="metric-value">${state.current_group||'—'}/${state.total_groups||'—'}</div></div><div><span class="label">Наступна</span><div class="metric-hint">${date(state.next_group_at)}</div></div></div>
    <div class="progress"><span style="width:${progress}%"></span></div>
    ${groups.length?`<div class="groups">${groups.map((group,i)=>`<div class="group">Група ${i+1}<b>${group.length} чатів</b></div>`).join('')}</div>`:''}
  </article>`;
}
function dayLabel(value) { return new Intl.DateTimeFormat('uk-UA',{weekday:'short'}).format(new Date(`${value}T12:00:00`)).replace('.', ''); }
function activityChart(days) {
  const maximum=Math.max(...days.map(day=>day.sent),1);
  return `<div class="activity-chart" role="img" aria-label="Кількість надісланих повідомлень за днями за останній тиждень">${days.map(day=>{
    const height=Math.max(7,Math.round(day.sent/maximum*100));
    const pchHeight=day.sent?Math.round(day.pch/day.sent*100):0;
    const pbHeight=day.sent?Math.round(day.pb/day.sent*100):0;
    const replyHeight=day.sent?Math.round(day.reply/day.sent*100):0;
    const psHeight=day.sent?Math.round(day.ps/day.sent*100):0;
    const farmHeight=day.sent?Math.round(day.farm/day.sent*100):0;
    return `<div class="chart-column"><div class="chart-value">${day.sent}</div><div class="chart-bar" title="${escapeHtml(day.date)}: ПЧ: ${day.pch}, ПБ: ${day.pb}, Вкрасти: ${day.reply}, ПС: ${day.ps}, Ферма: ${day.farm}"><span class="chart-bar-fill" style="height:${height}%">${pchHeight?`<i class="pch" style="height:${pchHeight}%"></i>`:''}${pbHeight?`<i class="pb" style="height:${pbHeight}%"></i>`:''}${replyHeight?`<i class="reply" style="height:${replyHeight}%"></i>`:''}${psHeight?`<i class="ps" style="height:${psHeight}%"></i>`:''}${farmHeight?`<i class="farm" style="height:${farmHeight}%"></i>`:''}</span></div><span class="chart-label">${dayLabel(day.date)}</span></div>`;
  }).join('')}</div><div class="chart-legend"><span><i class="legend-dot"></i>Усі надсилання:</span><span><i class="legend-dot pch"></i>ПЧ</span><span><i class="legend-dot pb"></i>ПБ</span><span><i class="legend-dot reply"></i>Вкрасти</span><span><i class="legend-dot ps"></i>ПС</span><span><i class="legend-dot farm"></i>Ферма</span></div>`;
}
function workloadGauge(label, state, description, tone='accent') {
  const percent=Math.min(100,Math.max(0,Number(state.percent)||0));
  const detail=state.assigned === undefined ? (state.active?'1 чат':'—') : `${state.assigned ?? 0}/${state.total ?? 0}`;
  return `<div class="gauge-card ${tone}"><div class="gauge" style="--value:${percent}"><span class="gauge-needle"></span><span class="gauge-hub"></span><div class="gauge-reading"><strong>${percent}%</strong><span>${escapeHtml(detail)}</span></div></div><strong class="gauge-title">${escapeHtml(label)}</strong><span class="gauge-note">${escapeHtml(description)}</span></div>`;
}
async function renderDashboard() {
  const hero = $('#hero-section');
  if (hero && app.heroVisible) {
    hero.style.display = 'flex';
  } else if (hero) {
    hero.style.display = 'none';
  }
  
  const data=await api('/api/status'); const auth=data.telegram; const pch=data.schedulers.PCH; const pb=data.schedulers.PB; const dashboard=data.dashboard;
  const manualSummary=[
    data.chats.pch ? `ПЧ: ${data.chats.pch}` : '',
    data.chats.pb ? `ПБ: ${data.chats.pb}` : '',
  ].filter(Boolean).join(' · ');
  setContent(`<div class="grid metrics">
    <article class="card metric"><span class="metric-label">Telegram</span><span class="metric-value">${auth.authorized?'У мережі':'Не в мережі'}</span>${chip(auth.auth_stage)}</article>
    <article class="card metric"><span class="metric-label">Чати у ручному списку</span><span class="metric-value">${data.chats.manual ?? 0}</span>${manualSummary?`<span class="metric-hint">Розподіл: ${manualSummary}</span>`:''}</article>
    <article class="card metric"><span class="metric-label">Надіслано за 7 днів</span><span class="metric-value">${dashboard.activity.total_7d}</span><span class="metric-hint">сьогодні: ${dashboard.activity.today}</span></article>
    <article class="card metric"><span class="metric-label">Очікують видалення</span><span class="metric-value">${data.messages.pending_deletion}</span><span class="metric-hint">помилки: ${data.messages.errors}</span></article>
  </div><div class="grid dashboard-grid" style="margin-top:18px"><article class="card chart-card"><div class="card-title-row"><div><h3 class="card-title">Активність надсилань</h3><p class="metric-hint">Останні 7 днів</p></div><span class="status accent">${dashboard.activity.total_7d} усього</span></div>${activityChart(dashboard.activity.days)}</article><article class="card stack"><div class="card-title-row"><div><h3 class="card-title">Завантаженість</h3><p class="metric-hint">Призначення робочих списків</p></div>${chip(data.farm.enabled?'RUNNING':'IDLE')}</div><div class="gauge-list">${workloadGauge('ПЧ',dashboard.workload.pch,pch.enabled?'виконується':'очікує запуску')}${workloadGauge('ПБ',dashboard.workload.pb,pb.enabled?'виконується':'очікує запуску')}${workloadGauge('Ферма',dashboard.workload.farm,data.farm.enabled?'активна, повідомлення зберігаються':'не запущена','farm')}</div><div class="queue-summary"><div><b>${data.messages.pending_deletion}</b><span>у черзі видалення</span></div><div><b>${data.messages.errors}</b><span>помилок</span></div><div><b>${data.messages.sent}</b><span>записів усього</span></div></div></article></div><div class="grid two" style="margin-top:18px">${schedulerCard(pch)}${schedulerCard(pb)}</div>
  <div class="card" style="margin-top:18px"><div class="card-title-row"><div><h3 class="card-title">Живий вивід системи</h3><p class="metric-hint">Логи в реальному часі</p></div><button class="button secondary" data-action="go-logs">Відкрити журнал</button></div><div id="dashboard-logs" class="dashboard-logs"></div></div>`);
  $('[data-action="go-logs"]')?.addEventListener('click',()=>setPage('logs'));
  (async()=>{try{const logs=await api('/api/logs?limit=10');const list=$('#dashboard-logs');if(list&&logs.length){list.innerHTML=logs.map(log=>`<div class="logline"><span class="level ${escapeHtml(log.level)}">${escapeHtml(log.level)}</span><span class="log-module mono">${escapeHtml(log.module)}</span><span class="log-event mono">${escapeHtml(log.event)}</span><span>${escapeHtml(log.message)}${log.chat_id?` <span class="mono">чат:${log.chat_id}</span>`:''}</span></div>`).join('');}}catch(e){console.error('Failed to load logs:',e)}})();
  connectDashboardLogs();
}

async function renderAuth() {
  const status=await api('/api/auth/status');
  const setup=!status.configured ? `<div class="notice warn">Додайте API_ID і API_HASH з <code>my.telegram.org/apps</code> у файл <code>.env</code>, а потім перезапустіть застосунок. Ці значення не показуються та не записуються до журналу.</div>` : '';
  setContent(`<div class="grid two"><article class="card stack"><div class="card-title-row"><h3 class="card-title">Стан підключення</h3>${chip(status.auth_stage)}</div>${setup}
    <dl class="kv"><dt>API налаштовано</dt><dd>${status.configured?'Так':'Ні'}</dd><dt>З’єднання</dt><dd>${status.connected?'Встановлено':'Немає'}</dd><dt>Сесія</dt><dd>${status.authorized?'Авторизована':'Не авторизована'}</dd></dl>
    ${status.authorized?`<div class="form-actions">${button('Вийти з Telegram','logout','danger')}</div>`:''}</article>
  <article class="card"><h3 class="card-title">Увійти за номером</h3><form id="phone-form" class="stack"><div class="field"><label>Номер телефону в міжнародному форматі</label><input required name="phone" placeholder="+380…" autocomplete="tel"></div><div class="form-actions">${button('Отримати код','phone')}</div></form><form id="code-form" class="stack" style="margin-top:16px"><div class="field"><label>Код Telegram</label><input required name="code" inputmode="numeric" autocomplete="one-time-code"></div>${button('Підтвердити код','code')}</form><form id="password-form" class="stack" style="margin-top:16px"><div class="field"><label>Пароль двоетапної перевірки</label><input required type="password" name="password" autocomplete="current-password"></div>${button('Підтвердити 2FA','password')}</form></article></div>
  <div class="card" style="margin-top:18px"><div class="card-title-row"><div><h3 class="card-title">Увійти за QR-кодом</h3><p class="metric-hint">Створюється справжній одноразовий QR-токен Telegram. Відкрийте Telegram на телефоні → Налаштування → Пристрої → Підключити пристрій.</p></div>${button('Створити QR','qr')}</div><div id="qr-result"></div></div>`);
  $('#phone-form').addEventListener('submit',submitForm('/api/auth/phone','Номер прийнято, введіть код Telegram.'));
  $('#code-form').addEventListener('submit',submitForm('/api/auth/code','Код прийнято.'));
  $('#password-form').addEventListener('submit',submitForm('/api/auth/2fa','Двоетапну перевірку завершено.'));
  $('[data-action="qr"]').addEventListener('click',async()=>{try{const data=await api('/api/auth/qr',{method:'POST'});$('#qr-result').innerHTML=`<img class="qr" alt="QR-код для входу Telegram" src="${data.qr_image}"><p class="notice">Очікування сканування. QR-код втратить чинність, якщо не використати його вчасно.</p>`;toast('Справжній QR-код Telegram створено.','success');}catch(e){toast(e.message,'error')}});
  $('[data-action="logout"]')?.addEventListener('click',async()=>{try{await api('/api/auth/logout',{method:'POST'});toast('Сесію Telegram завершено.','success');renderPage()}catch(e){toast(e.message,'error')}});
}
function submitForm(url, success) { return async event => { event.preventDefault(); const body=Object.fromEntries(new FormData(event.currentTarget)); try {const data=await api(url,{method:'POST',body:JSON.stringify(body)});toast(data.status==='PASSWORD_REQUIRED'?'Потрібен пароль 2FA.':success,'success');renderPage();} catch(e){toast(e.message,'error')} }; }

async function renderChats() {
  const chats=await api('/api/chats');
  setContent(`<div class="toolbar"><div class="notice">Після авторизації застосунок сам завантажує всі доступні Telegram-чати. Тут можна ввімкнути будь-який чат для ПЧ, ПБ, Gemini або автовідповіді.</div>${button('Оновити чати','refresh-chats','secondary')}</div>
  <div class="card" style="margin-top:18px"><div class="card-title-row"><h3 class="card-title">Додати чат за ID</h3><span class="metric-hint">Telegram підтвердить ID та назву.</span></div><form id="add-chat" class="form-actions"><input class="inline-input" name="chat_id" required inputmode="numeric" placeholder="ID чату" style="max-width:260px;border:1px solid var(--border);border-radius:9px;background:var(--surface-2);color:var(--text);padding:9px">${button('Додати чат','add-chat')}</form></div>
  <div class="card" style="margin-top:18px"><div class="card-title-row"><h3 class="card-title">Чати (${chats.length})</h3><span class="metric-hint">Зміни застосуються з наступного циклу; поточний план лишається сталим.</span></div>${chats.length?`<div class="table-wrap"><table class="table"><thead><tr><th>Назва</th><th>ID чату</th><th>Тип</th><th>ПЧ</th><th>ПБ</th><th>Gemini</th><th>ОМ/ОЧ/ОБ</th><th></th></tr></thead><tbody>${chats.map(c=>`<tr><td><div class="chat-name">${escapeHtml(c.title)}</div><div class="mono">${escapeHtml(c.username||'без імені користувача')}</div></td><td class="mono">${c.chat_id}</td><td>${escapeHtml(c.chat_type)}</td><td><input class="switch assignment" data-id="${c.chat_id}" data-field="pch_enabled" type="checkbox" ${c.pch_enabled?'checked':''}></td><td><input class="switch assignment" data-id="${c.chat_id}" data-field="pb_enabled" type="checkbox" ${c.pb_enabled?'checked':''}></td><td><input class="switch assignment" data-id="${c.chat_id}" data-field="gemini_enabled" type="checkbox" ${c.gemini_enabled?'checked':''}></td><td><input class="switch assignment" data-id="${c.chat_id}" data-field="keyword_reply_enabled" type="checkbox" ${c.keyword_reply_enabled?'checked':''}></td><td><button class="button small secondary remove-chat" data-id="${c.chat_id}">Видалити</button></td></tr>`).join('')}</tbody></table></div>`:empty('Список поки порожній','Авторизуйтеся в Telegram або додайте чат за ID.')}</div>`);
  $('#add-chat').addEventListener('submit', async event=>{event.preventDefault();const chat_id=Number(new FormData(event.currentTarget).get('chat_id'));try{await api('/api/chats',{method:'POST',body:JSON.stringify({chat_id})});toast('Чат додано до робочого списку.','success');renderPage()}catch(e){toast(e.message,'error')}});
  $$('.assignment').forEach(el=>el.addEventListener('change',async event=>{const node=event.currentTarget;try{await api(`/api/chats/${node.dataset.id}`,{method:'PUT',body:JSON.stringify({[node.dataset.field]:node.checked})});toast('Список розсилання оновлено.','success')}catch(e){node.checked=!node.checked;toast(e.message,'error')}}));
  $$('.remove-chat').forEach(el=>el.addEventListener('click',()=>confirmAction('Видалити чат із робочого списку?', 'Він зникне зі списку та не потраплятиме до нових циклів. Історія повідомлень збережеться.', ()=>runAction(`/api/chats/${el.dataset.id}`,'DELETE','Чат прибрано з робочого списку.',renderPage))));
  $('[data-action="refresh-chats"]')?.addEventListener('click', async()=>{try{const result=await api('/api/chats/refresh',{method:'POST'});toast(`Чати оновлено: додано ${result.added}, оновлено ${result.updated}.`,'success');renderPage()}catch(e){toast(e.message,'error')}});
}
async function renderFarm() {
  const [farm,chats]=await Promise.all([api('/api/farm'),api('/api/chats')]);
  const options=chats.map(c=>`<option value="${c.chat_id}" ${farm.chat_id===c.chat_id?'selected':''}>${escapeHtml(c.title)} (${c.chat_id})</option>`).join('');
  setContent(`<div class="grid two"><article class="card stack"><div class="card-title-row"><h3 class="card-title">Ферма</h3>${chip(farm.status)}</div><p class="notice">Повідомлення надсилається в один обраний чат одразу після ввімкнення, а далі кожні <b>3 години 58 хвилин</b>. Повідомлення Ферми не видаляються автоматично. Налаштування зберігаються та відновлюються після перезапуску.</p><form id="farm-form" class="stack"><div class="field"><label>Чат призначення</label><select name="chat_id" required><option value="">Оберіть чат</option>${options}</select></div><div class="field"><label>Текст повідомлення</label><textarea name="text" required readonly style="background:var(--surface-2);cursor:not-allowed">${escapeHtml(farm.message_text||'ферма')}</textarea></div><div class="field"><label>Стан</label><select name="enabled"><option value="true" ${farm.enabled?'selected':''}>Увімкнено</option><option value="false" ${!farm.enabled?'selected':''}>Вимкнено</option></select></div><div class="form-actions">${button('Зберегти','save-farm','primary')}</div></form></article><article class="card stack"><h3 class="card-title">Стан</h3><dl class="kv"><dt>Наступне надсилання</dt><dd>${date(farm.next_send_at)}</dd><dt>Останнє надсилання</dt><dd>${date(farm.last_sent_at)}</dd><dt>Інтервал</dt><dd>3 години 58 хвилин</dd><dt>Видалення</dt><dd>Вимкнено</dd></dl>${farm.last_error?`<div class="notice warn">Остання помилка: ${escapeHtml(farm.last_error)}. Наступну спробу буде виконано автоматично.</div>`:''}</article></div>`);
  $('#farm-form').addEventListener('submit',async event=>{event.preventDefault();const data=Object.fromEntries(new FormData(event.currentTarget));data.chat_id=data.chat_id?Number(data.chat_id):null;data.enabled=data.enabled==='true';data.text='ферма';try{await api('/api/farm',{method:'PUT',body:JSON.stringify(data)});toast(data.enabled?'Ферму ввімкнено.':'Ферму зупинено.','success');renderPage()}catch(e){toast(e.message,'error')}});
}
async function renderPS() {
  const [ps,chats]=await Promise.all([api('/api/ps'),api('/api/chats')]);
  const options=chats.map(c=>`<option value="${c.chat_id}" ${ps.chat_id===c.chat_id?'selected':''}>${escapeHtml(c.title)} (${c.chat_id})</option>`).join('');
  setContent(`<div class="grid two"><article class="card stack"><div class="card-title-row"><h3 class="card-title">ПС</h3>${chip(ps.status)}</div><p class="notice">Повідомлення надсилається в один обраний чат одразу після ввімкнення, а далі кожні <b>23 години 58 хвилин</b>. Повідомлення ПС не видаляються автоматично. Налаштування зберігаються та відновлюються після перезапуску.</p><form id="ps-form" class="stack"><div class="field"><label>Чат призначення</label><select name="chat_id" required><option value="">Оберіть чат</option>${options}</select></div><div class="field"><label>Текст повідомлення</label><textarea name="text" required readonly style="background:var(--surface-2);cursor:not-allowed">${escapeHtml(ps.message_text||'пограбувати світ')}</textarea></div><div class="field"><label>Стан</label><select name="enabled"><option value="true" ${ps.enabled?'selected':''}>Увімкнено</option><option value="false" ${!ps.enabled?'selected':''}>Вимкнено</option></select></div><div class="form-actions">${button('Зберегти','save-ps','primary')}</div></form></article><article class="card stack"><h3 class="card-title">Стан</h3><dl class="kv"><dt>Наступне надсилання</dt><dd>${date(ps.next_send_at)}</dd><dt>Останнє надсилання</dt><dd>${date(ps.last_sent_at)}</dd><dt>Інтервал</dt><dd>23 години 58 хвилин</dd><dt>Видалення</dt><dd>Вимкнено</dd></dl>${ps.last_error?`<div class="notice warn">Остання помилка: ${escapeHtml(ps.last_error)}. Наступну спробу буде виконано автоматично.</div>`:''}</article></div>`);
  $('#ps-form').addEventListener('submit',async event=>{event.preventDefault();const data=Object.fromEntries(new FormData(event.currentTarget));data.chat_id=data.chat_id?Number(data.chat_id):null;data.enabled=data.enabled==='true';data.text='пограбувати світ';try{await api('/api/ps',{method:'PUT',body:JSON.stringify(data)});toast(data.enabled?'ПС ввімкнено.':'ПС зупинено.','success');renderPage()}catch(e){toast(e.message,'error')}});
}
async function runAction(path,method,message,after) {try{await api(path,{method});toast(message,'success');if(after)after()}catch(e){toast(e.message,'error')}}

async function renderMailing(kind) {
  const lower=kind.toLowerCase(); const [state,template]=await Promise.all([api(`/api/scheduler/${lower}`),api(`/api/scheduler/${lower}/template`)]);
  const description=kind==='PCH'?'ПЧ поділяється на кількість груп із Налаштувань (типово 6).':'ПБ поділяється на кількість груп із Налаштувань (типово 12).';
  const isPB = kind === 'PB';
  const isPCH = kind === 'PCH';
  const isFixed = isPB || isPCH;
  const defaultText = isPB ? 'пограбувати банк' : (isPCH ? 'пограбувати чат' : '');
  const displayText = template.text || defaultText;
  const textareaAttrs = isFixed ? 'readonly style="background:var(--surface-2);cursor:not-allowed"' : '';
  const saveButton = isFixed ? '' : button('Зберегти текст','save-template','secondary');
  setContent(`<div class="grid two"><div>${schedulerCard(state)}</div><article class="card"><h3 class="card-title">Повідомлення ${kindLabel(kind)}</h3><p class="metric-hint">${description} Затримка діє між групами, а не між повідомленнями.</p><form id="template-form" class="stack"><div class="field"><label>Текст повідомлення</label><textarea required name="text" placeholder="Введіть повідомлення…" ${textareaAttrs}>${escapeHtml(displayText)}</textarea></div><div class="form-actions">${saveButton}${button('Запустити','start')} ${button('Призупинити','pause','secondary')} ${button('Продовжити','resume','secondary')} ${button('Зупинити','stop','danger')}</div></form></article></div>
  <div class="card" style="margin-top:18px"><h3 class="card-title">Гарантії виконання</h3><div class="grid three"><div><span class="label">План груп</span><p class="metric-hint">Знімок створюється під час запуску, тому зміна списку не порушує розпочатий цикл.</p></div><div><span class="label">Захист від повтору</span><p class="metric-hint">ПЧ не повторюється в тому самому чаті раніше ніж за 6 годин, ПБ — за 12 годин.</p></div><div><span class="label">Відновлення</span><p class="metric-hint">Після перезапуску продовжуються лише ще не завершені доставки.</p></div></div></div>`);
  if (!isFixed) {
    $('#template-form').addEventListener('submit', async event=>{event.preventDefault();const text=new FormData(event.currentTarget).get('text');try{await api(`/api/scheduler/${lower}/template`,{method:'PUT',body:JSON.stringify({text})});toast('Текст повідомлення збережено.','success')}catch(e){toast(e.message,'error')}});
  }
  const actionLabel={start:'запущено',pause:'призупинено',resume:'продовжено',stop:'зупинено'};
  ['start','pause','resume','stop'].forEach(action=>{ $(`[data-action="${action}"]`).addEventListener('click', async event=>{event.preventDefault(); const text=new FormData($('#template-form')).get('text');try{await api(`/api/scheduler/${lower}/${action}`,{method:'POST',body:action==='start'?JSON.stringify({text}):undefined});toast(`${kindLabel(kind)}: ${actionLabel[action]}.`,'success');renderPage()}catch(e){toast(e.message,'error')}}); });
}

async function renderMessages() {
  const messages=await api('/api/messages');
  setContent(`<div class="grid two"><article class="card"><h3 class="card-title">Надіслати зараз</h3><p class="notice">Ручне надсилання можливе до чату, який ви ввімкнули у відповідному списку ПЧ, ПБ або ПС. Повідомлення буде видалено за чинним таймером.</p><form id="manual-message" class="stack" style="margin-top:14px"><div class="form-grid"><div class="field"><label>ID чату</label><input required name="chat_id" inputmode="numeric"></div><div class="field"><label>Тип</label><select name="message_type"><option value="PCH">ПЧ</option><option value="PB">ПБ</option><option value="PS">ПС</option></select></div></div><div class="field"><label>Текст</label><textarea required name="text"></textarea></div>${button('Надіслати','manual-send')}</form></article><article class="card"><h3 class="card-title">Видалення</h3><p class="metric-hint">Перед видаленням сервіс запитує повідомлення у Telegram. Відсутнє повідомлення має стан <b>ALREADY_DELETED</b>, а тимчасові збої повторюються обмежену кількість разів.</p></article></div>
  <div class="card" style="margin-top:18px"><div class="card-title-row"><h3 class="card-title">Останні повідомлення</h3><span class="metric-hint">${messages.length} записів</span></div>${messages.length?`<div class="table-wrap"><table class="table"><thead><tr><th>Тип</th><th>Чат</th><th>ID повідомлення</th><th>Надіслано</th><th>Видалення</th><th>Стан</th></tr></thead><tbody>${messages.map(m=>`<tr><td>${kindLabel(m.message_type)}</td><td class="mono">${m.chat_id}</td><td class="mono">${m.telegram_message_id}</td><td>${date(m.sent_at)}</td><td>${m.delete_scheduled?date(m.delete_at):'Не видаляється'}</td><td>${chip(m.status)}</td></tr>`).join('')}</tbody></table></div>`:empty('Надсилань ще немає','Тут з’являться справді надіслані Telegram-повідомлення.')}</div>`);
  $('#manual-message').addEventListener('submit',async e=>{e.preventDefault();const form=Object.fromEntries(new FormData(e.currentTarget));form.chat_id=Number(form.chat_id);try{await api('/api/messages/send',{method:'POST',body:JSON.stringify(form)});toast('Повідомлення надіслано; видалення заплановано.','success');renderPage()}catch(err){toast(err.message,'error')}});
}

async function renderScheduler() { const states=await api('/api/scheduler/status');setContent(`<div class="grid two">${schedulerCard(states.PCH)}${schedulerCard(states.PB)}</div><div class="card" style="margin-top:18px"><h3 class="card-title">Незалежні процеси</h3><p class="metric-hint">Пауза, зупинка або очікування ПЧ не впливає на ПБ і навпаки. Час наступної та поточна група зберігаються у SQLite.</p></div>`); }

async function renderLogs() {
  const logs=await api('/api/logs?limit=300');
  setContent(`<div class="card"><div class="toolbar"><div><h3 class="card-title">Журнал у реальному часі</h3><span class="metric-hint">Нові записи надходять через Server-Sent Events.</span></div><form id="log-filter" class="form-actions" style="margin:0"><select name="level"><option value="">Усі рівні</option><option>DEBUG</option><option>INFO</option><option>WARNING</option><option>ERROR</option><option>CRITICAL</option></select><input name="q" placeholder="Пошук" style="border:1px solid var(--border);border-radius:9px;background:var(--surface-2);color:var(--text);padding:8px">${button('Знайти','filter-logs','secondary')}</form></div><div id="log-list">${logsHtml(logs)}</div></div>`);
  $('#log-filter').addEventListener('submit',async e=>{e.preventDefault(); const query=new URLSearchParams(new FormData(e.currentTarget));try{$('#log-list').innerHTML=logsHtml(await api(`/api/logs?limit=300&${query}`))}catch(err){toast(err.message,'error')}});
  connectLogs();
}
function logsHtml(logs) { return logs.length?logs.map(log=>`<div class="logline"><span class="level ${escapeHtml(log.level)}">${escapeHtml(log.level)}</span><span class="log-module mono">${escapeHtml(log.module)}</span><span class="log-event mono">${escapeHtml(log.event)}</span><span>${escapeHtml(log.message)}${log.chat_id?` <span class="mono">чат:${log.chat_id}</span>`:''}</span></div>`).join(''):empty('За цим фільтром журнал порожній','Нові системні події з\'являться тут.'); }
function connectLogs() { if(app.sse) app.sse.close(); app.sse=new EventSource('/api/events'); app.sse.addEventListener('log.created',event=>{const log=JSON.parse(event.data);if(app.page==='logs'){const list=$('#log-list');if(list){if(list.querySelector('.empty'))list.innerHTML='';list.insertAdjacentHTML('afterbegin',logsHtml([log]));while(list.children.length>300)list.lastElementChild.remove();}}if(app.page==='dashboard'){const list=$('#dashboard-logs');if(list){const logLine=`<div class="logline"><span class="level ${escapeHtml(log.level)}">${escapeHtml(log.level)}</span><span class="log-module mono">${escapeHtml(log.module)}</span><span class="log-event mono">${escapeHtml(log.event)}</span><span>${escapeHtml(log.message)}${log.chat_id?` <span class="mono">чат:${log.chat_id}</span>`:''}</span></div>`;list.insertAdjacentHTML('afterbegin',logLine);while(list.children.length>10)list.lastElementChild.remove();}}}); }
function connectDashboardLogs() { connectLogs(); }

async function renderSettings() { const [s,g]=await Promise.all([api('/api/settings'),api('/api/gemini/settings')]);const geminiModels=['gemini-2.0-flash-exp','gemini-1.5-flash','gemini-1.5-flash-8b','gemini-flash-lite-latest'];setContent(`<div class="card"><h3 class="card-title">Параметри планувальника</h3><form id="settings-form" class="stack"><div class="form-grid"><div class="field"><label>Видаляти через, секунд</label><input name="delete_after_seconds" type="number" min="1" max="86400" value="${s.delete_after_seconds}"></div><div class="field"><label>Повторів видалення</label><input name="max_delete_retries" type="number" min="0" max="10" value="${s.max_delete_retries}"></div><div class="field"><label>Груп ПЧ</label><input name="pch_groups" type="number" min="1" max="100" value="${s.pch_groups}"></div><div class="field"><label>Груп ПБ</label><input name="pb_groups" type="number" min="1" max="100" value="${s.pb_groups}"></div><div class="field"><label>Затримка між групами, секунд</label><input name="group_delay_seconds" type="number" min="1" max="604800" value="${s.group_delay_seconds}"></div><div class="field"><label>Рівень журналювання</label><select name="log_level">${['DEBUG','INFO','WARNING','ERROR','CRITICAL'].map(x=>`<option ${s.log_level===x?'selected':''}>${x}</option>`).join('')}</select></div><div class="field"><label>Тема</label><select name="theme"><option value="dark" ${s.theme==='dark'?'selected':''}>Темна</option><option value="light" ${s.theme==='light'?'selected':''}>Світла</option></select></div><div class="field"><label>Автоперепідключення</label><select name="auto_reconnect"><option value="true" ${s.auto_reconnect?'selected':''}>Увімкнено</option><option value="false" ${!s.auto_reconnect?'selected':''}>Вимкнено</option></select></div></div>${button('Зберегти налаштування','save-settings')}</form></div><div class="card" style="margin-top:18px"><div class="card-title-row"><div><h3 class="card-title">Помічник Gemini</h3><p class="metric-hint">Відповідає лише на вхідні повідомлення у групах, де ввімкнено перемикач Gemini. Налаштування окремі для вибраного облікового запису. Відповіді Gemini не видаляються автоматично.</p></div>${chip(g.configured?'GEMINI_READY':'GEMINI_KEY_REQUIRED')}</div><form id="gemini-form" class="stack" style="margin-top:14px"><div class="form-grid"><div class="field"><label>Автовідповіді Gemini</label><select name="enabled"><option value="false" ${!g.enabled?'selected':''}>Вимкнено</option><option value="true" ${g.enabled?'selected':''}>Увімкнено</option></select></div><div class="field"><label>Модель (безкоштовні)</label><select name="model">${geminiModels.map(m=>`<option value="${m}" ${g.model===m?'selected':''}>${m}</option>`).join('')}</select></div></div><div class="field"><label>Інструкція для помічника</label><textarea required name="system_prompt">${escapeHtml(g.system_prompt)}</textarea></div>${button('Зберегти Gemini','save-gemini')}</form>${g.configured?'':`<div class="notice warn">Додайте <code>GEMINI_API_KEY</code> до <code>.env</code> та перезапустіть застосунок перед увімкненням Gemini. Ключ ніколи не зберігається в базі даних або журналі.</div>`}</div><div class="notice warn" style="margin-top:18px">Зміна числа груп і затримки застосовується до наступного нового запуску ПЧ або ПБ. Поточний збережений план не змінюється.</div>`);
  $('#settings-form').addEventListener('submit',async e=>{e.preventDefault(); const data=Object.fromEntries(new FormData(e.currentTarget));['delete_after_seconds','max_delete_retries','pch_groups','pb_groups','group_delay_seconds'].forEach(k=>data[k]=Number(data[k]));data.auto_reconnect=data.auto_reconnect==='true';try{await api('/api/settings',{method:'PUT',body:JSON.stringify(data)});document.documentElement.dataset.theme=data.theme;toast('Налаштування збережено.','success')}catch(err){toast(err.message,'error')}});
  $('#gemini-form').addEventListener('submit',async e=>{e.preventDefault(); const data=Object.fromEntries(new FormData(e.currentTarget));data.enabled=data.enabled==='true';try{await api('/api/gemini/settings',{method:'PUT',body:JSON.stringify(data)});toast('Налаштування Gemini збережено.','success');renderPage()}catch(err){toast(err.message,'error')}});
}
async function renderSystem() { const sys=await api('/api/system');const status=await api('/api/auth/status');setContent(`<div class="grid two"><article class="card"><h3 class="card-title">Стан системи</h3><dl class="kv"><dt>Тека даних</dt><dd class="mono">${escapeHtml(sys.data_directory)}</dd><dt>Сесія на диску</dt><dd>${sys.session_persistent?'Є':'З’явиться після авторизації'}</dd><dt>Відновлення планувальника</dt><dd>${sys.scheduler_recovery?'Увімкнено':'Ні'}</dd><dt>Відновлення видалення</dt><dd>${sys.delete_task_recovery?'Увімкнено':'Ні'}</dd></dl></article><article class="card"><h3 class="card-title">Telegram</h3><dl class="kv"><dt>API</dt><dd>${status.configured?'Налаштовано':'Не налаштовано'}</dd><dt>Авторизація</dt><dd>${status.authorized?'Активна':'Потрібна'}</dd><dt>Автоперепідключення</dt><dd>Підтримується клієнтом Telethon</dd></dl></article></div><div class="notice" style="margin-top:18px">Під час штатної зупинки застосунок зберігає завдання у SQLite, закриває Telegram-з’єднання та після наступного запуску продовжує незавершені операції.</div>`); }

document.addEventListener('DOMContentLoaded',async()=>{
  $$('.nav').forEach(el=>el.addEventListener('click',()=>setPage(el.dataset.page)));
  $('#menu-button').addEventListener('click',()=>$('#sidebar').classList.toggle('open'));
  $('#global-refresh').addEventListener('click',()=>renderPage());
  $('#theme-button').addEventListener('click',async()=>{try{const s=await api('/api/settings');s.theme=s.theme==='dark'?'light':'dark';await api('/api/settings',{method:'PUT',body:JSON.stringify(s)});document.documentElement.dataset.theme=s.theme;toast('Тему змінено.','success')}catch(e){toast(e.message,'error')}});
  $('#account-select').addEventListener('change', async event=>{try{await api(`/api/accounts/${event.currentTarget.value}/select`,{method:'POST'});toast('Обліковий запис перемкнено.','success');await loadAccounts();renderPage()}catch(e){toast(e.message,'error')}});
  $('#add-account').addEventListener('click', async()=>{const label=window.prompt('Назва нового облікового запису Telegram');if(!label)return;try{await api('/api/accounts',{method:'POST',body:JSON.stringify({label})});toast('Обліковий запис створено. Авторизуйтеся на сторінці «Авторизація».','success');await loadAccounts();setPage('auth')}catch(e){toast(e.message,'error')}});
  try {const s=await api('/api/settings');document.documentElement.dataset.theme=s.theme;} catch (_) {}
  try {await loadAccounts();} catch (e) {toast(e.message,'error');}
  renderPage(); setInterval(refreshConnection,30000);
});
