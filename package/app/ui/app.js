'use strict';

/* ------------------------------------------------------------------ setup -- */

const BASE = window.FANCONTROL_BASE || '/';
const API = BASE.replace(/\/+$/, '') + '/api/';
const SVGNS = 'http://www.w3.org/2000/svg';

const SOURCE_LABELS = { cpu: 'CPU', gpu: '显卡', hdd: '硬盘' };
const MODE_LABELS = { curve: '温度曲线', manual: '固定转速', auto: 'BIOS 自动' };
const CURVE = { X0: 38, X1: 332, Y0: 12, Y1: 156, TMIN: 20, TMAX: 100, W: 340, H: 190 };

// Mirrors fanconfig.py so a newly added channel draws a usable curve before the
// first save round-trips through the server.
const DEFAULT_CPU_POINTS = [[30, 20], [45, 35], [60, 60], [75, 100]];
const DEFAULT_HDD_POINTS = [[30, 20], [40, 30], [48, 60], [55, 100]];

let status = null;
let cfg = null;
let hardware = { channels: [], disks: [] };
let pollTimer = null;
let toastTimer = null;

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.prototype.slice.call(root.querySelectorAll(sel));

function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function pct(duty) {
  return Math.round((Number(duty) || 0) * 100 / 255) + '%';
}

function fmtTemp(value, digits) {
  return (value === null || value === undefined) ? '–' : Number(value).toFixed(digits == null ? 1 : digits);
}

async function api(path, options) {
  const opts = Object.assign({ cache: 'no-store', headers: { 'Content-Type': 'application/json' } }, options || {});
  const res = await fetch(API + path, opts);
  let data = null;
  try { data = await res.json(); } catch (err) { data = null; }
  if (!res.ok || !data || data.ok === false) {
    throw new Error((data && data.error) || ('请求失败 HTTP ' + res.status));
  }
  return data;
}

function toast(message, isError) {
  const node = $('#toast');
  node.textContent = message;
  node.className = 'toast' + (isError ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.className = 'toast hidden'; }, 2800);
}

function showAlert(message, isError) {
  const node = $('#alert');
  if (!message) { node.className = 'alert hidden'; return; }
  node.textContent = message;
  node.className = 'alert' + (isError ? ' err' : '');
}

/* --------------------------------------------------------- curve helpers -- */

function geometry() {
  const c = CURVE;
  return {
    xOf: (t) => c.X0 + (Math.min(c.TMAX, Math.max(c.TMIN, t)) - c.TMIN) / (c.TMAX - c.TMIN) * (c.X1 - c.X0),
    yOf: (p) => c.Y1 - Math.min(100, Math.max(0, p)) / 100 * (c.Y1 - c.Y0),
    tOf: (x) => c.TMIN + (Math.min(c.X1, Math.max(c.X0, x)) - c.X0) / (c.X1 - c.X0) * (c.TMAX - c.TMIN),
    pOf: (y) => (c.Y1 - Math.min(c.Y1, Math.max(c.Y0, y))) / (c.Y1 - c.Y0) * 100
  };
}

function svgEl(tag, attrs) {
  const node = document.createElementNS(SVGNS, tag);
  for (const key in attrs) node.setAttribute(key, attrs[key]);
  return node;
}

function svgText(x, y, content, anchor, cls) {
  const node = svgEl('text', { x: x, y: y, 'text-anchor': anchor || 'start', class: cls || 'axis-text' });
  node.textContent = content;
  return node;
}

