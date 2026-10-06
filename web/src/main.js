import './style.css';

const API = '/api';
const state = {
  devices: [],
  selected: null,
  device: null,
  flash: null,
  concurrent: null,
};

const h = (tag, attrs = {}, ...children) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return el;
};

async function api(method, path, body) {
  const res = await fetch(API + path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data?.error?.detail || res.statusText), {
    status: res.status, data,
  });
  return data;
}

function flash(kind, text) { state.flash = { kind, text }; render(); }

async function refresh(selectId) {
  const list = await api('GET', '/devices');
  state.devices = list.devices;
  if (selectId) state.selected = selectId;
  if (!state.selected && state.devices.length) state.selected = state.devices[0].device_id;
  if (state.selected) {
    const d = await api('GET', `/devices/${encodeURIComponent(state.selected)}`);
    state.device = d.device;
  } else state.device = null;
  render();
}

// ---------------------------------------------------------------- rendering
function render() {
  const app = document.getElementById('app');
  app.innerHTML = '';
  app.append(renderHeader(), renderCreate());
  if (state.flash) {
    app.append(h('div', { class: `flash ${state.flash.kind}` }, state.flash.text));
  }
  if (state.devices.length) app.append(renderDeviceBar());
  if (state.device) {
    app.append(renderDevice(), renderRecovery(), renderCandidate(),
      renderConcurrent(), renderEvidence());
  }
}

function renderHeader() {
  return h('header', {},
    h('h1', {}, '轨道载荷 A/B 双槽镜像升级 — 断电安全验收台'),
    h('p', {}, '任意断电均不会引导摘要不符或未确认的候选；新版本生效后不可回退。'
      + '服务持久化槽位清单、候选阶段与确认代次，上电时仅从清单完整且已确认的槽位裁决唯一活动槽位。'),
  );
}

function renderCreate() {
  const id = h('input', { value: 'payload-01', placeholder: '设备编号' });
  const ver = h('input', { value: '1.0.0', placeholder: '当前版本，如 1.0.0' });
  const btn = h('button', {}, '创建双槽设备（含当前版本与镜像摘要）');
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      const r = await api('POST', '/devices', { device_id: id.value.trim(), version: ver.value.trim() });
      state.flash = null;
      await refresh(r.device.device_id);
    } catch (e) { flash('err', `创建失败：${e.message}`); } finally { btn.disabled = false; }
  });
  return h('section', { class: 'card' },
    h('h2', {}, '1. 设备出厂 / 初始化'),
    h('div', { class: 'row' },
      h('div', { class: 'field' }, h('label', {}, '设备编号'), id),
      h('div', { class: 'field' }, h('label', {}, '当前固件版本'), ver),
      btn,
    ),
  );
}

function renderDeviceBar() {
  return h('section', { class: 'card' },
    h('h2', {}, '设备列表'),
    h('div', { class: 'devlist' },
      ...state.devices.map((d) => h('button', {
        class: d.device_id === state.selected ? 'sel' : '',
        onclick: () => { state.selected = d.device_id; state.flash = null; state.concurrent = null; refresh(); },
      }, d.device_id)),
    ),
  );
}

function statusBadge(slot) {
  return h('span', { class: `badge ${slot.status}` }, slot.status);
}

function renderSlot(slot) {
  const active = slot.name === state.device.active_slot;
  return h('div', { class: `slot ${active ? 'active' : ''}` },
    h('h4', {}, `槽位 ${slot.name}`,
      active ? h('span', { class: 'active-tag' }, '● 活动槽位（正在引导）') : statusBadge(slot)),
    h('div', { class: 'kv' },
      h('div', {}, h('b', {}, '状态'), slot.status),
      h('div', {}, h('b', {}, '版本'), slot.version ?? '—'),
      h('div', {}, h('b', {}, '清单摘要'), h('span', { class: 'mono' }, slot.digest ?? '—')),
      h('div', {}, h('b', {}, '实测摘要'), h('span', { class: 'mono' }, slot.actual_digest ?? '—')),
      h('div', {}, h('b', {}, '写入进度'), slot.size == null ? '出厂预置' : `${slot.written}/${slot.size} 字节`),
      h('div', {}, h('b', {}, '确认代次'), slot.confirmed_generation ?? '—'),
      h('div', {}, h('b', {}, '清单完整'), String(slot.manifest_complete)),
      h('div', {}, h('b', {}, '可引导'), String(slot.bootable)),
    ),
  );
}