function drawCurve(svg, points, markerTemp) {
  const g = geometry();
  const c = CURVE;
  const pts = points.slice().sort((a, b) => a[0] - b[0]);
  const nodes = [];

  for (let t = c.TMIN; t <= c.TMAX; t += 20) {
    nodes.push(svgEl('line', { class: 'grid-line', x1: g.xOf(t), y1: c.Y0, x2: g.xOf(t), y2: c.Y1 }));
    nodes.push(svgText(g.xOf(t), c.Y1 + 13, t + '°', 'middle'));
  }
  for (let p = 0; p <= 100; p += 25) {
    nodes.push(svgEl('line', { class: 'grid-line', x1: c.X0, y1: g.yOf(p), x2: c.X1, y2: g.yOf(p) }));
    nodes.push(svgText(c.X0 - 6, g.yOf(p) + 3, p + '%', 'end'));
  }

  if (pts.length >= 2) {
    const path = pts.map((pt, i) => (i ? 'L' : 'M') + g.xOf(pt[0]).toFixed(1) + ' ' + g.yOf(pt[1]).toFixed(1)).join(' ');
    nodes.push(svgEl('path', {
      class: 'curve-area',
      d: path + ' L ' + g.xOf(pts[pts.length - 1][0]).toFixed(1) + ' ' + c.Y1 +
         ' L ' + g.xOf(pts[0][0]).toFixed(1) + ' ' + c.Y1 + ' Z'
    }));
    nodes.push(svgEl('path', { class: 'curve-line', d: path }));
  }

  pts.forEach((pt, i) => {
    const dot = svgEl('circle', { class: 'curve-dot', cx: g.xOf(pt[0]), cy: g.yOf(pt[1]), r: 5.5 });
    dot.dataset.i = String(i);
    nodes.push(dot);
  });

  if (markerTemp !== null && markerTemp !== undefined && isFinite(markerTemp)) {
    const x = g.xOf(markerTemp);
    nodes.push(svgEl('line', { class: 'curve-marker', x1: x, y1: c.Y0, x2: x, y2: c.Y1 }));
    nodes.push(svgText(Math.min(x + 4, c.X1 - 26), c.Y0 + 9, Number(markerTemp).toFixed(0) + '°C',
                       'start', 'curve-marker-text'));
  }

  svg.replaceChildren.apply(svg, nodes);
}

function toSvg(svg, event) {
  const rect = svg.getBoundingClientRect();
  const box = svg.viewBox.baseVal;
  return {
    x: (event.clientX - rect.left) / rect.width * box.width,
    y: (event.clientY - rect.top) / rect.height * box.height
  };
}

function attachCurveEditor(svg, fan) {
  const g = geometry();
  let dragIndex = -1;
  const redraw = () => drawCurve(svg, fan.points, liveTempFor(fan));

  svg.addEventListener('pointerdown', (event) => {
    const dot = event.target.closest ? event.target.closest('.curve-dot') : null;
    if (!dot) return;
    dragIndex = Number(dot.dataset.i);
    try { svg.setPointerCapture(event.pointerId); } catch (err) { /* ignore */ }
    event.preventDefault();
  });

  svg.addEventListener('pointermove', (event) => {
    if (dragIndex < 0 || dragIndex >= fan.points.length) return;
    const pos = toSvg(svg, event);
    const pts = fan.points;
    const lower = dragIndex > 0 ? pts[dragIndex - 1][0] + 1 : 0;
    const upper = dragIndex < pts.length - 1 ? pts[dragIndex + 1][0] - 1 : 120;
    const temp = Math.max(lower, Math.min(upper, Math.round(g.tOf(pos.x))));
    pts[dragIndex] = [temp, Math.round(g.pOf(pos.y))];
    redraw();
  });

  const endDrag = (event) => {
    if (dragIndex < 0) return;
    dragIndex = -1;
    try { svg.releasePointerCapture(event.pointerId); } catch (err) { /* ignore */ }
    redraw();
  };
  svg.addEventListener('pointerup', endDrag);
  svg.addEventListener('pointercancel', endDrag);

  svg.addEventListener('dblclick', (event) => {
    if (fan.points.length >= 8) { toast('每条曲线最多 8 个节点', true); return; }
    const pos = toSvg(svg, event);
    fan.points.push([
      Math.max(20, Math.min(119, Math.round(g.tOf(pos.x)))),
      Math.round(g.pOf(pos.y))
    ]);
    fan.points.sort((a, b) => a[0] - b[0]);
    redraw();
    toast('已添加节点，记得保存');
  });

  svg.addEventListener('contextmenu', (event) => {
    const dot = event.target.closest ? event.target.closest('.curve-dot') : null;
    if (!dot) return;
    event.preventDefault();
    if (fan.points.length <= 2) { toast('至少需要保留 2 个节点', true); return; }
    fan.points.splice(Number(dot.dataset.i), 1);
    redraw();
    toast('已删除节点，记得保存');
  });

  redraw();
}

/* ------------------------------------------------------------ rendering -- */

function liveTempFor(fan) {
  const keys = fan.source || [];
  let best = null;
  keys.forEach((key) => {
    const value = status && status.sources && status.sources[key] ? status.sources[key].temperature : null;
    if (value === null || value === undefined) return;
    best = (best === null) ? value : Math.max(best, value);
  });
  return best;
}

function renderSources() {
  const box = $('#sources');
  const enabled = (cfg && cfg.sources) || {};
  box.innerHTML = Object.keys(SOURCE_LABELS).map((key) => {
    const info = (status && status.sources && status.sources[key]) || {};
    return '' +
      '<div class="source' + (enabled[key] ? ' on' : '') + '" data-source="' + key + '">' +
        '<div class="source-head">' +
          '<span class="source-name">' + esc(SOURCE_LABELS[key]) + '</span>' +
          '<label class="switch"><input type="checkbox" data-src-toggle="' + key + '"' +
            (enabled[key] ? ' checked' : '') + '><span class="track"><span class="knob"></span></span></label>' +
        '</div>' +
        '<p class="source-temp"><b data-live="temp">' + fmtTemp(info.temperature, 0) + '</b><small>°C</small></p>' +
        '<p class="source-detail" data-live="detail">' + esc(info.detail || '未启用') + '</p>' +
      '</div>';
  }).join('');

  $$('[data-src-toggle]').forEach((input) => {
    input.addEventListener('change', () => {
      const key = input.dataset.srcToggle;
      cfg.sources[key] = input.checked;
      input.closest('.source').classList.toggle('on', input.checked);
      renderFanSources();
    });
  });
}

function renderDisks() {
  const box = $('#disks');
  const disks = (status && status.devices) || hardware.disks || [];
  if (!disks.length) { box.innerHTML = ''; return; }
  const selected = (cfg.sources && cfg.sources.hdd_devices) || [];
  const allOff = selected.length === 0;

  box.innerHTML = disks.map((disk) => {
    const id = disk.device || disk.id;
    const on = allOff || selected.indexOf(id) >= 0;
    const hot = disk.temperature !== null && disk.temperature >= 50;
    return '' +
      '<label class="disk' + (hot ? ' hot' : '') + '" data-disk="' + esc(id) + '">' +
        '<input type="checkbox" data-disk-toggle="' + esc(id) + '"' + (on ? ' checked' : '') + '>' +
        '<span class="dev">' + esc(id) + '</span>' +
        '<span class="model">' + esc(disk.label || disk.kind || '') + '</span>' +
        '<span class="dt" data-live="temp">' + fmtTemp(disk.temperature, 0) + '°</span>' +
      '</label>';
  }).join('') +
  '<p class="curve-hint" style="grid-column:1/-1">不勾选任何硬盘时，使用全部硬盘中的最高温度。</p>';

  $$('[data-disk-toggle]').forEach((input) => {
    input.addEventListener('change', () => {
      const picked = $$('[data-disk-toggle]').filter((i) => i.checked).map((i) => i.dataset.diskToggle);
      cfg.sources.hdd_devices = picked;
    });
  });
}

/* --------------------------------------------------------------- fan card -- */

function bindRange(card, selector, labelSelector, apply) {
  const input = $(selector, card);
  const label = $(labelSelector, card);
  if (!input) return;
  input.addEventListener('input', () => {
    const value = Number(input.value);
    if (label) label.textContent = pct(value);
    apply(value);
  });
}