function renderDevice() {
  const d = state.device;
  const powerBtn = d.powered_on
    ? h('button', {
      class: 'danger',
      onclick: () => wrap(() => api('POST', `/devices/${encodeURIComponent(d.device_id)}/power-off`)),
    }, '模拟断电')
    : h('button', { onclick: () => wrapPowerOn(d.device_id) }, '重新打开设备（上电恢复）');
  return h('section', { class: 'card' },
    h('h2', {}, `设备 ${d.device_id}`),
    h('div', { class: 'meta' },
      h('span', {}, h('b', {}, '电源：'),
        h('span', { class: d.powered_on ? 'power-on' : 'power-off' }, d.powered_on ? '通电运行' : '已断电')),
      h('span', {}, h('b', {}, '活动槽位：'), d.active_slot),
      h('span', {}, h('b', {}, '活动版本：'), d.slots[d.active_slot]?.version ?? '不可引导'),
      h('span', {}, h('b', {}, '确认代次：'), String(d.generation)),
      h('span', {}, h('b', {}, '升级资格：'),
        d.qualified_request ? `已由 ${d.qualified_request} 于代次 ${d.qualified_generation} 取得（槽 ${d.qualified_slot}）` : '空闲'),
      powerBtn,
    ),
    h('div', { class: 'slots' }, ...['A', 'B'].map((n) => renderSlot(d.slots[n]))),
  );

  async function wrap(fn) {
    try { await fn(); state.flash = null; await refresh(); }
    catch (e) { flash('err', e.message); }
  }
}

async function wrapPowerOn(id) {
  try {
    const r = await api('POST', `/devices/${encodeURIComponent(id)}/power-on`);
    const rec = r.recovery;
    if (rec.active_slot) {
      flash('ok', `上电恢复完成：从槽位 ${rec.active_slot} 引导（版本 ${r.device.slots[rec.active_slot].version}，代次 ${rec.generation}），裁决依据见下方恢复报告。`);
    } else {
      flash('err', '上电恢复：无合格槽位，设备未引导（见恢复报告中的诊断证据）。');
    }
    await refresh(id);
  } catch (e) { flash('err', e.message); }
}

function renderRecovery() {
  const rec = state.device.last_recovery;
  const card = h('section', { class: 'card recovery' }, h('h2', {}, '恢复裁决依据（最近一次上电）'));
  if (!rec) { card.append(h('p', { class: 'hint' }, '尚无恢复记录。')); return card; }
  card.append(
    h('div', { class: 'meta' },
      h('span', {}, h('b', {}, '选定活动槽位：'), rec.active_slot ?? '无（拒绝引导）'),
      h('span', {}, h('b', {}, '确认代次：'), String(rec.generation)),
      h('span', {}, h('b', {}, '合格槽位：'), rec.eligible.length ? rec.eligible.join(', ') : '无'),
      h('span', {}, h('b', {}, '触发方式：'), rec.powered_from_off ? '断电后重新上电' : '初始上电'),
    ),
    rec.critical ? h('div', { class: 'flash err' }, rec.critical) : null,
    h('h3', {}, '裁决理由'),
    h('ul', { class: 'rationale' }, ...rec.rationale.map((x) => h('li', {}, x))),
    h('h3', {}, '逐槽诊断证据（未确认 / 损坏候选保留且不引导）'),
    ...rec.diagnoses.map((dg) => h('div', {
      class: `diagnosis ${dg.reason === 'eligible' ? 'good' : 'bad'}`,
    },
      h('div', {}, `槽位 ${dg.slot}：${dg.detail}`),
      h('div', { class: 'reason' }, `diagnostic=${dg.reason}`),
    )),
  );
  return card;
}