function buildFanCard(fan, channelInfo) {
  const card = document.createElement('div');
  card.className = 'fan';
  card.dataset.channel = String(fan.channel);
  const missing = !channelInfo || !channelInfo.present;

  card.innerHTML = '' +
    '<div class="fan-head">' +
      '<input class="fan-name" maxlength="32" value="' + esc(fan.name) + '">' +
      '<span class="badge">CH' + fan.channel + '</span>' +
      (missing ? '<span class="badge dim">无测速信号</span>' : '') +
      '<div class="fan-stats">' +
        '<span class="stat"><b data-live="rpm">–</b>RPM</span>' +
        '<span class="stat"><b data-live="duty">–</b>占空比</span>' +
        '<span class="stat">依据温度 <b data-live="temp">–</b>°C</span>' +
      '</div>' +
      '<button class="fan-remove" title="从配置中移除此通道">移除</button>' +
    '</div>' +
    '<div class="fan-body">' +
      '<div class="fan-controls">' +
        '<div class="field"><label>调速模式</label>' +
          '<select class="fan-mode">' +
            Object.keys(MODE_LABELS).map((key) =>
              '<option value="' + key + '"' + (fan.mode === key ? ' selected' : '') + '>' +
              MODE_LABELS[key] + '</option>').join('') +
          '</select></div>' +
        '<div class="field"><span class="field-label">温度来源</span>' +
          '<div class="checks">' +
            Object.keys(SOURCE_LABELS).map((key) =>
              '<label><input type="checkbox" class="fan-src" value="' + key + '"' +
              ((fan.source || []).indexOf(key) >= 0 ? ' checked' : '') + '>' +
              SOURCE_LABELS[key] + '</label>').join('') +
          '</div></div>' +
        '<div class="field"><label>最低占空比</label><div class="range-row">' +
          '<input type="range" class="fan-min" min="0" max="255" value="' + fan.min_duty + '">' +
          '<b class="fan-min-v">' + pct(fan.min_duty) + '</b></div></div>' +
        '<div class="field"><label>最高占空比</label><div class="range-row">' +
          '<input type="range" class="fan-max" min="0" max="255" value="' + fan.max_duty + '">' +
          '<b class="fan-max-v">' + pct(fan.max_duty) + '</b></div></div>' +
        '<div class="field fan-manual-field"><label>固定占空比</label><div class="range-row">' +
          '<input type="range" class="fan-manual" min="0" max="255" value="' + fan.manual_duty + '">' +
          '<b class="fan-manual-v">' + pct(fan.manual_duty) + '</b></div></div>' +
      '</div>' +
      '<div class="curve-wrap">' +
        '<svg viewBox="0 0 ' + CURVE.W + ' ' + CURVE.H + '"></svg>' +
        '<p class="curve-hint">横轴温度 20–100 °C，纵轴占空比 0–100 %。虚线为当前温度。</p>' +
      '</div>' +
    '</div>';

  const nameInput = $('.fan-name', card);
  nameInput.addEventListener('input', () => { fan.name = nameInput.value; });

  $('.fan-remove', card).addEventListener('click', () => {
    const label = fan.name || ('CH' + fan.channel);
    if (!window.confirm('移除风扇「' + label + '」？\n\n保存后该通道将交还主板 BIOS 自动控制，' +
                        '不再由本应用调速。')) {
      return;
    }
    const index = cfg.fans.indexOf(fan);
    if (index >= 0) cfg.fans.splice(index, 1);
    renderFans();
    renderFanSources();
    toast('已移除 ' + label + '，点击「保存」后生效');
  });

  const modeSelect = $('.fan-mode', card);
  const manualField = $('.fan-manual-field', card);
  const syncMode = () => {
    fan.mode = modeSelect.value;
    manualField.classList.toggle('hidden', fan.mode !== 'manual');
  };
  modeSelect.addEventListener('change', syncMode);
  syncMode();

  $$('.fan-src', card).forEach((input) => {
    input.addEventListener('change', () => {
      let picked = $$('.fan-src', card).filter((i) => i.checked).map((i) => i.value);
      if (!picked.length) { input.checked = true; picked = [input.value]; }
      fan.source = picked;
    });
  });

  bindRange(card, '.fan-min', '.fan-min-v', (v) => { fan.min_duty = v; });
  bindRange(card, '.fan-max', '.fan-max-v', (v) => { fan.max_duty = v; });
  bindRange(card, '.fan-manual', '.fan-manual-v', (v) => { fan.manual_duty = v; });

  attachCurveEditor($('svg', card), fan);
  return card;
}

function renderFans() {
  const box = $('#fans');
  box.innerHTML = '';
  const channels = new Map((hardware.channels || []).map((c) => [c.channel, c]));

  (cfg.fans || []).forEach((fan) => {
    box.appendChild(buildFanCard(fan, channels.get(fan.channel)));
  });

  if (!(cfg.fans || []).length) {
    box.innerHTML = '<p class="curve-hint">未检测到可控风扇。请确认主板使用 Nuvoton NCT67xx 芯片，或点击“刷新硬件”。</p>';
  }

  const used = new Set((cfg.fans || []).map((f) => f.channel));
  const free = (hardware.channels || []).filter((c) => !used.has(c.channel));
  if (free.length) {
    const row = document.createElement('div');
    row.className = 'field';
    row.style.display = 'flex';
    row.style.gap = '8px';
    row.style.alignItems = 'center';
    row.innerHTML = '<select style="max-width:260px">' +
      free.map((c) => '<option value="' + c.channel + '">CH' + c.channel +
        (c.present ? '（' + c.rpm + ' RPM）' : '（无测速信号）') + '</option>').join('') +
      '</select><button class="btn ghost">添加该通道</button>';
    $('button', row).addEventListener('click', () => {
      const channel = Number($('select', row).value);
      cfg.fans.push({
        channel: channel,
        name: 'FAN' + channel,
        mode: 'curve',
        source: ['cpu'],
        points: DEFAULT_CPU_POINTS.map((point) => point.slice()),
        min_duty: 0,
        max_duty: 255,
        manual_duty: 128
      });
      renderFans();
      toast('已添加通道 CH' + channel + '，保存后生效');
    });
    box.appendChild(row);
  }
}

function renderFanSources() {
  // keep the source dropdown semantics visible when a source is switched off
  $$('.fan').forEach((card) => {
    const fan = (cfg.fans || []).find((f) => String(f.channel) === card.dataset.channel);
    if (!fan) return;
    $$('.fan-src', card).forEach((input) => {
      const off = cfg.sources[input.value] === false;
      input.disabled = off;
      input.parentElement.style.opacity = off ? '.45' : '1';
    });
  });
}

function renderSettings() {
  const box = $('#settings');
  const fields = [
    { key: 'interval', label: '控制周期（秒）', min: 1, max: 60, hint: '每次读取温度并调整风扇的间隔' },
    { key: 'hdd_interval', label: '硬盘温度读取间隔（秒）', min: 10, max: 3600, hint: '间隔越长越不容易唤醒休眠硬盘' },
    { key: 'hysteresis', label: '温度迟滞（°C）', min: 0, max: 20, hint: '降温超过该幅度才允许风扇降速，避免反复变速' },
    { key: 'fail_safe_duty', label: '失效保护占空比', min: 0, max: 255, hint: '所有温度源都不可用时使用', duty: true }
  ];
  box.innerHTML = fields.map((f) => {
    const value = cfg[f.key];
    if (f.duty) {
      return '<div class="field"><label>' + f.label + '（' + pct(value) + '）</label>' +
        '<div class="range-row"><input type="range" data-set="' + f.key + '" min="' + f.min +
        '" max="' + f.max + '" value="' + value + '"><b>' + pct(value) + '</b></div>' +
        '<p class="curve-hint">' + f.hint + '</p></div>';
    }
    return '<div class="field"><label>' + f.label + '</label>' +
      '<input type="number" data-set="' + f.key + '" min="' + f.min + '" max="' + f.max +
      '" value="' + value + '"><p class="curve-hint">' + f.hint + '</p></div>';
  }).join('');

  $$('[data-set]').forEach((input) => {
    input.addEventListener('input', () => {
      const key = input.dataset.set;
      const value = Number(input.value);
      cfg[key] = value;
      if (key === 'fail_safe_duty') {
        const label = input.parentElement.querySelector('b');
        if (label) label.textContent = pct(value);
        const title = input.parentElement.parentElement.querySelector('label');
        if (title) title.textContent = '失效保护占空比（' + pct(value) + '）';
      }
    });
  });
}

function renderAll() {
  renderSources();
  renderDisks();
  renderFans();
  renderFanSources();
  renderSettings();
  $('#master').checked = !!cfg.enabled;
}

/* ------------------------------------------------------------ live update -- */