function renderCandidate() {
  const d = state.device;
  const ver = h('input', { value: bumpVersion(d.slots[d.active_slot]?.version), placeholder: '更高候选版本' });
  const reqId = h('input', { value: `req-${Math.random().toString(16).slice(2, 8)}`, placeholder: '请求标识' });
  const fault = h('select', {},
    h('option', { value: '' }, '不断电（正常写入+校验）'),
    h('option', { value: 'candidate_write' }, '故障点：候选写入中断电'),
    h('option', { value: 'digest_check' }, '故障点：摘要校验时断电'),
  );
  const corrupt = h('input', { type: 'checkbox' });
  const submit = h('button', {}, '提交更高版本候选');
  const confirm = h('button', { class: 'secondary' }, '确认切换（原子提交，代次 +1）');
  const confirmFault = h('input', { type: 'checkbox' });

  submit.addEventListener('click', async () => {
    submit.disabled = true;
    try {
      const body = { version: ver.value.trim(), request_id: reqId.value.trim() || undefined };
      if (fault.value) body.fault_point = fault.value;
      if (corrupt.checked) body.corrupt = true;
      const r = await api('POST', `/devices/${encodeURIComponent(d.device_id)}/candidate`, body);
      if (r.outcome === 'power_cut') {
        flash('info', `已在故障点「${r.fault_point}」断电：${r.detail}。请点击“重新打开设备”观察恢复裁决。`);
      } else if (r.outcome === 'verification_failed') {
        flash('err', `摘要不符：清单 ${r.claimed_digest.slice(0, 16)}… 实测 ${r.actual_digest.slice(0, 16)}…，候选已标记 REJECTED 并保留证据，不会引导。`);
      } else {
        flash('ok', `候选已写入槽位 ${r.target_slot} 且摘要校验一致，状态 VERIFIED，等待确认切换。`);
      }
      await refresh(d.device_id);
    } catch (e) {
      if (e.status === 409 && e.data?.error?.code === 'upgrade_conflict') {
        flash('err', `冲突 409：当前代次升级资格已被 ${e.data.error.holder_request} 取得，本请求未改写任何活动版本。`);
      } else flash('err', `候选提交失败：${e.message}`);
      await refresh(d.device_id);
    } finally { submit.disabled = false; }
  });

  confirm.addEventListener('click', async () => {
    confirm.disabled = true;
    try {
      const body = {};
      if (confirmFault.checked) body.fault_point = 'confirm_switch';
      const r = await api('POST', `/devices/${encodeURIComponent(d.device_id)}/confirm`, body);
      if (r.outcome === 'power_cut') {
        flash('info', '已在“确认切换”提交前断电：原子切换记录未提交，重新上电将继续引导旧版本，候选仍未确认。');
      } else {
        flash('ok', `切换已提交：活动槽位 ${r.active_slot}，确认代次 ${r.generation}。旧槽位 SUPERSEDED，不可回退。重开设备视图将显示相同槽位/版本/摘要/代次。`);
      }
      await refresh(d.device_id);
    } catch (e) { flash('err', `确认失败：${e.message}`); }
    finally { confirm.disabled = false; }
  });

  return h('section', { class: 'card' },
    h('h2', {}, '2. 候选升级与确认切换'),
    h('div', { class: 'row' },
      h('div', { class: 'field' }, h('label', {}, '候选版本（须更高）'), ver),
      h('div', { class: 'field' }, h('label', {}, '请求标识 request_id'), reqId),
      h('div', { class: 'field' }, h('label', {}, '故障注入'), fault),
      h('div', { class: 'field' }, h('label', {}, '损坏镜像字节（摘要不符）'), corrupt),
      submit,
    ),
    h('h3', {}, '确认'),
    h('div', { class: 'row' },
      h('label', { style: 'display:flex;gap:6px;align-items:center;font-size:13px' },
        confirmFault, '确认切换提交前模拟断电'),
      confirm,
    ),
    h('p', { class: 'hint' }, '提示：先提交候选 → 选择故障点断电 → 点击顶部“重新打开设备”，即可验证写入中断/校验中断/确认中断三种场景。'),
  );
}