function updateLive() {
  if (!status) return;

  const health = $('#health');
  const subtitle = $('#subtitle');
  if (!status.hardware_ready) {
    health.className = 'pulse err';
    subtitle.textContent = '未找到风扇控制器';
    const others = status.unsupported || [];
    if (others.length) {
      showAlert('没有找到带 PWM 控制的风扇控制器，但检测到其它控制器：' +
        others.map((o) => o.name + '（' + o.hwmon + '）').join('、') +
        '。点右上角「硬件检测」查看详情。', true);
    } else {
      showAlert('本机没有暴露任何可用的风扇控制芯片。可能是主板由 EC 管理风扇，' +
        '或对应的内核驱动未加载。点右上角「硬件检测」查看详情。', true);
    }
  } else if (status.controllable === false) {
    health.className = 'pulse warn';
    subtitle.textContent = '控制器 ' + status.chip + ' 仅支持监控，无法调速';
    showAlert((status.controller && status.controller.note) ||
      '该控制器不支持 PWM 写入，本应用只能读取转速与温度。');
  } else if (status.probing) {
    health.className = 'pulse warn';
    subtitle.textContent = '正在检测风扇通道…';
  } else if (status.degraded) {
    showAlert('');
    health.className = 'pulse warn';
    subtitle.textContent = '部分温度源不可用，已启用失效保护';
  } else if (!status.enabled) {
    showAlert('');
    health.className = 'pulse warn';
    subtitle.textContent = '调速已暂停，风扇由 BIOS 控制';
  } else {
    showAlert('');
    health.className = 'pulse ok';
    subtitle.textContent = status.chip
      ? '运行中 · ' + status.chip + ' · 管理 ' + (status.fans || []).length + ' 个通道'
      : '运行中';
  }

  $$('.source').forEach((card) => {
    const info = (status.sources || {})[card.dataset.source] || {};
    const temp = $('[data-live="temp"]', card);
    const detail = $('[data-live="detail"]', card);
    if (temp) temp.textContent = fmtTemp(info.temperature, 0);
    if (detail) detail.textContent = info.detail || '不可用';
  });

  $$('[data-disk]').forEach((row) => {
    const id = row.dataset.disk;
    const disk = (status.devices || []).find((d) => (d.device || d.id) === id);
    const cell = $('[data-live="temp"]', row);
    if (disk && cell) {
      cell.textContent = fmtTemp(disk.temperature, 0) + '°';
      row.classList.toggle('hot', disk.temperature !== null && disk.temperature >= 50);
    }
  });

  $$('.fan').forEach((card) => {
    const fan = (status.fans || []).find((f) => String(f.channel) === card.dataset.channel);
    if (!fan) return;
    const rpm = $('[data-live="rpm"]', card);
    const duty = $('[data-live="duty"]', card);
    const temp = $('[data-live="temp"]', card);
    if (rpm) rpm.textContent = fan.rpm === null || fan.rpm === undefined ? '–' : fan.rpm;
    if (duty) {
      const shown = fan.duty === null || fan.duty === undefined ? '–' : pct(fan.duty);
      duty.textContent = fan.effective_mode === 'auto' ? 'BIOS' : shown;
    }
    if (temp) temp.textContent = fmtTemp(fan.temperature, 0);

    const model = (cfg.fans || []).find((f) => String(f.channel) === card.dataset.channel);
    if (model) drawCurve($('svg', card), model.points, liveTempFor(model));
  });
}

async function refresh() {
  try {
    status = await api('status');
    updateLive();
  } catch (err) {
    $('#health').className = 'pulse err';
    $('#subtitle').textContent = '后端连接失败：' + err.message;
  }
}

/* ------------------------------------------------------------------ boot -- */

async function loadConfig() {
  cfg = await api('config');
  renderAll();
}

async function save() {
  const button = $('#btn-save');
  button.disabled = true;
  try {
    const result = await api('config', { method: 'POST', body: JSON.stringify(cfg) });
    cfg = result.config;
    renderAll();
    toast('已保存并生效');
    await refresh();
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = false;
  }
}

async function boot() {
  try {
    hardware = await api('hardware');
  } catch (err) {
    hardware = { channels: [], disks: [] };
  }
  try {
    await loadConfig();
  } catch (err) {
    showAlert('无法读取配置：' + err.message, true);
    return;
  }
  await refresh();
  pollTimer = setInterval(refresh, 2000);
  if (status && status.needs_setup) {
    showAlert('首次使用：请确认要管理的风扇通道，然后点击「应用并保存」。');
    openSetup();
  }
}

$('#btn-save').addEventListener('click', save);

$('#btn-reload').addEventListener('click', async () => {
  try {
    await loadConfig();
    await refresh();
    toast('已放弃未保存的修改');
  } catch (err) {
    toast(err.message, true);
  }
});

$('#btn-restore').addEventListener('click', async () => {
  if (!window.confirm('立即让主板 BIOS 接管所有风扇？')) return;
  try {
    await api('action', { method: 'POST', body: JSON.stringify({ action: 'restore-auto' }) });
    await refresh();
    toast('所有风扇已交还 BIOS');
  } catch (err) {
    toast(err.message, true);
  }
});