function renderConcurrent() {
  const d = state.device;
  const card = h('section', { class: 'card' },
    h('h2', {}, '3. 并发裁决：两个页面同时提交不同候选'),
    h('p', { class: 'hint' },
      '两个请求使用不同 request_id 在同一时刻提交不同版本；仅一个取得当前代次升级资格，另一个稳定返回 409 且不改写活动版本。'));
  const v1 = h('input', { value: bumpVersion(d.slots[d.active_slot]?.version) });
  const v2 = h('input', { value: bumpVersion(bumpVersion(d.slots[d.active_slot]?.version)) });
  const lanes = [
    { name: '页面甲', ver: v1, id: `req-A-${Math.random().toString(16).slice(2, 6)}` },
    { name: '页面乙', ver: v2, id: `req-B-${Math.random().toString(16).slice(2, 6)}` },
  ];
  const run = h('button', {}, '并发提交两个候选');
  const thenConfirm = h('button', { class: 'secondary' }, '对获胜候选执行确认切换');
  const thenReopen = h('button', { class: 'secondary' }, '断电后重开，核对状态一致');

  run.addEventListener('click', async () => {
    run.disabled = true;
    const payloads = lanes.map((l) => api('POST', `/devices/${encodeURIComponent(d.device_id)}/candidate`, {
      version: l.ver.value.trim(), request_id: l.id,
    }).then((r) => ({ lane: l, ok: true, r })).catch((e) => ({ lane: l, ok: false, e })));
    const results = await Promise.all(payloads);
    state.concurrent = results;
    await refresh(d.device_id);
    const loser = results.find((x) => !x.ok);
    if (loser && loser.e.status === 409) {
      state.flash = { kind: 'info', text: `并发裁决符合预期：${loser.lane.name} 收到稳定 409（资格已被另一页面取得），活动版本未被改写。` };
      render();
    }
    run.disabled = false;
  });

  thenConfirm.addEventListener('click', async () => {
    try {
      const r = await api('POST', `/devices/${encodeURIComponent(d.device_id)}/confirm`, {});
      flash('ok', `获胜候选已确认：槽位 ${r.active_slot}，代次 ${r.generation}。`);
      await refresh(d.device_id);
    } catch (e) { flash('err', e.message); }
  });

  thenReopen.addEventListener('click', async () => {
    try {
      await api('POST', `/devices/${encodeURIComponent(d.device_id)}/power-off`);
      const r = await api('POST', `/devices/${encodeURIComponent(d.device_id)}/power-on`);
      const s = r.device.slots[r.recovery.active_slot];
      flash('ok', `重开后仍为槽位 ${r.recovery.active_slot} / 版本 ${s.version} / 摘要 ${s.digest.slice(0, 16)}… / 代次 ${r.recovery.generation}，与切换后完全一致。`);
      await refresh(d.device_id);
    } catch (e) { flash('err', e.message); }
  });

  const laneEls = lanes.map((l) => h('div', { class: 'lane', id: `lane-${l.id}` },
    h('div', {}, h('b', {}, l.name), `（request_id=${l.id}）`),
    h('div', { class: 'field', style: 'margin:8px 0' }, h('label', {}, '候选版本'), l.ver),
    h('div', { class: 'result' }, state.concurrent
      ? formatConcurrent(state.concurrent.find((x) => x.lane.id === l.id))
      : '等待提交…'),
  ));
  state.concurrent?.forEach((x) => {
    const el = card.querySelector(`#lane-${x.lane.id}`);
    if (el) el.classList.add(x.ok ? 'win' : 'lose');
  });

  card.append(h('div', { class: 'concurrent' }, ...laneEls),
    h('div', { class: 'row', style: 'margin-top:12px' }, run, thenConfirm, thenReopen));
  return card;
}

function formatConcurrent(x) {
  if (!x) return '—';
  if (x.ok) return `获得升级资格 200\noutcome=${x.r.outcome}\ntarget_slot=${x.r.target_slot}\n请求=${x.lane.id}`;
  return `稳定冲突 HTTP ${x.e.status}\ncode=${x.e.data?.error?.code}\n${x.e.data?.error?.detail || x.e.message}\n活动版本未改写`;
}

function renderEvidence() {
  const d = state.device;
  const card = h('section', { class: 'card' },
    h('h2', {}, '4. 持久化诊断证据（追加写，断电不丢）'),
    h('div', { class: 'log' },
      ...(d.evidence?.length ? d.evidence.map((ev) => h('div', { class: 'ev' },
        h('span', { class: 'seq' }, `#${ev.seq}`),
        h('span', { class: 'tag' }, ev.reason),
        `[槽 ${ev.slot}] ${ev.detail}`))
        : [h('span', { class: 'hint' }, '暂无证据')]),
    ),
  );
  return card;
}

function bumpVersion(v) {
  if (!v) return '2.0.0';
  const parts = v.split('.').map((x) => parseInt(x, 10) || 0);
  parts[parts.length - 1] += 1;
  return parts.join('.');
}

refresh().catch((e) => flash('err', `加载失败：${e.message}`));