$('#master').addEventListener('change', async (event) => {
  cfg.enabled = event.target.checked;
  try {
    const result = await api('config', { method: 'POST', body: JSON.stringify(cfg) });
    cfg = result.config;
    toast(cfg.enabled ? '已启用调速' : '已暂停调速，风扇交还 BIOS');
    await refresh();
  } catch (err) {
    toast(err.message, true);
  }
});


/* ----------------------------------------------------------- setup wizard -- */

let setupState = { results: null, selected: null, probing: false, busy: false };

// Channel list for the wizard: everything the controller exposes, merged with
// the most recent probe result when there is one.
function setupChannels() {
  const probed = new Map((setupState.results || []).map((r) => [r.channel, r]));
  return (hardware.channels || [])
    .map((c) => Object.assign({}, c, probed.get(c.channel) || {}))
    .sort((a, b) => a.channel - b.channel);
}

function detectedOf(c) {
  return c.detected !== undefined ? c.detected : (c.rpm || 0) > 0;
}

function openSetup() {
  setupState.selected = null;
  $('#setup').classList.remove('hidden');
  renderSetup();
}

function closeSetup() {
  $('#setup').classList.add('hidden');
}

function defaultSelection(channels) {
  const configured = new Set(((cfg && cfg.fans) || []).map((f) => f.channel));
  const picked = new Set();
  channels.forEach((c) => {
    if (c.controllable === false) return;
    if (configured.has(c.channel) || detectedOf(c)) picked.add(c.channel);
  });
  return picked;
}

function renderSetup() {
  const body = $('#setup-body');
  const note = $('#setup-note');
  const ctrl = (status && status.controller) || hardware.controller || null;
  const channels = setupChannels();
  const controllable = !status || status.controllable !== false;

  if (setupState.selected === null) setupState.selected = defaultSelection(channels);

  if (!channels.length) {
    body.innerHTML =
      '<div class="alert err" style="margin:0">' +
      '没有找到任何带 PWM 控制的风扇控制器。<br><br>' +
      '可能的原因：主板风扇由 EC 管理（笔记本 / 部分迷你主机）、' +
      '对应内核驱动未加载，或芯片型号尚未被支持。' +
      '可以执行 <code>dmesg | grep -iE "nct|it87|f718|w836"</code> 与 ' +
      '<code>cat /sys/class/hwmon/*/name</code> 进一步排查。' +
      '</div>';
    note.textContent = '';
    $('#setup-apply').disabled = true;
    return;
  }

  const others = ((status && status.unsupported) || []);
  const rows = channels.map((c) => {
    const has = detectedOf(c);
    const ok = c.controllable !== false;
    const sel = setupState.selected.has(c.channel);
    const rpm = (c.rpm_before !== undefined)
      ? c.rpm_before + ' → ' + c.rpm_peak
      : (c.rpm === null || c.rpm === undefined ? '–' : String(c.rpm));
    const src = c.temp_sel_label
      ? esc(c.temp_sel_label)
      : (c.temp_sel === null || c.temp_sel === undefined ? '–' : 'temp' + c.temp_sel);
    return '' +
      '<tr class="' + (has ? 'detected' : '') + '">' +
        '<td><input type="checkbox" data-chan="' + c.channel + '"' +
          (sel ? ' checked' : '') + (ok ? '' : ' disabled') + '></td>' +
        '<td><span class="badge">CH' + c.channel + '</span></td>' +
        '<td class="num">' + rpm + '</td>' +
        '<td>' + (has ? '<span class="tag yes">有风扇</span>'
                      : '<span class="tag no">无转速</span>') + '</td>' +
        '<td>' + src + '</td>' +
        '<td class="num">' + (c.duty === null || c.duty === undefined ? '–' : c.duty) + '</td>' +
      '</tr>';
  }).join('');

  body.innerHTML =
    '<div class="hw-card">' +
      '<div class="hw-item">风扇控制器<b>' + esc((status && status.chip) || hardware.chip || '未知') + '</b></div>' +
      '<div class="hw-item">驱动家族<b>' + esc(ctrl ? ctrl.label : '未知') + '</b></div>' +
      '<div class="hw-item">PWM 通道<b>' + channels.length + ' 路</b></div>' +
      '<div class="hw-item">可调速<b>' +
        (controllable ? '<span class="tag yes">是</span>' : '<span class="tag warn">否（仅监控）</span>') +
      '</b></div>' +
    '</div>' +
    (ctrl && ctrl.note ? '<div class="alert">' + esc(ctrl.note) + '</div>' : '') +
    (others.length
      ? '<div class="alert">还检测到其它带 PWM 的控制器，但未被使用：' +
        others.map((o) => esc(o.name + '（' + o.hwmon + '）')).join('、') + '</div>'
      : '') +
    '<table class="chan-table">' +
      '<thead><tr><th style="width:34px"></th><th>通道</th><th>转速 RPM</th>' +
      '<th>风扇</th><th>BIOS 绑定的温度源</th><th>当前占空比</th></tr></thead>' +
      '<tbody>' + rows + '</tbody>' +
    '</table>' +
    '<p class="curve-hint" style="margin-top:10px">' +
      '「无转速」的通道通常没有接风扇，可以不勾选。' +
      '如果某个风扇当时是停转的而被漏判，点下面的「主动检测」逐个通道试转一次即可识别。' +
    '</p>';

  $$('[data-chan]').forEach((input) => {
    input.addEventListener('change', () => {
      const ch = Number(input.dataset.chan);
      if (input.checked) setupState.selected.add(ch);
      else setupState.selected.delete(ch);
      $('#setup-note').textContent = '已选 ' + setupState.selected.size + ' 个通道';
    });
  });

  $('#setup-apply').disabled = !controllable;
  note.textContent = '已选 ' + setupState.selected.size + ' / ' + channels.length + ' 个通道';
}

async function runProbe() {
  if (setupState.probing) return;
  setupState.probing = true;
  const button = $('#setup-probe');
  const note = $('#setup-note');
  button.disabled = true;
  note.innerHTML = '<span class="spin"></span>正在逐个通道全速试转，请稍候（每路约 2 秒）…';
  try {
    const res = await api('probe', { method: 'POST', body: JSON.stringify({ settle: 2 }) });
    setupState.results = res.results || [];
    hardware = res.hardware || hardware;
    // newly found fans join the selection; nothing is ever removed
    if (setupState.selected === null) setupState.selected = new Set();
    setupState.results.forEach((r) => {
      if (r.detected && r.controllable !== false) setupState.selected.add(r.channel);
    });
    renderSetup();
    const found = setupState.results.filter((r) => r.detected).length;
    toast('检测完成：' + found + ' / ' + setupState.results.length + ' 个通道接有风扇');
  } catch (err) {
    note.textContent = '';
    toast('检测失败：' + err.message, true);
  } finally {
    setupState.probing = false;
    button.disabled = false;
  }
}

async function applySetup() {
  if (setupState.busy) return;
  setupState.busy = true;
  const button = $('#setup-apply');
  button.disabled = true;
  try {
    const channels = Array.from(setupState.selected).sort((a, b) => a - b);
    const res = await api('setup', { method: 'POST', body: JSON.stringify({ channels: channels }) });
    cfg = res.config;
    closeSetup();
    renderAll();
    await refresh();
    toast('已应用：管理 ' + channels.length + ' 个风扇通道');
  } catch (err) {
    toast(err.message, true);
  } finally {
    setupState.busy = false;
    button.disabled = false;
  }
}

$('#btn-detect').addEventListener('click', openSetup);

// Closing the wizard without applying means "keep what I have" -- record that
// so it does not reappear on every page load.
async function dismissSetup() {
  closeSetup();
  if (!status || !status.needs_setup) return;
  try {
    await api('action', { method: 'POST', body: JSON.stringify({ action: 'skip-setup' }) });
    await refresh();
  } catch (err) {
    /* not fatal: the wizard simply shows again next time */
  }
}

$('#setup-close').addEventListener('click', dismissSetup);
$('#setup-probe').addEventListener('click', runProbe);
$('#setup-apply').addEventListener('click', applySetup);
$('#setup-select-detected').addEventListener('click', () => {
  const channels = setupChannels();
  setupState.selected = new Set(
    channels.filter((c) => detectedOf(c) && c.controllable !== false).map((c) => c.channel)
  );
  renderSetup();
});
$('#setup').addEventListener('click', (event) => {
  if (event.target === $('#setup')) dismissSetup();
});

boot();
