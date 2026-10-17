const VALID_SCREENS = ['live', 'events', 'stats', 'devices', 'settings'];

let appCfg = { cameras: [] };
let camStatuses = {};
let focusedCamId = 1;
let currentGrid = 4;
let drawToolMode = null;
let isDraggingRoi = false;
let dragStartPt = null;

let cachedEvents = [];
let sideFilterMode = 'all';
let selectedEventIndex = 0;
let selectedEventId = null;  // v16 #R16: الاختيار بالمعرّف (كان بالترتيب ويفتح حدث غلط مع الفلترة)

// v16 #R16: حالة go2rtc ووضع العرض المباشر
let go2rtcState = { running: false, streams: [], port: 1985 };
let go2rtcKnown = false;  // v16 #R17: صارت حالة go2rtc معروفة من أول /api/status
let liveViewMode = 'hq';  // v17 #R25: العرض دائماً عالي الدقة؛ MJPEG احتياط تلقائي فقط
const hqFailed = {};
let statsCharts = {};

let clipFrames = [];
let clipFps = 8;
let clipIndex = 0;
let clipPlaying = false;
let clipTimer = null;
let clipPlaybackSpeed = 1.0;

// v16 #R13: روابط جاهزة لكل مزود متوافق مع OpenAI (الموديل اختاره من «جلب الموديلات»)
const PROVIDER_PRESETS = {
  openai: { url: 'https://api.openai.com/v1', model: 'gpt-4o-mini' },
  openrouter: { url: 'https://openrouter.ai/api/v1', model: '' },
  groq: { url: 'https://api.groq.com/openai/v1', model: '' },
  mistral: { url: 'https://api.mistral.ai/v1', model: '' },
  xai: { url: 'https://api.x.ai/v1', model: '' },
  together: { url: 'https://api.together.xyz/v1', model: '' },
  dashscope: { url: 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1', model: '' },
  deepseek_custom: { url: 'https://api.deepseek.com/v1', model: '' },
  lmstudio: { url: 'http://127.0.0.1:1234/v1', model: '' },
  custom_openai: { url: '', model: '' }
};

const GLOBAL_FIELDS = [
  'power_mode','jpeg_quality',
  'ai_provider','universal_base_url','universal_api_key','universal_model',
  'gemini_api_key','gemini_model','ollama_url','ollama_model',
  'motion_sensitivity','cooldown_seconds','custom_rules',
  'telegram_bot_token','telegram_chat_id',
  'retention_days','max_storage_mb',
  'live_fps','detect_fps','live_max_width','live_jpeg_quality','go2rtc_enabled','go2rtc_url','clip_max_width'
];

let polygonDraftPts = [];
let pendingStorageClearMode = 'events_only';

// v16 #R16: أيقونات Lucide من الـ sprite المضمّن
function ic(name, cls = '') {
  return `<svg class="ico ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
}

function getCategoryIcon(cat) {
  const map = { person: 'user', vehicle: 'car', animal: 'paw-print' };
  const key = map[cat] ? cat : 'other';
  return ic(map[cat] || 'circle-question-mark', 'cat-ico cat-' + key);
}

function evEpoch(ev) {
  const t = Number(ev.detected_ts);
  if (t > 0) return t;
  const p = Date.parse(String(ev.timestamp || '').replace(' ', 'T'));
  return isNaN(p) ? 0 : p / 1000;
}

// v14 fix #12: دالة escapeHtml لتعقيم أي نص قادم من AI أو تيليجرام أو أسماء الكاميرات قبل وضعه في innerHTML أو title
function escapeHtml(val) {
  if (val === null || val === undefined) return '';
  return String(val)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function updateLiveClock() {
  const now = new Date();
  const pad = n => String(n).padStart(2, '0');
  const dStr = `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}  ${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
  const el = document.getElementById('liveClock');
  if (el) el.textContent = dStr;
}
setInterval(updateLiveClock, 1000);
updateLiveClock();

let archiveEvents = null;  // v17 #R21: أحداث الفهرس + الأرشيف لشاشة الأحداث
async function loadEventsArchive() {
  try {
    const d = await (await fetch('/api/events?days=30&limit=400')).json();
    archiveEvents = d.events || [];
    renderAllEventViews();
  } catch (e) {}
}

function findEvent(evId) {
  return (cachedEvents || []).find(e => e.id === evId) || (archiveEvents || []).find(e => e.id === evId);
}

function switchScreen(scr, updateHash = true) {
  if (!VALID_SCREENS.includes(scr)) scr = 'live';
  if (scr === 'stats') setTimeout(loadStatsView, 0);  // v16 #R16
  if (scr === 'events') setTimeout(loadEventsArchive, 0);  // v17 #R21
  VALID_SCREENS.forEach(v => {
    const viewEl = document.getElementById('view_' + v);
    if (viewEl) viewEl.classList.toggle('active', v === scr);
    const navEl = document.getElementById('nav_' + v);
    if (navEl) navEl.classList.toggle('active', v === scr);
  });
  if (updateHash && window.location.hash !== '#' + scr) {
    history.replaceState(null, '', '#' + scr);
  }
}

function handleHashChange() {
  const h = window.location.hash.replace('#', '').trim();
  if (VALID_SCREENS.includes(h)) {
    switchScreen(h, false);
  }
}

function toggleMoreGrid(e) {
  e.stopPropagation();
  document.getElementById('moreGridMenu').classList.toggle('open');
}

function setGridLayout(n, btn) {
  currentGrid = Number(n);
  const grid = document.getElementById('cameraGrid');
  grid.className = 'camera-grid grid-layout-' + currentGrid;
  document.querySelectorAll('.layout-btn, #btnMoreGrid').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  else {
    const b = document.getElementById('gridBtn_' + currentGrid);
    if (b) b.classList.add('active');
  }
  document.getElementById('moreGridMenu').classList.remove('open');
  renderMatrixCells();
}

function toggleInspector() {
  const drawer = document.getElementById('inspectorDrawer');
  const btn = document.getElementById('btnInspectorToggle');
  const isHidden = drawer.classList.toggle('hidden');
  if (btn) btn.classList.toggle('active', !isHidden);
}

function toggleEventsPanel() {
  const panel = document.getElementById('eventsSidePanel');
  const btn = document.getElementById('btnEventsPanelToggle');
  const isClosed = panel.classList.toggle('closed');
  if (btn) btn.classList.toggle('active', !isClosed);
}

function showBannerNotice(txt) {
  const b = document.getElementById('roiStatusBanner');
  if (!b) return;
  if (!txt) {
    b.style.display = 'none';
    return;
  }
  b.textContent = txt;
  b.style.display = 'inline-flex';
}

// v16 #10: رسم المنطقة والخط بدون تداخل: النقطة تنضاف بالنقر (مو بالضغط)، بدون تكرار، وبدون أضلاع متقاطعة
let draftCamId = null;
let zoneClickStart = null;
window.__roiRedraw = window.__roiRedraw || {};
// v16 #11: حالة رسم الخط (نقرة بداية + نقرة نهاية أو سحب) ثم كلك يمين للتثبيت
let lineStage = 0;            // 0 لا شي، 1 تحددت البداية، 2 الخط جاهز بانتظار التثبيت
let lineDraftStart = null;
let lineDraftEnd = null;
let lineDraftCamId = null;
let linePress = null;
let lineStartedThisPress = false;
window.__lineRedraw = window.__lineRedraw || {};

function resetLineDraft() {
  const fn = window.__lineRedraw[lineDraftCamId];
  lineStage = 0;
  lineDraftStart = null;
  lineDraftEnd = null;
  linePress = null;
  lineStartedThisPress = false;
  if (fn) fn();
  lineDraftCamId = null;
}

async function saveDraftLine(camId) {
  if (lineStage !== 2 || !lineDraftStart || !lineDraftEnd) return;
  const startPt = lineDraftStart;
  const endPt = lineDraftEnd;
  const cam = (appCfg.cameras || []).find(c => Number(c.id) === Number(camId));
  const curRoi = (cam && cam.roi) ? { ...cam.roi } : {
    line_enabled: false, lx1: 0.2, ly1: 0.55, lx2: 0.8, ly2: 0.55,
    zone_enabled: false, zx1: 0.15, zy1: 0.15, zx2: 0.85, zy2: 0.85, combo_mode: 'both'
  };
  curRoi.line_enabled = true;
  curRoi.lx1 = Number(startPt.normX.toFixed(4));
  curRoi.ly1 = Number(startPt.normY.toFixed(4));
  curRoi.lx2 = Number(endPt.normX.toFixed(4));
  curRoi.ly2 = Number(endPt.normY.toFixed(4));
  if (cam) cam.roi = curRoi;

  await fetch('/api/save_roi', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ cam_id: camId, roi: curRoi })
  });

  activateRoiDraw(null);
  showBannerNotice('✓ تم تثبيت خط العبور على CAM-' + String(camId).padStart(2, '0'));
  setTimeout(() => showBannerNotice(''), 2500);
  renderDevicesView();
}

function clearRoiPreviews() {
  document.querySelectorAll('.roi-canvas').forEach(cv => {
    const c = cv.getContext('2d');
    c.clearRect(0, 0, cv.width, cv.height);
  });
}

function roiSegmentsCross(a, b, c, d) {
  const o = (p, q, r) => (q.normX - p.normX) * (r.normY - p.normY) - (q.normY - p.normY) * (r.normX - p.normX);
  const d1 = o(a, b, c), d2 = o(a, b, d), d3 = o(c, d, a), d4 = o(c, d, b);
  if (d1 === 0 || d2 === 0 || d3 === 0 || d4 === 0) return false;
  return ((d1 > 0) !== (d2 > 0)) && ((d3 > 0) !== (d4 > 0));
}

function polygonSelfIntersects(pts) {
  const n = pts.length;
  for (let i = 0; i < n; i++) {
    const a = pts[i], b = pts[(i + 1) % n];
    for (let j = i + 2; j < n; j++) {
      if (i === 0 && j === n - 1) continue;
      const c = pts[j], d = pts[(j + 1) % n];
      if (roiSegmentsCross(a, b, c, d)) return true;
    }
  }
  return false;
}

function sortPolygonAroundCenter(pts) {
  const cx = pts.reduce((s, p) => s + p.normX, 0) / pts.length;
  const cy = pts.reduce((s, p) => s + p.normY, 0) / pts.length;
  return pts.slice().sort((p, q) => Math.atan2(p.normY - cy, p.normX - cx) - Math.atan2(q.normY - cy, q.normX - cx));
}

function polygonArea(pts) {
  let a = 0;
  for (let i = 0; i < pts.length; i++) {
    const p = pts[i], q = pts[(i + 1) % pts.length];
    a += p.normX * q.normY - q.normX * p.normY;
  }
  return Math.abs(a) / 2;
}

function redrawDraftPreview() {
  const fn = window.__roiRedraw[draftCamId];
  if (fn) fn();
}

function isPolyTool() {
  return drawToolMode === 'zone' || drawToolMode === 'mask';
}

function activateRoiDraw(mode) {
  if (isPolyTool() && mode === drawToolMode && polygonDraftPts.length >= 3) {
    finishPolygonZoneDraw(draftCamId || focusedCamId);
    return;
  }
  vertexDrag = null;
  drawToolMode = (drawToolMode === mode) ? null : mode;
  polygonDraftPts = [];
  draftCamId = null;
  zoneClickStart = null;
  isDraggingRoi = false;
  dragStartPt = null;
  lineStage = 0;
  lineDraftStart = null;
  lineDraftEnd = null;
  lineDraftCamId = null;
  linePress = null;
  clearRoiPreviews();
  document.getElementById('drawLineBtn').classList.toggle('active', drawToolMode === 'line');
  document.getElementById('drawZoneBtn').classList.toggle('active', drawToolMode === 'zone');
  const mb = document.getElementById('drawMaskBtn');
  if (mb) mb.classList.toggle('active', drawToolMode === 'mask');
  if (drawToolMode === 'line') {
    showBannerNotice('خط العبور: انقر بداية الخط ثم نهايته، ثم «تثبيت» • Esc للإلغاء');
  } else if (drawToolMode === 'zone') {
    showBannerNotice('منطقة الكشف: انقر زوايا المنطقة بالترتيب ثم «تثبيت». ولتعديل منطقة موجودة اسحب نقاطها مباشرة');
  } else if (drawToolMode === 'mask') {
    showBannerNotice('منطقة استبعاد: ارسم حول الشجرة أو الشارع اللي يسبب إنذارات كاذبة ثم «تثبيت». تقدر ترسم أكثر من منطقة');
  } else {
    showBannerNotice('');
  }
  updateDrawToolbar();
  // v16 #R16: الرسم يحتاج طبقة التحليل (MJPEG) للكاميرا المحددة
  if (tilesNeedRerender()) renderMatrixCells();
  document.querySelectorAll('.roi-canvas').forEach(cv => {
    cv.classList.toggle('drawing-enabled', drawToolMode !== null);
  });
  redrawExistingShapes();
}

// ===================== v17 #R20: شريط أزرار الرسم فوق الكاميرا (بديل كلك يمين/الأزرار) =====================
function drawToolbarCam() {
  if (isPolyTool()) return draftCamId || focusedCamId;
  if (drawToolMode === 'line') return lineDraftCamId || focusedCamId;
  return null;
}

function updateDrawToolbar() {
  document.querySelectorAll('.draw-toolbar').forEach(el => el.remove());
  const cid = drawToolbarCam();
  if (!cid) return;
  const tile = document.getElementById('camTile_' + cid);
  if (!tile) return;
  const bar = document.createElement('div');
  bar.className = 'draw-toolbar';
  bar.onclick = e => e.stopPropagation();
  bar.onmousedown = e => e.stopPropagation();
  const title = drawToolMode === 'line' ? 'خط العبور' : (drawToolMode === 'mask' ? 'منطقة استبعاد' : 'منطقة الكشف');
  const count = isPolyTool() ? `${polygonDraftPts.length} نقطة` : (lineStage === 2 ? 'جاهز' : (lineStage === 1 ? 'حدد النهاية' : 'حدد البداية'));
  const canSave = isPolyTool() ? polygonDraftPts.length >= 3 : lineStage === 2;
  bar.innerHTML = `<span class="dt-title">${title} • <span class="mono">${count}</span></span>
    <button class="dt-btn primary" ${canSave ? '' : 'disabled'} onclick="drawToolbarAction('save')">${ic('check')} تثبيت</button>
    <button class="dt-btn" onclick="drawToolbarAction('undo')">تراجع</button>
    <button class="dt-btn" onclick="drawToolbarAction('cancel')">${ic('x')} إلغاء</button>`;
  tile.appendChild(bar);
}

function drawToolbarAction(act) {
  if (act === 'cancel') {
    activateRoiDraw(null);
    showBannerNotice('تم إلغاء الرسم');
    setTimeout(() => showBannerNotice(''), 1500);
    return;
  }
  if (act === 'undo') {
    if (isPolyTool() && polygonDraftPts.length) {
      polygonDraftPts.pop();
      if (!polygonDraftPts.length) draftCamId = null;
      redrawDraftPreview();
      redrawExistingShapes();
    } else if (drawToolMode === 'line') {
      resetLineDraft();
    }
    updateDrawToolbar();
    return;
  }
  if (act === 'save') {
    if (isPolyTool() && polygonDraftPts.length >= 3) finishPolygonZoneDraw(draftCamId || focusedCamId);
    else if (drawToolMode === 'line' && lineStage === 2) saveDraftLine(lineDraftCamId);
  }
}

// ===================== v17 #R20: تعديل المنطقة بسحب نقاطها + عرض الأشكال الموجودة أثناء الرسم =====================
let vertexDrag = null;

function polysForTool(cam) {
  const roi = (cam && cam.roi) || {};
  if (drawToolMode === 'zone') return (roi.zone_enabled && roi.polygon && roi.polygon.length >= 3) ? [roi.polygon] : [];
  if (drawToolMode === 'mask') return (roi.masks || []).filter(m => m && m.length >= 3);
  return [];
}

function redrawExistingShapes() {
  document.querySelectorAll('.roi-canvas').forEach(cv => {
    const cid = Number(cv.id.replace('roiCanvas_', ''));
    // v17 #R25: الأشكال ونقاط التعديل تنرسم فوق الفيديو عالي الدقة أيضاً (drawHqOverlay يتوقف أثناء الرسم للكاميرا المحددة)
    const ctx = cv.getContext('2d');
    cv.width = cv.clientWidth;
    cv.height = cv.clientHeight;
    ctx.clearRect(0, 0, cv.width, cv.height);
    if (!isPolyTool() || (polygonDraftPts.length && draftCamId === cid)) return;
    const cam = (appCfg.cameras || []).find(c => Number(c.id) === cid);
    const box = getVideoRenderBox(cv, document.getElementById('cellImg_' + cid));
    polysForTool(cam).forEach(poly => {
      ctx.beginPath();
      poly.forEach((p, i) => { const x = box.left + p[0] * box.width, y = box.top + p[1] * box.height; i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.closePath();
      ctx.strokeStyle = drawToolMode === 'mask' ? '#CFCFCF' : '#E7C873';
      ctx.lineWidth = 2;
      ctx.setLineDash(drawToolMode === 'mask' ? [6, 4] : []);
      ctx.stroke();
      ctx.setLineDash([]);
      poly.forEach(p => {
        const x = box.left + p[0] * box.width, y = box.top + p[1] * box.height;
        ctx.beginPath(); ctx.arc(x, y, 6, 0, Math.PI * 2);
        ctx.fillStyle = '#FFFFFF'; ctx.fill();
        ctx.strokeStyle = '#1C1A16'; ctx.lineWidth = 1.5; ctx.stroke();
      });
    });
  });
}

function hitVertex(cam, canvas, e) {
  const box = getVideoRenderBox(canvas, document.getElementById('cellImg_' + cam.id));
  const r = canvas.getBoundingClientRect();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  const polys = polysForTool(cam);
  for (let pi = 0; pi < polys.length; pi++) {
    for (let vi = 0; vi < polys[pi].length; vi++) {
      const x = box.left + polys[pi][vi][0] * box.width, y = box.top + polys[pi][vi][1] * box.height;
      if (Math.hypot(mx - x, my - y) <= 10) return { pi, vi };
    }
  }
  return null;
}

async function saveEditedPolygon(cam) {
  const roi = { ...cam.roi };
  await fetch('/api/save_roi', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ cam_id: cam.id, roi }) });
  showBannerNotice('✓ تم تعديل ' + (drawToolMode === 'mask' ? 'منطقة الاستبعاد' : 'منطقة الكشف') + ' على CAM-' + String(cam.id).padStart(2, '0'));
  setTimeout(() => showBannerNotice(''), 2000);
}

document.addEventListener('keydown', (e) => {
  if (!drawToolMode) return;
  const tag = (e.target && e.target.tagName) || '';
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
  if (e.key === 'Escape') {
    e.preventDefault();
    activateRoiDraw(null);
    showBannerNotice('تم إلغاء الرسم');
    setTimeout(() => showBannerNotice(''), 1500);
  } else if (isPolyTool() && (e.key === 'Backspace' || ((e.key === 'z' || e.key === 'Z') && (e.ctrlKey || e.metaKey)))) {
    e.preventDefault();
    if (polygonDraftPts.length) {
      polygonDraftPts.pop();
      redrawDraftPreview();
      if (!polygonDraftPts.length) draftCamId = null;
      showBannerNotice('نقاط المنطقة: ' + polygonDraftPts.length);
    }
  } else if (isPolyTool() && e.key === 'Enter' && polygonDraftPts.length >= 3) {
    e.preventDefault();
    finishPolygonZoneDraw(draftCamId || focusedCamId);
  } else if (drawToolMode === 'line' && e.key === 'Enter' && lineStage === 2) {
    e.preventDefault();
    saveDraftLine(lineDraftCamId);
  } else if (drawToolMode === 'line' && e.key === 'Backspace' && lineStage > 0) {
    e.preventDefault();
    resetLineDraft();
    showBannerNotice('انمسح الخط المؤقت. انقر بداية الخط من جديد');
  }
});

async function finishPolygonZoneDraw(camId) {
  if (polygonDraftPts.length < 3) {
    showBannerNotice('المنطقة تحتاج 3 نقاط على الأقل');
    return;
  }
  // v16 #10: نرفض المنطقة المتقاطعة (أو نرتّبها تلقائياً لو ينفع) والمنطقة الصغيرة جداً
  let pts = polygonDraftPts.slice();
  let autoSorted = false;
  if (polygonSelfIntersects(pts)) {
    const sorted = sortPolygonAroundCenter(pts);
    if (!polygonSelfIntersects(sorted)) {
      pts = sorted;
      autoSorted = true;
    } else {
      showBannerNotice('✕ أضلاع المنطقة متقاطعة. امسح آخر نقطة (زر يمين) أو Esc وارسمها بالترتيب حول المنطقة');
      return;
    }
  }
  if (polygonArea(pts) < 0.002) {
    showBannerNotice('✕ المنطقة صغيرة جداً، ارسمها أكبر');
    return;
  }
  polygonDraftPts = pts;
  const isMask = drawToolMode === 'mask';  // v17 #R20
  const cam = (appCfg.cameras || []).find(c => Number(c.id) === Number(camId));
  const curRoi = (cam && cam.roi) ? { ...cam.roi } : {
    line_enabled: false, lx1: 0.2, ly1: 0.55, lx2: 0.8, ly2: 0.55,
    zone_enabled: false, zx1: 0.15, zy1: 0.15, zx2: 0.85, zy2: 0.85, combo_mode: 'both'
  };
  const newPoly = polygonDraftPts.map(p => [Number(p.normX.toFixed(4)), Number(p.normY.toFixed(4))]);
  if (isMask) {
    curRoi.masks = [...(curRoi.masks || []), newPoly];
  } else {
    curRoi.zone_enabled = true;
    curRoi.polygon = newPoly;
    const xs = curRoi.polygon.map(p => p[0]);
    const ys = curRoi.polygon.map(p => p[1]);
    curRoi.zx1 = Math.min(...xs);
    curRoi.zy1 = Math.min(...ys);
    curRoi.zx2 = Math.max(...xs);
    curRoi.zy2 = Math.max(...ys);
  }
  if (cam) cam.roi = curRoi;

  polygonDraftPts = [];
  draftCamId = null;
  zoneClickStart = null;
  drawToolMode = null;
  document.getElementById('drawZoneBtn').classList.remove('active');
  document.getElementById('drawLineBtn').classList.remove('active');
  if (document.getElementById('drawMaskBtn')) document.getElementById('drawMaskBtn').classList.remove('active');
  updateDrawToolbar();
  document.querySelectorAll('.roi-canvas').forEach(cv => {
    cv.classList.remove('drawing-enabled');
    const cctx = cv.getContext('2d');
    cctx.clearRect(0, 0, cv.width, cv.height);
  });

  await fetch('/api/save_roi', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ cam_id: camId, roi: curRoi })
  });
  showBannerNotice('✓ تم حفظ ' + (isMask ? 'منطقة الاستبعاد' : 'منطقة الكشف') + ' على CAM-' + String(camId).padStart(2, '0') + (autoSorted ? ' (رتّبت النقاط تلقائياً)' : ''));
  renderMatrixCells();  // v17 #R20: الخانة ترجع لعرضها عالي الدقة بعد الرسم
  setTimeout(() => showBannerNotice(''), 3000);
  renderDevicesView();
}

async function clearCameraRoi(which = 'all') {
  drawToolMode = null;
  polygonDraftPts = [];
  document.getElementById('drawLineBtn').classList.remove('active');
  document.getElementById('drawZoneBtn').classList.remove('active');
  const cam = (appCfg.cameras || []).find(c => Number(c.id) === focusedCamId);
  const curRoi = (cam && cam.roi) ? { ...cam.roi } : {
    line_enabled: false, lx1: 0.2, ly1: 0.55, lx2: 0.8, ly2: 0.55,
    zone_enabled: false, zx1: 0.15, zy1: 0.15, zx2: 0.85, zy2: 0.85, polygon: [], combo_mode: 'both'
  };
  if (which === 'line' || which === 'all') curRoi.line_enabled = false;
  if (which === 'zone' || which === 'all') {
    curRoi.zone_enabled = false;
    curRoi.polygon = [];
  }
  if (which === 'mask' || which === 'all') curRoi.masks = [];
  if (cam) cam.roi = curRoi;

  await fetch('/api/save_roi', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ cam_id: focusedCamId, roi: curRoi })
  });
  showBannerNotice('✓ تم مسح التحديد الهندسي للكاميرا CAM-' + String(focusedCamId).padStart(2, '0'));
  setTimeout(() => showBannerNotice(''), 2500);
  renderDevicesView();
}

function getVideoRenderBox(canvas, img) {
  const cw = canvas.clientWidth;
  const ch = canvas.clientHeight;
  // v17 #R20: لو الخانة تعرض فيديو go2rtc (مو صورة MJPEG) ناخذ أبعاد الفيديو، وإلا الإحداثيات تنحرف
  let natW = img && img.naturalWidth, natH = img && img.naturalHeight;
  if (!natW || !natH) {
    const vid = canvas.parentElement ? canvas.parentElement.querySelector('video') : null;
    if (vid && vid.videoWidth) { natW = vid.videoWidth; natH = vid.videoHeight; }
  }
  if (!natW || !natH) {
    return { left: 0, top: 0, width: cw, height: ch };
  }
  const scale = Math.min(cw / natW, ch / natH);
  const rw = natW * scale;
  const rh = natH * scale;
  const rx = (cw - rw) / 2.0;
  const ry = (ch - rh) / 2.0;
  return { left: rx, top: ry, width: rw, height: rh };
}

function attachLaserCanvasRoiEvents(canvas, camId) {
  const ctx = canvas.getContext('2d');
  const imgEl = document.getElementById('cellImg_' + camId);

  function getExactVideoNormalized(e) {
    const cRect = canvas.getBoundingClientRect();
    const clickX = e.clientX - cRect.left;
    const clickY = e.clientY - cRect.top;
    const vBox = getVideoRenderBox(canvas, imgEl);
    const normX = Math.max(0.0, Math.min(1.0, (clickX - vBox.left) / vBox.width));
    const normY = Math.max(0.0, Math.min(1.0, (clickY - vBox.top) / vBox.height));
    return { normX, normY };
  }

  function toPx(pt, vBox) {
    return [vBox.left + pt.normX * vBox.width, vBox.top + pt.normY * vBox.height];
  }

  // v16 #11: المنطقة تنرسم من النقاط المثبتة بس، والماوس يظهر كخط متقطع خفيف (ما عاد الشكل يلحق الماوس)
  function redrawPolygonPreview(hoverPt = null) {
    canvas.width = canvas.clientWidth;
    canvas.height = canvas.clientHeight;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!polygonDraftPts.length || draftCamId !== camId) return;
    const vBox = getVideoRenderBox(canvas, imgEl);
    ctx.lineWidth = 2;
    ctx.strokeStyle = '#E7C873';
    ctx.fillStyle = 'rgba(231, 200, 115, 0.18)';
    ctx.beginPath();
    polygonDraftPts.forEach((pt, idx) => {
      const [x, y] = toPx(pt, vBox);
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    if (polygonDraftPts.length >= 3) {
      ctx.closePath();
      ctx.fill();
    }
    ctx.stroke();
    if (hoverPt) {
      const [lx, ly] = toPx(polygonDraftPts[polygonDraftPts.length - 1], vBox);
      const [hx, hy] = toPx(hoverPt, vBox);
      ctx.save();
      ctx.setLineDash([6, 5]);
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.85)';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(lx, ly);
      ctx.lineTo(hx, hy);
      ctx.stroke();
      ctx.restore();
    }
    polygonDraftPts.forEach((pt, idx) => {
      const [x, y] = toPx(pt, vBox);
      ctx.beginPath();
      ctx.arc(x, y, idx === 0 ? 6 : 4.5, 0, Math.PI * 2);
      ctx.fillStyle = idx === 0 ? '#C9A44A' : '#FFFFFF';
      ctx.fill();
      ctx.strokeStyle = '#1C1A16';
      ctx.lineWidth = 1;
      ctx.stroke();
    });
  }
  window.__roiRedraw[camId] = () => redrawPolygonPreview();

  function redrawLinePreview(hoverPt = null) {
    canvas.width = canvas.clientWidth;
    canvas.height = canvas.clientHeight;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!lineDraftStart || lineDraftCamId !== camId) return;
    const vBox = getVideoRenderBox(canvas, imgEl);
    const endPt = lineDraftEnd || hoverPt;
    const [sx, sy] = toPx(lineDraftStart, vBox);
    if (endPt) {
      const [ex, ey] = toPx(endPt, vBox);
      ctx.save();
      if (!lineDraftEnd) ctx.setLineDash([8, 6]);
      ctx.strokeStyle = lineDraftEnd ? '#C9A44A' : '#FFFFFF';
      ctx.lineWidth = lineDraftEnd ? 3.5 : 2;
      ctx.beginPath();
      ctx.moveTo(sx, sy);
      ctx.lineTo(ex, ey);
      ctx.stroke();
      ctx.restore();
      ctx.beginPath();
      ctx.arc(ex, ey, 5, 0, Math.PI * 2);
      ctx.fillStyle = '#FFFFFF';
      ctx.fill();
    }
    ctx.beginPath();
    ctx.arc(sx, sy, 6, 0, Math.PI * 2);
    ctx.fillStyle = '#C9A44A';
    ctx.fill();
  }
  window.__lineRedraw[camId] = () => redrawLinePreview();

  // v16 #11: كلك يمين = تثبيت المنطقة أو الخط
  canvas.addEventListener('contextmenu', (e) => {
    if (!drawToolMode) return;
    e.preventDefault();
    if (isPolyTool()) {
      if (draftCamId === camId && polygonDraftPts.length >= 3) {
        finishPolygonZoneDraw(camId);
      } else {
        showBannerNotice('حط 3 نقاط على الأقل بالزر الأيسر، بعدها كلك يمين للتثبيت');
      }
      return;
    }
    if (drawToolMode === 'line') {
      if (lineStage === 2 && lineDraftCamId === camId) {
        saveDraftLine(camId);
      } else {
        showBannerNotice('حدد بداية الخط ونهايته بالزر الأيسر، بعدها كلك يمين للتثبيت');
      }
    }
  });

  canvas.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    selectCameraById(camId);
    if (!drawToolMode) return;
    if (isPolyTool()) {
      // v17 #R20: سحب نقطة موجودة لتعديل المنطقة (لما ما فيه رسم جاري)
      if (!polygonDraftPts.length) {
        const cam = (appCfg.cameras || []).find(c => Number(c.id) === camId);
        const hit = cam ? hitVertex(cam, canvas, e) : null;
        if (hit) {
          vertexDrag = { cam, pi: hit.pi, vi: hit.vi };
          zoneClickStart = null;
          e.preventDefault();
          return;
        }
      }
      // النقطة تنضاف عند الإفلات لو ما صار سحب
      if (polygonDraftPts.length && draftCamId !== null && draftCamId !== camId) {
        showBannerNotice('أنت ترسم منطقة على CAM-' + String(draftCamId).padStart(2, '0') + '. ثبّتها بكلك يمين أو اضغط Esc أول');
        zoneClickStart = null;
        return;
      }
      zoneClickStart = { x: e.clientX, y: e.clientY };
      return;
    }
    if (drawToolMode === 'line') {
      const pt = getExactVideoNormalized(e);
      linePress = { x: e.clientX, y: e.clientY };
      if (lineStage !== 1 || lineDraftCamId !== camId) {
        if (lineDraftCamId !== null && lineDraftCamId !== camId) resetLineDraft();
        lineDraftStart = pt;
        lineDraftEnd = null;
        lineStage = 1;
        lineDraftCamId = camId;
        lineStartedThisPress = true;
        redrawLinePreview();
        updateDrawToolbar();
      } else {
        lineStartedThisPress = false;
      }
    }
  });

  canvas.addEventListener('dblclick', (e) => {
    if (isPolyTool() && polygonDraftPts.length >= 3 && draftCamId === camId) {
      e.stopPropagation();
      e.preventDefault();
      finishPolygonZoneDraw(camId);
    }
  });

  canvas.addEventListener('mousemove', (e) => {
    if (vertexDrag && Number(vertexDrag.cam.id) === camId) {
      const pt = getExactVideoNormalized(e);
      const polys = polysForTool(vertexDrag.cam);
      if (polys[vertexDrag.pi]) {
        polys[vertexDrag.pi][vertexDrag.vi] = [Number(pt.normX.toFixed(4)), Number(pt.normY.toFixed(4))];
        redrawExistingShapes();
      }
      return;
    }
    if (isPolyTool()) {
      if (polygonDraftPts.length > 0 && draftCamId === camId) {
        redrawPolygonPreview(getExactVideoNormalized(e));
      } else if (!polygonDraftPts.length) {
        const cam = (appCfg.cameras || []).find(c => Number(c.id) === camId);
        canvas.style.cursor = (cam && hitVertex(cam, canvas, e)) ? 'grab' : 'crosshair';
      }
      return;
    }
    if (drawToolMode === 'line' && lineStage === 1 && lineDraftCamId === camId) {
      redrawLinePreview(getExactVideoNormalized(e));
    }
  });

  canvas.addEventListener('mouseup', (e) => {
    if (e.button !== 0) return;
    if (vertexDrag) {
      const cam = vertexDrag.cam;
      vertexDrag = null;
      if (drawToolMode === 'zone' && cam.roi && cam.roi.polygon) {
        const xs = cam.roi.polygon.map(p => p[0]), ys = cam.roi.polygon.map(p => p[1]);
        cam.roi.zx1 = Math.min(...xs); cam.roi.zy1 = Math.min(...ys); cam.roi.zx2 = Math.max(...xs); cam.roi.zy2 = Math.max(...ys);
      }
      saveEditedPolygon(cam);
      return;
    }
    if (isPolyTool()) {
      // نقرة = نقطة. السحب في وضع المنطقة ما يضيف نقاط
      if (!zoneClickStart) return;
      const moved = Math.hypot(e.clientX - zoneClickStart.x, e.clientY - zoneClickStart.y);
      zoneClickStart = null;
      if (moved > 8) {
        showBannerNotice('في وضع المنطقة انقر نقطة نقطة بدون سحب. لرسم خط العبور اضغط زر «خط العبور» أول');
        return;
      }
      const pt = getExactVideoNormalized(e);
      const last = polygonDraftPts[polygonDraftPts.length - 1];
      if (last && Math.hypot(pt.normX - last.normX, pt.normY - last.normY) < 0.015) return;
      if (polygonDraftPts.length === 0) draftCamId = camId;
      polygonDraftPts.push(pt);
      redrawPolygonPreview();
      updateDrawToolbar();
      showBannerNotice('نقاط المنطقة: ' + polygonDraftPts.length + (polygonDraftPts.length >= 3 ? ' — اضغط «تثبيت» أو كلك يمين' : ' — كمّل النقاط بالزر الأيسر'));
      return;
    }
    if (drawToolMode !== 'line' || lineStage !== 1 || lineDraftCamId !== camId || !linePress) return;
    const moved = Math.hypot(e.clientX - linePress.x, e.clientY - linePress.y);
    linePress = null;
    if (lineStartedThisPress && moved <= 8) {
      showBannerNotice('تحددت بداية الخط. انقر نهاية الخط');
      return;
    }
    const endPt = getExactVideoNormalized(e);
    if (Math.hypot(endPt.normX - lineDraftStart.normX, endPt.normY - lineDraftStart.normY) < 0.04) {
      showBannerNotice('الخط قصير جداً. انقر نهاية أبعد');
      if (lineStartedThisPress) resetLineDraft();
      return;
    }
    lineDraftEnd = endPt;
    lineStage = 2;
    redrawLinePreview();
    updateDrawToolbar();
    showBannerNotice('اضغط «تثبيت» (أو كلك يمين) لحفظ الخط • انقر من جديد لإعادة رسمه • Esc للإلغاء');
  });
}

function renderMatrixCells() {
  const mat = document.getElementById('cameraGrid');
  const count = Number(currentGrid);
  const cams = appCfg.cameras || [];

  let cellsToShow = [];
  if (count === 1) {
    const found = cams.find(c => Number(c.id) === focusedCamId) || cams[0] || { id: 1, name: 'خانة فارغة', empty: true };
    cellsToShow = [found];
  } else {
    for (let i = 1; i <= count; i++) {
      const found = cams.find(c => Number(c.id) === i) || { id: i, name: 'كاميرا ' + i, empty: true };
      cellsToShow.push(found);
    }
  }

  // v16 #R17: نقفل بث MJPEG القديم صراحةً قبل إزالة العنصر، وإلا يظل الاتصال مفتوح ويحسب "مشاهد" ويستهلك معالج
  mat.querySelectorAll('img.cam-stream-img').forEach(img => { img.src = ''; });
  mat.querySelectorAll('cctv-rtc').forEach(el => { try { el.ondisconnect(); } catch (e) {} });
  const hqPending = (liveViewMode === 'hq' && !go2rtcKnown);  // أول تحميل: ننتظر حالة go2rtc بدل ما نفتح MJPEG ثم نقفله

  mat.innerHTML = cellsToShow.map(c => {
    const cid = Number(c.id);
    const code = 'CAM-' + String(cid).padStart(2, '0');
    const isSel = (cid === focusedCamId);
    const st = camStatuses[cid] || {};
    const isOnline = Boolean(st.connected);
    const isAnalyzing = Boolean(st.analyzing);

    let dotCls = isOnline ? 'dot-online' : (c.empty ? '' : 'dot-danger');
    if (isAnalyzing) dotCls = 'dot-warning';

    // شارة حالة قصيرة في الأعلى فقط إذا كانت الحالة غير طبيعية ("تحليل..." أو "منقطع")
    let abnormalBadge = '';
    if (isAnalyzing) {
      abnormalBadge = `<div id="tileTag_${cid}" class="tile-state-tag tag-analyzing">تحليل...</div>`;
    } else if (!isOnline && !c.empty) {
      abnormalBadge = `<div id="tileTag_${cid}" class="tile-state-tag tag-offline">التحليل منقطع</div>`;
    } else {
      abnormalBadge = `<div id="tileTag_${cid}" class="tile-state-tag" style="display:none;"></div>`;
    }

    const resStr = st.resolution || 'غير معروف';
    const sFps = (st.stream_fps || 0).toFixed(1);
    const dispFps = (st.display_fps || 0).toFixed(1);
    const dFps = (st.detect_fps || 0).toFixed(1);
    const encMs = (st.encode_ms || 0).toFixed(1);
    const feedQuery = (count >= 9) ? '?w=480' : '';

    return `
      <div id="camTile_${cid}" class="cam-tile ${isSel ? 'selected' : ''}" data-cam-id="${cid}"
           onclick="selectCameraById(${cid})" ondblclick="toggleTileFullscreen(${cid})">
        ${tileUsesHq(cid)
          ? `<div id="cellVidHost_${cid}" class="cam-stream-host"></div>`
          : (hqPending && !c.empty
              ? `<div class="cam-stream-host" data-pending-hq="1"></div>`
              : `<img id="cellImg_${cid}" class="cam-stream-img" src="/video_feed/${cid}${feedQuery}" alt="${code}" />`)}
        <canvas id="roiCanvas_${cid}" class="roi-canvas ${drawToolMode ? 'drawing-enabled' : ''}"></canvas>

        <div class="tile-header">
          <div class="tile-cam-title">
            <span id="tileDot_${cid}" class="status-dot ${dotCls}"></span>
            <span>${code} • ${escapeHtml(c.name || ('كاميرا ' + cid))}</span>
            ${tileUsesHq(cid) ? '<span class="hq-badge" title="بث عالي الدقة عبر go2rtc">HQ</span>' : ''}
          </div>
          ${abnormalBadge}
        </div>

        ${c.empty ? `<div class="tile-center-alert" style="display:block; border-color:var(--border-subtle);" onclick="event.stopPropagation()">
          <h4><span>خانة فارغة</span></h4>
          <p>لا توجد كاميرا مضافة في هذه الخانة</p>
          <button class="btn-tool" style="margin-top:6px; height:24px; font-size:11px;" onclick="switchScreen('devices')">إضافة كاميرا</button>
        </div>` : ''}
        <div id="tileOfflineBox_${cid}" class="tile-center-alert" style="display:${(isOnline || c.empty || tileUsesHq(cid)) ? 'none' : 'block'};" onclick="event.stopPropagation()">
          <h4>
            <svg class="ico" style="color:var(--status-danger)" viewBox="0 0 24 24"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
            <span>انقطع اتصال البث</span>
          </h4>
          <p id="tileOfflineMsg_${cid}">${escapeHtml(st.status_msg || 'اضغط تشغيل لبدء البث أو إعادة المحاولة')}</p>
          <button class="btn-tool primary" style="margin-top:6px; height:24px; font-size:11px;" onclick="startCam(${cid})">${ic('play')} تشغيل / إعادة اتصال</button>
        </div>

        <div class="tile-footer" onclick="event.stopPropagation()">
          <div id="tileMeta_${cid}" class="tile-tech-meta mono">${isOnline ? 'الكشف: نشط' : ''}</div>
          <div class="tile-quick-actions">
            <button class="icon-btn-mini" onclick="startCam(${cid})" title="تشغيل أو تحديث البث" aria-label="تشغيل بث الكاميرا ${cid}">${ic('play')}</button>
            <button class="icon-btn-mini" onclick="snapshotCam(${cid})" title="التقاط صورة" aria-label="التقاط صورة للكاميرا ${cid}">
              <svg class="ico" viewBox="0 0 24 24"><path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/></svg>
            </button>
            <button class="icon-btn-mini" onclick="toggleTileFullscreen(${cid})" title="ملء الشاشة" aria-label="ملء الشاشة للكاميرا ${cid}">
              <svg class="ico" viewBox="0 0 24 24"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/></svg>
            </button>
          </div>
        </div>
      </div>
    `;
  }).join('');

  cellsToShow.forEach(c => {
    const cid = Number(c.id);
    const cv = document.getElementById('roiCanvas_' + cid);
    if (cv) attachLaserCanvasRoiEvents(cv, cid);
    if (document.getElementById('cellVidHost_' + cid)) mountHqPlayer(cid);
  });
  updateLiveModeBtn();
}

// ===================== v16 #R16: البث عالي الدقة عبر go2rtc =====================
function tileUsesHq(cid) {
  if (!go2rtcState.running || liveViewMode !== 'hq' || hqFailed[cid]) return false;
  // v17 #R25: الرسم يصير فوق الفيديو عالي الدقة نفسه (ما نرجع لبث 640)
  return (go2rtcState.streams || []).includes('cam' + cid);
}

function hqStreamName(cid) {
  const sub = 'cam' + cid + '_sub';
  return (Number(currentGrid) >= 9 && (go2rtcState.streams || []).includes(sub)) ? sub : ('cam' + cid);
}

function go2rtcOrigin() {
  return `${location.protocol}//${location.hostname}:${go2rtcState.port || 1985}`;
}

let go2rtcPlayerPromise = null;
function ensureGo2rtcPlayer() {
  if (customElements.get('cctv-rtc')) return Promise.resolve(true);
  if (!go2rtcPlayerPromise) {
    go2rtcPlayerPromise = import(go2rtcOrigin() + '/video-rtc.js').then(mod => {
      if (!customElements.get('cctv-rtc')) {
        class CctvRtc extends mod.VideoRTC {
          constructor() {
            super();
            // v16 #R17: MSE (عبر WebSocket/TCP) بدل WebRTC (UDP): بث 4K/12MP كان يفقد حزم حتى داخل نفس الجهاز
            // ويتجمّد ويطلع أسود. MSE بدون أي فقد وتأخيره ~1 ثانية فقط.
            this.mode = 'mse';
            this.media = 'video';
            this.visibilityThreshold = 0.01;  // يوقف البث لما تكون الشاشة مخفية
          }
          oninit() {
            super.oninit();
            this.video.controls = false;
            this.video.muted = true;
            this.video.style.objectFit = 'contain';
            this.video.style.background = '#000';
          }
        }
        customElements.define('cctv-rtc', CctvRtc);
      }
      return true;
    }).catch(err => {
      console.warn('go2rtc player load failed', err);
      go2rtcPlayerPromise = null;
      return false;
    });
  }
  return go2rtcPlayerPromise;
}

async function mountHqPlayer(cid) {
  const host = document.getElementById('cellVidHost_' + cid);
  if (!host) return;
  const ok = await ensureGo2rtcPlayer();
  if (!document.body.contains(host)) return;
  if (!ok) {
    hqFailed[cid] = true;
    renderMatrixCells();
    return;
  }
  const el = document.createElement('cctv-rtc');
  host.appendChild(el);
  el.src = `ws://${location.hostname}:${go2rtcState.port || 1985}/api/ws?src=${encodeURIComponent(hqStreamName(cid))}`;
  if (el.video) el.video.addEventListener('loadedmetadata', () => drawHqOverlay(cid), { once: true });
  let checks = 0, lastTime = -1;
  const watchdog = () => {
    if (!document.body.contains(el)) return;
    const v = el.video;
    if (v && v.videoWidth > 0 && v.currentTime > lastTime) {
      checks = 0; lastTime = v.currentTime;
      setTimeout(watchdog, 8000); return;
    }
    const visible = document.getElementById('view_live').classList.contains('active') && !document.hidden;
    if (!visible) { checks = 0; lastTime = -1; }
    if (!visible || ++checks < 2) {
      setTimeout(watchdog, 8000);
      return;
    }
    hqFailed[cid] = true;
    renderMatrixCells();
    showBannerNotice('تعذر العرض عالي الدقة للكاميرا CAM-' + String(cid).padStart(2, '0') + ' — رجعنا للبث العادي');
    setTimeout(() => showBannerNotice(''), 4000);
  };
  setTimeout(watchdog, 8000);
}

// v16 #R17: رسم المنطقة والخط ومربعات الكشف فوق البث عالي الدقة (بدل الرجوع لطبقة MJPEG)
const CLS_LABEL = { person: 'شخص', vehicle: 'مركبة', animal: 'حيوان', other: 'جسم' };
let overlayData = {};

function hqRenderBox(canvas, video) {
  const cw = canvas.clientWidth, ch = canvas.clientHeight;
  if (!video || !video.videoWidth || !video.videoHeight) return null;
  const scale = Math.min(cw / video.videoWidth, ch / video.videoHeight);
  const rw = video.videoWidth * scale, rh = video.videoHeight * scale;
  return { left: (cw - rw) / 2, top: (ch - rh) / 2, width: rw, height: rh };
}

function drawHqOverlay(cid) {
  const canvas = document.getElementById('roiCanvas_' + cid);
  const host = document.getElementById('cellVidHost_' + cid);
  if (!canvas || !host) return;
  if (drawToolMode && Number(cid) === Number(focusedCamId)) return;  // الرسم اليدوي له طبقته
  const video = host.querySelector('video');
  const box = hqRenderBox(canvas, video);
  canvas.width = canvas.clientWidth;
  canvas.height = canvas.clientHeight;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!box) return;
  const cam = (appCfg.cameras || []).find(c => Number(c.id) === Number(cid)) || {};
  const roi = cam.roi || {};
  const px = (nx, ny) => [box.left + nx * box.width, box.top + ny * box.height];
  const lw = Math.max(1.5, box.height / 360);
  ctx.lineJoin = 'round';
  ctx.font = `${Math.max(10, Math.round(box.height / 34))}px 'IBM Plex Sans Arabic', sans-serif`;
  if (roi.zone_enabled) {
    ctx.strokeStyle = '#E7C873';
    ctx.lineWidth = lw * 1.6;
    ctx.beginPath();
    const poly = (roi.polygon && roi.polygon.length >= 3) ? roi.polygon
      : [[roi.zx1 || 0.15, roi.zy1 || 0.15], [roi.zx2 || 0.85, roi.zy1 || 0.15], [roi.zx2 || 0.85, roi.zy2 || 0.85], [roi.zx1 || 0.15, roi.zy2 || 0.85]];
    poly.forEach((pt, i) => { const [x, y] = px(Number(pt[0]), Number(pt[1])); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.closePath();
    ctx.stroke();
    ctx.fillStyle = 'rgba(231, 200, 115, 0.08)';
    ctx.fill();
  }
  (roi.masks || []).forEach(mp => {
    if (!mp || mp.length < 3) return;
    ctx.beginPath();
    mp.forEach((pt, i) => { const [x, y] = px(Number(pt[0]), Number(pt[1])); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.closePath();
    ctx.fillStyle = 'rgba(90, 90, 90, 0.35)';
    ctx.fill();
    ctx.setLineDash([6, 4]);
    ctx.strokeStyle = '#CFCFCF';
    ctx.lineWidth = lw;
    ctx.stroke();
    ctx.setLineDash([]);
  });
  const od = overlayData[cid] || {};
  if (roi.line_enabled) {
    const [x1, y1] = px(Number(roi.lx1 ?? 0.2), Number(roi.ly1 ?? 0.55));
    const [x2, y2] = px(Number(roi.lx2 ?? 0.8), Number(roi.ly2 ?? 0.55));
    ctx.strokeStyle = od.flash ? '#FF4A3D' : '#FFD200';
    ctx.lineWidth = lw * 2;
    ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
    ctx.fillStyle = '#B4FF00';
    [[x1, y1], [x2, y2]].forEach(([x, y]) => { ctx.beginPath(); ctx.arc(x, y, lw * 3, 0, Math.PI * 2); ctx.fill(); });
  }
  for (const b of (od.boxes || [])) {
    const [x, y] = px(b.x, b.y);
    const w = b.w * box.width, h = b.h * box.height;
    ctx.strokeStyle = b.c === 'person' ? '#4DA3FF' : (b.c === 'vehicle' ? '#FFC53D' : '#99E699');
    ctx.lineWidth = lw * 1.6;
    ctx.strokeRect(x, y, w, h);
    const label = `${CLS_LABEL[b.c] || b.c} ${Math.round((b.p || 0) * 100)}%`;
    const tw = ctx.measureText(label).width + 8, th = Math.round(box.height / 26);
    ctx.fillStyle = 'rgba(0,0,0,0.65)';
    ctx.fillRect(x, Math.max(0, y - th), tw, th);
    ctx.fillStyle = '#FFFFFF';
    ctx.fillText(label, x + 4, Math.max(th - 4, y - 4));
  }
}

let overlayBusy = false;
async function pollHqOverlay() {
  const hosts = document.querySelectorAll('.cam-stream-host');
  if (!hosts.length || overlayBusy || document.hidden || !document.getElementById('view_live').classList.contains('active')) return;
  overlayBusy = true;
  try {
    overlayData = await (await fetch('/api/overlay')).json();
  } catch (e) {
    overlayData = {};
  } finally {
    overlayBusy = false;
  }
  hosts.forEach(h => drawHqOverlay(h.id.replace('cellVidHost_', '')));
}
setInterval(pollHqOverlay, 350);
window.addEventListener('resize', () => document.querySelectorAll('.cam-stream-host').forEach(h => drawHqOverlay(h.id.replace('cellVidHost_', ''))));

function tilesNeedRerender() {
  for (const tile of document.querySelectorAll('.cam-tile')) {
    const cid = Number(tile.dataset.camId);
    const isHq = Boolean(document.getElementById('cellVidHost_' + cid));
    if (isHq !== tileUsesHq(cid)) return true;
  }
  return false;
}

function updateLiveModeBtn() {
  const btn = document.getElementById('btnLiveMode');
  if (!btn) return;
  const txt = document.getElementById('liveModeTxt');
  const icon = document.getElementById('liveModeIcon');
  if (!go2rtcState.running) {
    txt.textContent = 'بث عادي';
    btn.classList.remove('active');
    btn.title = 'البث عالي الدقة غير متاح: ' + (go2rtcState.status_msg || 'go2rtc متوقف');
    if (icon) icon.setAttribute('href', '#i-video');
    return;
  }
  const hq = liveViewMode === 'hq';
  txt.textContent = hq ? 'عالي الدقة' : 'طبقة التحليل';
  btn.classList.toggle('active', hq);
  btn.title = hq
    ? 'العرض الحالي: بث عالي الدقة (go2rtc). اضغط لعرض طبقة التحليل بالمربعات والخطوط'
    : 'العرض الحالي: طبقة التحليل (MJPEG). اضغط للرجوع للبث عالي الدقة';
  if (icon) icon.setAttribute('href', hq ? '#i-video' : '#i-scan-eye');
}

function toggleLiveViewMode() {
  if (!go2rtcState.running) {
    showBannerNotice('البث عالي الدقة غير متاح الآن: ' + (go2rtcState.status_msg || 'go2rtc متوقف'));
    setTimeout(() => showBannerNotice(''), 3500);
    return;
  }
  liveViewMode = (liveViewMode === 'hq') ? 'analysis' : 'hq';
  try { localStorage.setItem('liveViewMode', liveViewMode); } catch (e) {}
  Object.keys(hqFailed).forEach(k => delete hqFailed[k]);
  renderMatrixCells();
}

function selectCameraById(camId) {
  focusedCamId = Number(camId);
  const code = 'CAM-' + String(focusedCamId).padStart(2, '0');
  document.getElementById('toolbarSelectedCam').textContent = code;

  document.querySelectorAll('.cam-tile').forEach(el => {
    el.classList.toggle('selected', Number(el.dataset.camId) === focusedCamId);
  });

  const cam = (appCfg.cameras || []).find(c => Number(c.id) === focusedCamId);
  if (cam) {
    document.getElementById('edit_cam_id').value = cam.id;
    document.getElementById('cam_name').value = cam.name || ('كاميرا ' + cam.id);
    document.getElementById('cam_conn_type').value = cam.conn_type || 'hikvision_nvr';
    document.getElementById('cam_ip').value = cam.ip || 'غير معروف';
    document.getElementById('cam_channel').value = cam.channel || cam.id;
    document.getElementById('cam_username').value = cam.username || 'admin';
    document.getElementById('cam_password').value = cam.password || '';
    document.getElementById('cam_http_port').value = cam.http_port || '80';
    document.getElementById('cam_stream_type').value = cam.stream_type || 'main';
    document.getElementById('cam_ai_watch').value = String(cam.ai_watch !== false);
    document.getElementById('cam_lpr_enabled').value = String(Boolean(cam.lpr_enabled));
    const an = cam.analysis || {};

    if (document.getElementById('cam_detect_quality')) document.getElementById('cam_detect_quality').value = cam.analysis_stream || 'third';
    if (document.getElementById('cam_line_direction')) document.getElementById('cam_line_direction').value = an.line_direction || cam.line_direction || 'both';
    if (document.getElementById('cam_zone_dwell_sec')) document.getElementById('cam_zone_dwell_sec').value = an.zone_dwell_sec || cam.zone_dwell_sec || 1.0;
    if (document.getElementById('cam_cooldown_seconds')) document.getElementById('cam_cooldown_seconds').value = an.cooldown_seconds || appCfg.cooldown_seconds || 12;
    if (document.getElementById('cam_detect_fps')) document.getElementById('cam_detect_fps').value = cam.detect_fps || '';  // v17 #R25
    if (document.getElementById('cam_trigger_source')) document.getElementById('cam_trigger_source').value = an.trigger_source || cam.trigger_source || 'local';
    if (document.getElementById('cam_schedule')) document.getElementById('cam_schedule').value = an.schedule || 'always';
    if (document.getElementById('cam_min_confidence')) {
      const mc = Number(an.min_confidence !== undefined ? an.min_confidence : 0.45);
      document.getElementById('cam_min_confidence').value = mc;
      document.getElementById('cam_min_conf_lbl').textContent = mc.toFixed(2);
    }
    if (document.getElementById('cam_ai_verify')) document.getElementById('cam_ai_verify').value = String(an.ai_verify !== false);
    if (document.getElementById('cam_alert_threshold')) {
      const at = Number(an.alert_threshold !== undefined ? an.alert_threshold : 0.6);
      document.getElementById('cam_alert_threshold').value = at;
      document.getElementById('cam_alert_thr_lbl').textContent = at.toFixed(2);
    }
    if (document.getElementById('cam_min_box_height_pct')) document.getElementById('cam_min_box_height_pct').value = (an.min_box_height_pct !== undefined ? an.min_box_height_pct : 8);
    const detSet = new Set(an.detect_classes || ['person', 'vehicle', 'animal', 'other']);
    const altSet = new Set(an.alert_classes || ['person', 'vehicle']);
    ['person', 'vehicle', 'animal', 'other'].forEach(cls => {
      const dcEl = document.getElementById('det_cls_' + cls);
      const acEl = document.getElementById('alt_cls_' + cls);
      if (dcEl) dcEl.checked = detSet.has(cls);
      if (acEl) acEl.checked = altSet.has(cls);
    });

    const r = cam.roi || {};
    document.getElementById('drawZoneBtn').classList.toggle('active', Boolean(r.zone_enabled));
    document.getElementById('drawLineBtn').classList.toggle('active', Boolean(r.line_enabled));

    const stEl = document.getElementById('cam_stream_type');
    if (stEl && !stEl._boundSync) {
      stEl._boundSync = true;
      stEl.addEventListener('change', (e) => {
        const dq = document.getElementById('cam_detect_quality');
        if (dq) dq.value = (e.target.value === 'main') ? 'main' : 'sub';
      });
    }
  }
  updateInspectorValues();
}

function selectCameraForEdit(camId) {
  selectCameraById(camId);
}

function openCameraEditDrawer(camId) {
  if (camId) selectCameraById(camId);
  const dr = document.getElementById('camEditDrawer');
  dr.classList.add('open');
}

function updateInspectorValues() {
  const code = 'CAM-' + String(focusedCamId).padStart(2, '0');
  const cam = (appCfg.cameras || []).find(c => Number(c.id) === focusedCamId) || {};
  const st = camStatuses[focusedCamId] || {};
  const hw = cam.cam_hw_info || {};

  document.getElementById('inspCamCode').textContent = code;
  document.getElementById('inspCamName').textContent = cam.name || ('كاميرا ' + focusedCamId);
  document.getElementById('inspFps').textContent = `${(st.stream_fps || 0).toFixed(1)} / ${(st.display_fps || 0).toFixed(1)} / ${(st.detect_fps || 0).toFixed(1)} fps • ${(st.encode_ms || 0).toFixed(1)}ms`;
  document.getElementById('inspRes').textContent = st.resolution || '--';
  const modelTxt = (hw.cam_model && !hw.cam_model.includes('غير معلن') && !hw.cam_model.includes('غير مفحوص')) ? hw.cam_model : 'غير معلن';
  document.getElementById('inspModel').textContent = modelTxt;
}

function snapshotCam(cid) {
  window.open(`/video_feed/${cid}`, '_blank');
}

function toggleTileFullscreen(cid) {
  const tile = document.getElementById('camTile_' + cid) || document.getElementById('cameraGrid');
  if (!document.fullscreenElement) {
    if (tile.requestFullscreen) tile.requestFullscreen();
  } else {
    if (document.exitFullscreen) document.exitFullscreen();
  }
}

async function startCam(cid) {
  await fetch('/api/cam_action', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'start', cam_id: cid})
  });
  const img = document.getElementById('cellImg_' + cid);
  if (img) img.src = `/video_feed/${cid}?t=` + Date.now();
  if (hqFailed[cid] || document.getElementById('cellVidHost_' + cid)) {
    delete hqFailed[cid];
    renderMatrixCells();  // v16 #R16: إعادة توصيل البث عالي الدقة
  }
}

async function stopCam(cid) {
  await fetch('/api/cam_action', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'stop', cam_id: cid})
  });
}

async function startAllCameras() {
  document.getElementById('moreGridMenu').classList.remove('open');
  await fetch('/api/cam_action', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'start_all'})
  });
  renderMatrixCells();
}

async function stopAllCameras() {
  document.getElementById('moreGridMenu').classList.remove('open');
  await fetch('/api/cam_action', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'stop_all'})
  });
}

async function analyzeFocusedCam(btn) {
  const span = btn ? btn.querySelector('.btn-text') : null;
  const orig = span ? span.textContent : '';
  if (span) span.textContent = 'جاري التحليل...';
  await fetch('/api/analyze_now', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({cam_id: focusedCamId})
  });
  if (span) span.textContent = orig;
  refreshStatus();
}

function filterSideEvents(mode) {
  sideFilterMode = mode;
  const chips = {
    all: 'sideChipAll',
    confirmed: 'sideChipConfirmed',
    important: 'sideChipImp',
    person: 'sideChipPerson',
    vehicle: 'sideChipVehicle',
    animal: 'sideChipAnimal'
  };
  Object.entries(chips).forEach(([m, id]) => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('active', m === mode);
  });
  renderAllEventViews();
}

function classifyEventSeverity(ev) {
  if (ev.important) return { key: 'high', label: 'مرتفعة', cls: 'sev-high' };
  // لو الحدث غير مؤكد صراحة من الذكاء الاصطناعي لا يظهر كإنذار متوسط بل كنشاط عادي
  if (ev.confirmed === false && !ev.important) {
    return { key: 'normal', label: 'طبيعي / غير مؤكد', cls: 'sev-normal' };
  }
  if (ev.is_lpr || ev.plate_url || (ev.trigger && (ev.trigger.includes('منطقة') || ev.trigger.includes('مضلّع')))) {
    return { key: 'medium', label: 'متوسطة', cls: 'sev-medium' };
  }
  return { key: 'normal', label: 'عادية', cls: 'sev-normal' };
}

function getEventAiState(ev) {
  const st = ev.ai_status || '';
  if (st === 'pending' || st === 'analyzing') return 'pending';
  if (st === 'failed' || st === 'overload_skipped' || ev.is_error) return 'failed';
  if (st === 'succeeded') return 'ok';
  const d = (ev.description || '');
  if (d.includes('خطأ') || d.includes('فشل') || d.includes('تعذر') || d.includes('Timeout')) return 'failed';
  return 'ok';
}

function isEventFailed(ev) {
  return getEventAiState(ev) === 'failed';
}

function formatPlateEvidence(ev) {
  const states = {confirmed:'مؤكدة آلياً',candidate:'مرشحة غير مؤكدة',partial:'قراءة جزئية غير مؤكدة',
    unreadable:'غير مقروءة',waiting_hq:'بانتظار إطار التسجيل',reading:'القراءة قيد التنفيذ',
    reader_unavailable:'قارئ اللوحات غير جاهز',busy:'قارئ اللوحات مشغول',failed:'فشلت القراءة',not_requested:'غير مطلوبة'};
  let text = states[ev.plate_status] || 'غير مؤكدة';
  if (ev.plate_text) text += ': ' + ev.plate_text;
  if (ev.plate_conf) text += ' (ثقة OCR ' + Math.round(Number(ev.plate_conf)*100) + '%)';
  if (ev.ai_plate_proposal && ev.plate_status !== 'confirmed') text += ' • اقتراح الرؤية للمراجعة فقط: ' + ev.ai_plate_proposal;
  if (ev.evidence_source) text += ' • المصدر: ' + (ev.evidence_source === 'recording' ? 'التسجيل' : 'صورة تحليل احتياطية') + ' ' + (ev.evidence_resolution || '');
  if (ev.plate_native_resolution) text += ' • القصّة الأصلية: ' + ev.plate_native_resolution;
  if (ev.notified_plate_status && ev.notified_plate_status !== ev.plate_status) text += ' • هذه النتيجة أحدث من التنبيه المرسل';
  return text;
}

function formatTelegramStatusText(ev) {
  const st = ev.telegram_status || 'not_requested';
  const ms = ev.telegram_accept_ms ? ` (زمن قبول تيليجرام للطلب: ${ev.telegram_accept_ms} ms)` : '';
  if (st === 'accepted') return `تيليجرام: قُبل من تيليجرام${ms} (القبول لا يعني وصول الإشعار للهاتف لو كان الجهاز مغلقاً)`;
  if (st === 'skipped') return `تيليجرام: ${ev.telegram_status_msg || 'لم يُرسل'}`;
  if (st === 'waiting_evidence') return 'تيليجرام: بانتظار تجهيز صورة الحدث ونتيجة اللوحة';
  if (st === 'unknown') return 'تيليجرام: النتيجة غير معروفة — انقطع الرد؛ لم تتم إعادة الإرسال لتجنب التكرار';
  if (st === 'rate_limited') return 'تيليجرام: رفض مؤقت بسبب حد الطلبات؛ لم يُرسل التنبيه';
  if (st === 'sending') return 'تيليجرام: قيد الإرسال...';
  if (st === 'failed') return `تيليجرام: فشل (${ev.telegram_status_msg || 'خطأ إرسال'})`;
  return 'تيليجرام: لم يُطلب الإرسال';
}

function formatClipStatusText(ev) {
  const cs = ev.clip_status || 'complete';
  const dur = (ev.clip_duration !== undefined && ev.clip_duration !== null) ? `${ev.clip_duration}s` : '';
  if (cs === 'recording') return 'المقطع: جارٍ التسجيل...';
  if (cs === 'failed') return `المقطع: فشل التسجيل (${ev.clip_status_msg || 'خطأ'})`;
  if (cs === 'stream_interrupted') return `المقطع: ناقص بسبب انقطاع البث (${dur})`;
  if (cs === 'partial_pre_buffer') return `المقطع: جزئي (${dur})`;
  return `المقطع: محفوظ (${dur || '15s'})`;
}

function renderAllEventViews() {
  const all = cachedEvents || [];
  const impCount = all.filter(e => Boolean(e.important)).length;
  const confCount = all.filter(e => Boolean(e.confirmed || e.important)).length;
  document.getElementById('sideCountAll').textContent = all.length;
  document.getElementById('sideCountImp').textContent = impCount;
  const elConf = document.getElementById('sideCountConf');
  if (elConf) elConf.textContent = confCount;
  ['person', 'vehicle', 'animal'].forEach(cat => {
    const el = document.getElementById('sideCount' + cat.charAt(0).toUpperCase() + cat.slice(1));
    if (el) el.textContent = all.filter(e => e.category === cat).length;
  });

  // 1. تغذية اللوحة الجانبية (بحد أقصى سطرين بدون زمن تحليل)
  let sideList = all;
  if (sideFilterMode === 'important') sideList = all.filter(e => Boolean(e.important));
  else if (sideFilterMode === 'confirmed') sideList = all.filter(e => Boolean(e.confirmed || e.important));
  else if (['person', 'vehicle', 'animal'].includes(sideFilterMode)) sideList = all.filter(e => e.category === sideFilterMode);
  const sideContainer = document.getElementById('sideEventsList');

  if (!sideList.length) {
    sideContainer.innerHTML = `<div style="padding:18px; text-align:center; color:var(--text-muted); font-size:12px;">لا توجد أحداث مطابقة حالياً</div>`;
  } else {
    sideContainer.innerHTML = sideList.slice(0, 30).map((ev, idx) => {
      const sev = classifyEventSeverity(ev);
      const aiSt = getEventAiState(ev);
      const timeOnly = escapeHtml((ev.timestamp || '').split(' ')[1] || ev.timestamp || '--:--:--');
      const camCode = 'CAM-' + String(ev.cam_id || 1).padStart(2, '0');
      const rawDesc = aiSt === 'failed'
        ? 'تم حفظ لقطة الحدث والمقطع المسجل (15 ثانية) للمراجعة اليدوية.'
        : (ev.description || ev.trigger || '');
      const descText = escapeHtml(rawDesc);

      let stateChip = `<span class="badge-sev ${sev.cls}">${sev.label}</span>`;
      if (aiSt === 'pending') {
        stateChip = `<span class="ev-fail-inline" style="color:var(--status-warning);">${ic('clock')} جاري التحليل...</span>`;
      } else if (aiSt === 'failed') {
        stateChip = `<span class="ev-fail-inline" title="${escapeHtml(ev.ai_error_reason || '')}">${ic('triangle-alert')} فشل التحليل، والمقطع محفوظ</span>`;
      }

      const tgTitle = escapeHtml(formatTelegramStatusText(ev));
      const tgBadge = (ev.telegram_status === 'accepted')
        ? `<span class="preview-mini-tag" style="color:var(--status-online);" title="${tgTitle}">TG ${ic('check')}</span>`
        : (['failed','unknown','rate_limited'].includes(ev.telegram_status)
            ? `<span class="preview-mini-tag" style="color:var(--status-danger);" title="${tgTitle}">TG ${ic('x')}</span>`
            : '');

      return `
        <div class="event-feed-card ${ev.id === selectedEventId ? 'active-card' : ''}" data-ev-id="${escapeHtml(ev.id)}" onclick="openEventById(this.dataset.evId)">
          <div class="ev-card-top">
            <div class="ev-thumb" data-prev-id="${escapeHtml(ev.id)}">
              <img src="${escapeHtml(ev.image_url)}" alt="حدث" loading="lazy" />
            </div>
            <div class="ev-head-meta">
              <div class="ev-cam-row">
                <span class="ev-cam-name"><span class="mono">${camCode}</span> • ${escapeHtml(ev.cam_name || 'كاميرا')}</span>
                <span class="ev-time mono">${timeOnly}</span>
              </div>
              <div class="ev-badges-row">
                ${getCategoryIcon(ev.category)}
                ${stateChip}
                ${tgBadge}
              </div>
            </div>
          </div>
          <div class="ev-desc-text">${descText}</div>
        </div>
      `;
    }).join('');
  }

  // 2. جدول شاشة الأحداث الكاملة مع الفلاتر الأربعة
  const fCam = document.getElementById('fltCam').value;
  const fDate = (document.getElementById('fltDate').value || '').trim();
  const fType = document.getElementById('fltType').value;
  const fSev = document.getElementById('fltSev').value;

  const tableSource = [...new Map([...(archiveEvents || []), ...all].map(e => [e.id, e])).values()].sort((a,b) => evEpoch(b)-evEpoch(a));
  const tableEvents = tableSource.filter(ev => {
    if (fCam !== 'all' && String(ev.cam_id || 1) !== String(fCam)) return false;
    if (fDate && !(ev.timestamp || '').includes(fDate)) return false;
    const sev = classifyEventSeverity(ev);
    if (fSev !== 'all' && sev.key !== fSev) return false;
    if (fType === 'person' && ev.category !== 'person') return false;
    if (fType === 'vehicle' && ev.category !== 'vehicle') return false;
    if (fType === 'animal' && ev.category !== 'animal') return false;
    if (fType === 'other' && !['other', 'unknown', 'general'].includes(ev.category || 'other')) return false;
    if (fType === 'confirmed' && !ev.confirmed) return false;
    if (fType === 'unconfirmed' && (ev.confirmed || isEventFailed(ev))) return false;
    if (fType === 'failed' && !isEventFailed(ev)) return false;
    if (fType === 'line' && !(ev.trigger || '').includes('خط')) return false;
    if (fType === 'zone' && !((ev.trigger || '').includes('منطقة') || (ev.trigger || '').includes('مضلّع'))) return false;
    if (fType === 'lpr' && !(ev.is_lpr || ev.plate_url || (ev.description || '').includes('لوحة') || (ev.description || '').includes('مركبة'))) return false;
    return true;
  });

  document.getElementById('eventsCountLabel').innerHTML = `<span class="mono">${tableEvents.length}</span> أحداث`;
  renderEventTimeline(tableEvents);  // v16 #R16
  const tbody = document.getElementById('eventsTableBody');
  const emptyBox = document.getElementById('emptyStateBanner');

  if (!tableEvents.length) {
    tbody.innerHTML = '';
    emptyBox.style.display = 'flex';
  } else {
    emptyBox.style.display = 'none';
    tbody.innerHTML = tableEvents.map((ev, idx) => {
      const sev = classifyEventSeverity(ev);
      const aiSt = getEventAiState(ev);
      const timeOnly = escapeHtml((ev.timestamp || '').split(' ')[1] || ev.timestamp || '--:--:--');
      const camCode = 'CAM-' + String(ev.cam_id || 1).padStart(2, '0');
      const rawTrig = aiSt === 'failed'
        ? (ev.trigger ? `${ev.trigger} — فشل التحليل، والمقطع محفوظ` : 'فشل التحليل، والمقطع محفوظ')
        : (ev.trigger || 'رصد حركة');
      const trigSummary = escapeHtml(rawTrig);

      let aiColHtml = `<span class="ai-state-icon ai-ok" title="نجح تحليل الرؤية (${escapeHtml(ev.model_used || '')})">${ic('check')}</span>`;
      if (aiSt === 'pending') {
        aiColHtml = `<span class="ai-state-icon ai-pending" title="بانتظار أو قيد التحليل في الطابور">${ic('clock')}</span>`;
      } else if (aiSt === 'failed') {
        aiColHtml = `<span class="ai-state-icon ai-fail" title="${escapeHtml(ev.ai_error_reason || ev.description || 'فشل تحليل AI')}">${ic('triangle-alert')}</span>`;
      }

      const clipSt = ev.clip_status || 'complete';
      const safeClipMsg = escapeHtml(ev.clip_status_msg || '');
      let clipBadge = `<span class="preview-mini-tag mono" title="${safeClipMsg}">${ev.clip_duration ? Math.round(ev.clip_duration) + 's' : '15s'}</span>`;
      if (clipSt === 'recording') {
        clipBadge = `<span class="preview-mini-tag" style="color:var(--status-warning);" title="${safeClipMsg}">يسجل...</span>`;
      } else if (clipSt === 'failed') {
        clipBadge = `<span class="preview-mini-tag" style="color:var(--status-danger);" title="${safeClipMsg}">فشل</span>`;
      } else if (clipSt === 'stream_interrupted' || clipSt === 'partial_pre_buffer') {
        clipBadge = `<span class="preview-mini-tag mono" style="color:var(--status-warning);" title="${safeClipMsg}">${ev.clip_duration ? Math.round(ev.clip_duration) + 's*' : 'ناقص'}</span>`;
      }

      return `
        <tr class="${ev.id === selectedEventId ? 'selected-row' : ''}" data-ev-id="${escapeHtml(ev.id)}" data-prev-id="${escapeHtml(ev.id)}" onclick="selectEventById(this.dataset.evId)">
          <td><span class="mono">${timeOnly}</span></td>
          <td><span title="${escapeHtml(ev.category || 'other')}">${getCategoryIcon(ev.category)}</span> <span class="mono" style="color:var(--text-primary); font-weight:600;">${camCode}</span> • ${escapeHtml(ev.cam_name || 'كاميرا')}</td>
          <td title="${escapeHtml(ev.description || '')}">${trigSummary}</td>
          <td><span class="badge-sev ${sev.cls}">${sev.label}</span></td>
          <td style="text-align:center;">${aiColHtml}</td>
          <td style="text-align:center;">${clipBadge}</td>
        </tr>
      `;
    }).join('');
  }
}

// v16 #R16: شريط زمني لآخر 24 ساعة (يمين = قبل 24 ساعة، يسار = الآن)
function renderEventTimeline(list) {
  const track = document.getElementById('tlTrack');
  const axis = document.getElementById('tlAxis');
  if (!track || !axis) return;
  const now = Date.now() / 1000;
  const span = 86400;
  const start = now - span;
  const inWin = list.filter(ev => { const t = evEpoch(ev); return t >= start && t <= now + 60; });
  track.innerHTML = inWin.map(ev => {
    const pos = Math.max(0, Math.min(100, ((evEpoch(ev) - start) / span) * 100));
    const cat = ['person', 'vehicle', 'animal'].includes(ev.category) ? ev.category : 'other';
    const cls = `tl-mark ${cat}${ev.important ? ' imp' : ''}${ev.id === selectedEventId ? ' sel' : ''}`;
    return `<button class="${cls}" style="right:${pos.toFixed(2)}%" data-ev-id="${escapeHtml(ev.id)}" data-prev-id="${escapeHtml(ev.id)}" onclick="selectEventById(this.dataset.evId)" aria-label="${escapeHtml(ev.timestamp || '')}"></button>`;
  }).join('');
  let ticks = '';
  for (let k = 0; k <= 8; k++) {
    const d = new Date((start + k * 3 * 3600) * 1000);
    const lbl = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
    const edge = (k === 0) ? ' style="right:0; transform:none;"' : (k === 8 ? ' style="left:0; transform:none;"' : ` style="right:${k * 12.5}%"`);
    ticks += `<span${edge}>${k === 8 ? 'الآن' : lbl}</span>`;
  }
  axis.innerHTML = ticks;
  const cnt = document.getElementById('tlCount');
  if (cnt) cnt.textContent = `آخر 24 ساعة • ${inWin.length} حدث`;
}

function openEventInScreen(idx) {
  const ev = (cachedEvents || [])[idx];
  if (ev) openEventById(ev.id);
}

function openEventById(evId) {
  switchScreen('events');
  selectEventById(evId);
}

async function selectEventForPlayer(idx) {
  const ev = (cachedEvents || [])[idx];
  if (ev) return selectEventById(ev.id);
}

async function selectEventById(evId, metadataOnly = false) {
  selectedEventId = evId;
  renderAllEventViews();
  const ev = findEvent(evId);
  if (!ev) return;

  const camCode = 'CAM-' + String(ev.cam_id || 1).padStart(2, '0');
  document.getElementById('playerTitle').textContent = `مقطع الحدث — ${camCode} (${ev.cam_name || 'كاميرا'})`;
  document.getElementById('playerTime').textContent = ev.timestamp || '--:--:--';

  const aiSt = getEventAiState(ev);
  const descBox = document.getElementById('playerAiDesc');
  const tgLine = escapeHtml(formatTelegramStatusText(ev));
  const clipLine = ev.clip_status_msg ? `حالة المقطع: ${escapeHtml(ev.clip_status_msg)}` : '';
  const safeDesc = escapeHtml(ev.description || ev.trigger || 'لا يوجد وصف');

  if (aiSt === 'failed') {
    const failDesc = escapeHtml(ev.description || 'تعذر الوصول لخادم الرؤية أثناء الحدث، وتم حفظ اللقطة والمقطع (15 ثانية) محلياً.');
    descBox.innerHTML = `<span style="color:var(--status-warning); font-weight:600;">⚠ فشل التحليل، والمقطع محفوظ:</span> ${failDesc}<div style="margin-top:4px; font-size:11px; color:var(--text-muted);">${clipLine} • ${tgLine}</div>`;
  } else if (aiSt === 'pending') {
    const pendDesc = escapeHtml(ev.description || 'جاري تحليل صورة الحدث المحفوظة وقت الاكتشاف...');
    descBox.innerHTML = `<span style="color:var(--status-warning); font-weight:600;">⏳ بانتظار التحليل:</span> ${pendDesc}<div style="margin-top:4px; font-size:11px; color:var(--text-muted);">${clipLine}</div>`;
  } else {
    descBox.innerHTML = `${safeDesc}<div style="margin-top:4px; font-size:11px; color:var(--text-muted);">${clipLine} • ${tgLine}</div>`;
  }

  const _modelTxt = (ev.model_requested && ev.model_used && ev.model_requested !== ev.model_used)
    ? `النموذج المطلوب: <span class="mono">${escapeHtml(ev.model_requested)}</span> ← المستخدم فعلياً: <span class="mono">${escapeHtml(ev.model_used)}</span>${ev.ai_fallback_reason ? ' <span style="color:var(--status-warning);" title="' + escapeHtml(ev.ai_fallback_reason) + '">(انتقال لبديل)</span>' : ''}`
    : `النموذج: <span class="mono">${escapeHtml(ev.model_used || '--')}</span>`;
  document.getElementById('playerModelName').innerHTML = _modelTxt
    + (ev.hq_image_url ? ` • <a href="${escapeHtml(ev.hq_image_url)}" target="_blank" rel="noopener" style="color:var(--accent-ink); font-weight:600;">صورة التسجيل ${escapeHtml(ev.evidence_resolution || '')}</a>` : '');
  document.getElementById('playerLatency').innerHTML = `زمن طلب الـAI: <span class="mono">${ev.ai_latency_ms ? ev.ai_latency_ms + ' ms' : (aiSt === 'failed' ? 'فشل' : '--')}</span>`
    + (ev.evidence_wait_ms ? ` • انتظار صورة التسجيل: <span class="mono">${ev.evidence_wait_ms} ms</span>` : '')
    + (ev.ai_queue_wait_ms ? ` • انتظار الطابور: <span class="mono">${ev.ai_queue_wait_ms} ms</span>` : '')
    + (ev.alert_delay_ms ? ` • من الرصد حتى التنبيه: <span class="mono">${ev.alert_delay_ms} ms</span>` : '');

  // شريط اللوحة المقتطعة (ANPR)
  const plateStrip = document.getElementById('playerPlateStrip');
  const plateImg = document.getElementById('playerPlateImg');
  if (ev.plate_url || ev.plate_status && ev.plate_status !== 'not_requested' || ev.ai_plate_proposal) {
    if (ev.plate_url) plateImg.src = ev.plate_url;
    else plateImg.removeAttribute('src');
    plateImg.style.display = ev.plate_url ? '' : 'none';
    plateStrip.style.display = 'flex';
    const pt = document.getElementById('playerPlateText');
    if (pt) pt.textContent = formatPlateEvidence(ev);
  } else {
    plateStrip.style.display = 'none';
  }

  // زر تحميل MP4 لا يُفعّل إلا بعد التحقق الفعلي من إنشاء ملف الـ MP4
  const dlBtn = document.getElementById('downloadMp4Btn');
  if (ev.mp4_url) {
    dlBtn.href = ev.mp4_url;
    dlBtn.style.pointerEvents = 'auto';
    dlBtn.style.opacity = '1';
  } else {
    dlBtn.href = '#';
    dlBtn.style.pointerEvents = 'none';
    dlBtn.style.opacity = '0.5';
  }

  if (metadataOnly) return;
  const videoEl = document.getElementById('playerVideoEl');
  const imgEl = document.getElementById('playerFrameImg');
  const phEl = document.getElementById('playerPlaceholder');

  stopClipTimer();
  clipFrames = [];
  clipIndex = 0;
  window._usingVideoElement = false;
  document.getElementById('clipScrubber').value = 0;
  document.getElementById('clipTimeLabel').textContent = '00:00 / 00:15';

  let mp4UrlToPlay = ev.mp4_url || '';
  let fallbackFrames = [];

  if (ev.clip_id) {
    try {
      const r = await fetch('/api/clip/' + ev.clip_id);
      const data = await r.json();
      if (data.mp4_verified && data.mp4_url) {
        mp4UrlToPlay = data.mp4_url;
        dlBtn.href = data.mp4_url;
        dlBtn.style.pointerEvents = 'auto';
        dlBtn.style.opacity = '1';
      }
      if (data.frames && data.frames.length > 0) {
        fallbackFrames = data.frames;
        // v16 #R6: لو الـ MP4 بترميز ما يشتغل في المتصفح، نشغّل الإطارات مباشرة
        if (data.browser_playable === false) mp4UrlToPlay = '';
      }
      // v17 #R21: مقاطع HEVC تحتاج دعم عتادي في المتصفح — نتحقق قبل التشغيل بدل ما نعرض شاشة سوداء
      if (data.needs_hevc && videoEl && !videoEl.canPlayType('video/mp4; codecs="hvc1.1.6.L153.B0"')) {
        mp4UrlToPlay = '';
        const nb = document.getElementById('playerAiDesc');
        if (nb) nb.insertAdjacentHTML('afterbegin', `<div style="color:var(--status-warning); font-weight:600; margin-bottom:6px;">${ic('triangle-alert')} هذا المقطع بترميز HEVC (H.265) ومتصفحك لا يشغّله. حمّله بزر MP4 وشغّله بـ VLC، أو افتح الصفحة من Chrome/Edge على ويندوز 11.</div>`);
      }
      if (data.duration_sec) {
        window._clipDurationSec = data.duration_sec;
        window._clipTriggerOffset = data.trigger_offset_sec;
        const pad2 = n => String(n).padStart(2, '0');
        document.getElementById('clipTimeLabel').textContent = `00:00 / 00:${pad2(Math.round(data.duration_sec))}`;
        clipFps = data.effective_fps || data.fps || 8;
        window._clipDurationSec = data.duration_sec || Math.round(fallbackFrames.length / clipFps) || 15;
        window._clipRelTimestamps = data.frame_timestamps_rel || [];
      } else if (data.duration_sec) {
        window._clipDurationSec = data.duration_sec;
      }
    } catch (e) {}
  }

  // تشغيل الـ MP4 بعنصر <video> أولاً، والرجوع لعرض الصور فقط إذا لم يتوفر MP4
  if (mp4UrlToPlay && videoEl) {
    window._usingVideoElement = true;
    imgEl.style.display = 'none';
    phEl.style.display = 'none';
    videoEl.style.display = 'block';
    videoEl.src = mp4UrlToPlay;
    videoEl.playbackRate = clipPlaybackSpeed;
    videoEl.ontimeupdate = () => {
      const dur = Math.max(1, Math.round(videoEl.duration || window._clipDurationSec || 15));
      const cur = Math.min(dur, Math.round(videoEl.currentTime || 0));
      const sc = document.getElementById('clipScrubber');
      sc.max = 100;
      sc.value = dur > 0 ? Math.round((videoEl.currentTime / dur) * 100) : 0;
      const pad = n => String(n).padStart(2, '0');
      document.getElementById('clipTimeLabel').textContent = `00:${pad(cur)} / 00:${pad(dur)}`;
    };
    videoEl.onerror = () => {
      window._usingVideoElement = false;
      videoEl.style.display = 'none';
      imgEl.style.display = 'block';
      imgEl.src = ev.image_url;
      if (fallbackFrames.length > 0) {
        clipFrames = fallbackFrames;
        const sc = document.getElementById('clipScrubber');
        sc.max = Math.max(0, clipFrames.length - 1);
        sc.value = 0;
        startClipPlayback();
      }
    };
    videoEl.play().catch(() => {});
  } else {
    if (videoEl) {
      videoEl.pause();
      videoEl.removeAttribute('src');
      videoEl.style.display = 'none';
    }
    imgEl.src = ev.image_url;
    imgEl.style.display = 'block';
    phEl.style.display = 'none';
    if (fallbackFrames.length > 0) {
      clipFrames = fallbackFrames;
      const sc = document.getElementById('clipScrubber');
      sc.max = Math.max(0, clipFrames.length - 1);
      sc.value = 0;
      startClipPlayback();
    }
  }
}

function startClipPlayback() {
  if (!clipFrames.length) return;
  stopClipTimer();
  clipPlaying = true;
  const intervalMs = Math.max(25, Math.round(1000 / (clipFps * clipPlaybackSpeed)));
  clipTimer = setInterval(() => {
    clipIndex = (clipIndex + 1) % clipFrames.length;
    renderCurrentClipFrame();
  }, intervalMs);
}

function stopClipTimer() {
  clipPlaying = false;
  if (clipTimer) {
    clearInterval(clipTimer);
    clipTimer = null;
  }
  const videoEl = document.getElementById('playerVideoEl');
  if (videoEl && !videoEl.paused) {
    try { videoEl.pause(); } catch (e) {}
  }
}

function renderCurrentClipFrame() {
  if (!clipFrames.length) return;
  const imgEl = document.getElementById('playerFrameImg');
  imgEl.src = 'data:image/jpeg;base64,' + clipFrames[clipIndex];
  document.getElementById('clipScrubber').value = clipIndex;

  const totalSec = Math.max(1, Math.round(window._clipDurationSec || (clipFrames.length / clipFps) || 15));
  const relTs = (window._clipRelTimestamps && window._clipRelTimestamps[clipIndex] !== undefined)
    ? Math.round(window._clipRelTimestamps[clipIndex])
    : Math.min(totalSec, Math.floor(clipIndex / clipFps));
  const curSec = Math.min(totalSec, relTs);
  const pad = n => String(n).padStart(2, '0');
  document.getElementById('clipTimeLabel').textContent = `00:${pad(curSec)} / 00:${pad(totalSec)}`;
}

function toggleClipPlay() {
  if (window._usingVideoElement) {
    const videoEl = document.getElementById('playerVideoEl');
    if (videoEl) {
      if (videoEl.paused) videoEl.play().catch(() => {});
      else videoEl.pause();
    }
    return;
  }
  if (!clipFrames.length) return;
  if (clipPlaying) stopClipTimer();
  else startClipPlayback();
}

function onScrubClip(val) {
  if (window._usingVideoElement) {
    const videoEl = document.getElementById('playerVideoEl');
    if (videoEl && videoEl.duration) {
      videoEl.currentTime = (Number(val) / 100.0) * videoEl.duration;
    }
    return;
  }
  if (!clipFrames.length) return;
  stopClipTimer();
  clipIndex = Number(val);
  renderCurrentClipFrame();
}

function setClipSpeed(spd, btn) {
  clipPlaybackSpeed = Number(spd);
  document.querySelectorAll('.speed-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  if (window._usingVideoElement) {
    const videoEl = document.getElementById('playerVideoEl');
    if (videoEl) videoEl.playbackRate = clipPlaybackSpeed;
    return;
  }
  if (clipPlaying) startClipPlayback();
}

function renderDevicesView() {
  const cams = appCfg.cameras || [];
  document.getElementById('devicesCamCount').textContent = cams.length;

  // تحديث فلتر الكاميرات في شاشة الأحداث
  const fltCam = document.getElementById('fltCam');
  const prevVal = fltCam.value;
  fltCam.innerHTML = `<option value="all">جميع الكاميرات (${cams.length})</option>` +
    cams.map(c => `<option value="${c.id}">CAM-${String(c.id).padStart(2,'0')} • ${escapeHtml(c.name || ('كاميرا ' + c.id))}</option>`).join('');
  if (prevVal) fltCam.value = prevVal;

  // تحديث بطاقة المسجل NVR (4 قيم فقط)
  const firstCam = cams[0] || {};
  const firstHw = firstCam.cam_hw_info || {};
  const onlineCount = cams.filter(c => (camStatuses[c.id] || {}).connected).length;
  const offlineCount = cams.length - onlineCount;

  document.getElementById('nvrModelVal').textContent = firstHw.nvr_model || 'غير مفحوص';
  document.getElementById('nvrFwVal').textContent = firstHw.nvr_firmware || 'غير مفحوص';
  document.getElementById('nvrIpVal').textContent = firstCam.ip || 'غير معروف';
  document.getElementById('nvrChannelsVal').innerHTML = `<span class="mono">${onlineCount}/${cams.length}</span> <span style="font-size:11px; color:var(--text-muted); font-weight:400;">(${offlineCount} منقطعة)</span>`;

  const hikConn = appCfg.hikconnect && appCfg.hikconnect.connected;
  const hikBadge = document.getElementById('hikCloudBadge');
  const hikSetBadge = document.getElementById('hikSettingsStatusBadge');
  if (hikConn) {
    hikBadge.textContent = 'Hik-Connect متصل';
    hikBadge.style.color = 'var(--status-online)';
    hikSetBadge.textContent = 'متصل سحابياً';
    hikSetBadge.style.color = 'var(--status-online)';
  } else {
    hikBadge.textContent = 'Hik-Connect غير مفعّل';
    hikBadge.style.color = 'var(--status-warning)';
    hikSetBadge.textContent = 'غير مفعّل';
    hikSetBadge.style.color = 'var(--status-warning)';
  }

  // جدول الكاميرات (6 أعمدة بارتفاع موحد، وبدون عمود العنوان/المسار)
  const tbody = document.getElementById('devicesTableBody');
  tbody.innerHTML = cams.map(c => {
    const cid = Number(c.id);
    const st = camStatuses[cid] || {};
    const online = Boolean(st.connected);
    const hw = c.cam_hw_info || {};
    const rawModel = hw.cam_model || '';
    const isDeclared = rawModel && !rawModel.includes('غير معلن') && !rawModel.includes('غير مفحوص') && rawModel !== '--';

    const modelCellHtml = isDeclared
      ? `<div class="model-stack">
           <span class="mono" style="color:var(--text-primary); font-weight:600;">${escapeHtml(rawModel)}</span>
           <span class="mono model-sub-fw">${escapeHtml(hw.cam_firmware || '')}</span>
         </div>`
      : `<span style="color:var(--text-muted);">غير معلن</span>`;

    let resExtra = '';
    if (st.analysis_override) {
      resExtra = ` <span title="${escapeHtml(st.analysis_note || 'تم تحويل التحليل للفرعي')}" style="color:var(--status-warn); font-size:11px; cursor:help;">⚠️ ${escapeHtml(st.analysis_override)}</span>`;
    }
    const resFps = online
      ? `${st.resolution || 'غير معروف'}${resExtra} • ${(st.stream_fps || 0).toFixed(0)}/${(st.detect_fps || 0).toFixed(0)} fps`
      : `-- • 0/0 fps`;

    const r = c.roi || {};
    const feats = [];
    if (r.zone_enabled) feats.push('منطقة كشف');
    if (r.line_enabled) feats.push('خط عبور');
    if (c.lpr_enabled) feats.push('تصوير لوحات');
    const featTxt = feats.length ? feats.join(' + ') : 'مراقبة عامة';

    return `
      <tr onclick="openCameraEditDrawer(${cid})">
        <td class="mono">CH-${String(c.channel || cid).padStart(2, '0')}</td>
        <td><strong>${escapeHtml(c.name || ('كاميرا ' + cid))}</strong></td>
        <td><span class="tile-state-tag ${online ? 'tag-online' : 'tag-offline'}">${online ? 'متصل' : 'منقطع'}</span></td>
        <td>${modelCellHtml}</td>
        <td class="mono">${resFps}</td>
        <td>${featTxt}</td>
      </tr>
    `;
  }).join('');

  // قائمة التفعيل السريع لتصوير اللوحات (ANPR) داخل شاشة الإعدادات
  const anprList = document.getElementById('anprCamerasQuickList');
  if (anprList) {
    anprList.innerHTML = cams.map(c => {
      const cid = Number(c.id);
      const lprOn = Boolean(c.lpr_enabled);
      return `
        <div class="setting-row">
          <span>CAM-${String(cid).padStart(2, '0')} • ${escapeHtml(c.name || ('كاميرا ' + cid))}</span>
          <button class="btn-tool ${lprOn ? 'active' : ''}" onclick="toggleCamLpr(${cid}, ${!lprOn})" title="تفعيل أو إيقاف تصوير اللوحات لهذه الكاميرا" aria-label="تبديل تصوير اللوحات للكاميرا ${cid}">
            ${lprOn ? '✓ تصوير اللوحات مفعّل' : 'تفعيل تصوير اللوحات'}
          </button>
        </div>
      `;
    }).join('');
  }
}

function toggleNvrSerial() {
  const firstCam = (appCfg.cameras || [])[0] || {};
  const hw = firstCam.cam_hw_info || {};
  const msgEl = document.getElementById('nvrProbeMsg');
  const sn = hw.cam_serial || 'غير معلن (افحص ISAPI لجلب الرقم التسلسلي)';
  msgEl.textContent = msgEl.textContent ? '' : `S/N: ${sn}`;
}

async function probeAndImportDevice() {
  const msgEl = document.getElementById('nvrProbeMsg');
  msgEl.style.color = 'var(--accent-primary)';
  msgEl.textContent = 'جاري فحص ISAPI...';

  const res = await fetch('/api/probe_device', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      cam_id: focusedCamId,
      http_port: document.getElementById('cam_http_port').value || '80'
    })
  });
  const data = await res.json();
  if (data.ok) {
    appCfg = data.config || appCfg;
    msgEl.style.color = 'var(--status-online)';
    msgEl.textContent = `✓ تم جلب بيانات المسجل والقنوات (${Object.keys(data.info.channels || {}).length})`;
    renderMatrixCells();
    renderDevicesView();
  } else {
    msgEl.style.color = 'var(--status-danger)';
    msgEl.textContent = `✕ ${data.error || 'تعذر الاتصال بـ ISAPI'}`;
  }
}

async function syncCloudOpenApi() {
  const box = document.getElementById('hikSyncStatus');
  box.style.color = 'var(--accent-primary)';
  box.textContent = 'جاري التحقق من مفاتيح السحابة...';

  const res = await fetch('/api/hikconnect_sync', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      api_url: document.getElementById('hik_api_url').value,
      app_key: document.getElementById('hik_app_key').value,
      app_secret: document.getElementById('hik_app_secret').value
    })
  });
  const data = await res.json();
  if (data.ok) {
    appCfg = data.config || appCfg;
    box.style.color = 'var(--status-online)';
    box.textContent = data.message;
    renderDevicesView();
  } else {
    box.style.color = 'var(--status-danger)';
    box.textContent = data.error;
  }
}

async function toggleCamLpr(cid, newState) {
  const cam = (appCfg.cameras || []).find(c => Number(c.id) === Number(cid));
  if (cam) cam.lpr_enabled = Boolean(newState);
  if (Number(cid) === focusedCamId) {
    document.getElementById('cam_lpr_enabled').value = String(Boolean(newState));
  }
  await fetch('/api/toggle_lpr', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ cam_id: cid, lpr_enabled: Boolean(newState) })
  });
  renderDevicesView();
}

async function saveCurrentCamera() {
  const cid = Number(document.getElementById('edit_cam_id').value || 1);
  let cams = appCfg.cameras || [];
  const existing = cams.find(x => Number(x.id) === cid) || {};

  const newCam = {
    id: cid,
    name: document.getElementById('cam_name').value || ('كاميرا ' + cid),
    conn_type: document.getElementById('cam_conn_type').value,
    ip: document.getElementById('cam_ip').value || 'غير معروف',
    port: '554',
    http_port: document.getElementById('cam_http_port').value || '80',
    username: document.getElementById('cam_username').value || 'admin',
    password: document.getElementById('cam_password').value || '',
    channel: document.getElementById('cam_channel').value || String(cid),
    stream_type: document.getElementById('cam_stream_type').value || 'main',
    live_stream: document.getElementById('cam_stream_type').value || 'main',
    analysis_stream: (document.getElementById('cam_detect_quality') ? document.getElementById('cam_detect_quality').value : (existing.analysis_stream || 'third')),
    line_direction: document.getElementById('cam_line_direction') ? document.getElementById('cam_line_direction').value : 'both',
    zone_dwell_sec: document.getElementById('cam_zone_dwell_sec') ? Number(document.getElementById('cam_zone_dwell_sec').value || 1.0) : 1.0,
    trigger_source: document.getElementById('cam_trigger_source') ? document.getElementById('cam_trigger_source').value : 'local',
    ai_watch: document.getElementById('cam_ai_watch').value === 'true',
    detect_fps: (document.getElementById('cam_detect_fps') && Number(document.getElementById('cam_detect_fps').value) > 0) ? Math.min(25, Number(document.getElementById('cam_detect_fps').value)) : null,  // v17 #R25
    lpr_enabled: document.getElementById('cam_lpr_enabled').value === 'true',
    analysis: {
      detect_classes: ['person', 'vehicle', 'animal', 'other'].filter(c => document.getElementById('det_cls_' + c) && document.getElementById('det_cls_' + c).checked),
      alert_classes: ['person', 'vehicle', 'animal', 'other'].filter(c => document.getElementById('alt_cls_' + c) && document.getElementById('alt_cls_' + c).checked),
      min_confidence: document.getElementById('cam_min_confidence') ? Number(document.getElementById('cam_min_confidence').value || 0.45) : 0.45,
      ai_verify: document.getElementById('cam_ai_verify') ? (document.getElementById('cam_ai_verify').value === 'true') : true,
      alert_threshold: document.getElementById('cam_alert_threshold') ? Number(document.getElementById('cam_alert_threshold').value || 0.6) : 0.6,
      min_box_height_pct: document.getElementById('cam_min_box_height_pct') ? Number(document.getElementById('cam_min_box_height_pct').value || 8) : 8,
      max_box_area_pct: (existing.analysis && existing.analysis.max_box_area_pct) || 60,
      ai_confirm_min_conf: (existing.analysis && existing.analysis.ai_confirm_min_conf) || 0.6,
      lpr_enabled: document.getElementById('cam_lpr_enabled').value === 'true',
      line_direction: document.getElementById('cam_line_direction') ? document.getElementById('cam_line_direction').value : 'both',
      zone_dwell_sec: document.getElementById('cam_zone_dwell_sec') ? Number(document.getElementById('cam_zone_dwell_sec').value || 1.0) : 1.0,
      cooldown_seconds: document.getElementById('cam_cooldown_seconds') ? Number(document.getElementById('cam_cooldown_seconds').value || 12) : 12,
      schedule: document.getElementById('cam_schedule') ? (document.getElementById('cam_schedule').value || 'always') : 'always',
      trigger_source: document.getElementById('cam_trigger_source') ? document.getElementById('cam_trigger_source').value : 'local'
    },
    enabled: true,
    cam_hw_info: existing.cam_hw_info || { cam_model: 'غير معلن', nvr_model: 'غير مفحوص' },
    roi: existing.roi || {
      line_enabled: false, lx1: 0.2, ly1: 0.55, lx2: 0.8, ly2: 0.55,
      zone_enabled: false, zx1: 0.15, zy1: 0.15, zx2: 0.85, zy2: 0.85, combo_mode: 'both'
    }
  };

  const idx = cams.findIndex(x => Number(x.id) === cid);
  if (idx >= 0) cams[idx] = newCam;
  else if (cams.length < 32) cams.push(newCam);
  appCfg.cameras = cams;

  await saveGlobalConfig(false);
  document.getElementById('camEditDrawer').classList.remove('open');
  renderMatrixCells();
  renderDevicesView();
  startCam(cid);
}

function addNewCameraSlot() {
  const cams = appCfg.cameras || [];
  if (cams.length >= 32) return;
  const usedIds = new Set(cams.map(c => Number(c.id)));
  let nextId = 1;
  while (usedIds.has(nextId) && nextId <= 32) nextId++;

  const baseCam = cams[0] || {};
  cams.push({
    id: nextId,
    name: 'كاميرا ' + nextId,
    conn_type: baseCam.conn_type || 'hikvision_nvr',
    ip: baseCam.ip || 'غير معروف',
    port: '554',
    http_port: baseCam.http_port || '80',
    username: baseCam.username || 'admin',
    password: baseCam.password || '',
    channel: String(nextId),
    stream_type: 'main',
    ai_watch: true,
    lpr_enabled: false,
    enabled: true,
    cam_hw_info: { cam_model: 'غير معلن', nvr_model: 'غير مفحوص' },
    roi: {
      line_enabled: false, lx1: 0.2, ly1: 0.55, lx2: 0.8, ly2: 0.55,
      zone_enabled: false, zx1: 0.15, zy1: 0.15, zx2: 0.85, zy2: 0.85, combo_mode: 'both'
    }
  });
  appCfg.cameras = cams;
  selectCameraById(nextId);
  saveGlobalConfig(false);
  renderMatrixCells();
  renderDevicesView();
}

function onProviderChange(keep = false) {
  const prov = document.getElementById('ai_provider').value;
  document.getElementById('universal_group').style.display = (prov !== 'gemini' && prov !== 'ollama') ? 'block' : 'none';
  const dl = document.getElementById('aiModelList');
  if (dl && !keep) dl.innerHTML = '';
  document.getElementById('gemini_group').style.display = (prov === 'gemini') ? 'block' : 'none';
  document.getElementById('ollama_group').style.display = (prov === 'ollama') ? 'block' : 'none';
  if (!keep && PROVIDER_PRESETS[prov]) {
    document.getElementById('universal_base_url').value = PROVIDER_PRESETS[prov].url;
    document.getElementById('universal_model').value = PROVIDER_PRESETS[prov].model;
  }
}

async function saveGlobalConfig(showMsg = false) {
  for (const k of GLOBAL_FIELDS) {
    if (document.getElementById(k)) appCfg[k] = document.getElementById(k).value;
  }
  appCfg.notifiers = appCfg.notifiers || {};
  appCfg.notifiers.telegram = {
    ...(appCfg.notifiers.telegram || {}),
    enabled: true,
    bot_token: document.getElementById('telegram_bot_token').value,
    chat_id: document.getElementById('telegram_chat_id').value
  };
  appCfg.notifiers.ntfy = {
    ...(appCfg.notifiers.ntfy || {}),
    enabled: Boolean(document.getElementById('ntfy_enabled') && document.getElementById('ntfy_enabled').checked),
    server_url: document.getElementById('ntfy_server_url') ? document.getElementById('ntfy_server_url').value : 'https://ntfy.sh',
    topic: document.getElementById('ntfy_topic') ? document.getElementById('ntfy_topic').value : '',
    token: document.getElementById('ntfy_token') ? document.getElementById('ntfy_token').value : ''
  };
  appCfg.notifiers.discord = {
    ...(appCfg.notifiers.discord || {}),
    enabled: Boolean(document.getElementById('discord_enabled') && document.getElementById('discord_enabled').checked),
    webhook_url: document.getElementById('discord_webhook_url') ? document.getElementById('discord_webhook_url').value : ''
  };
  appCfg.notifiers.smtp = {
    ...(appCfg.notifiers.smtp || {}),
    enabled: Boolean(document.getElementById('smtp_enabled') && document.getElementById('smtp_enabled').checked),
    host: document.getElementById('smtp_host') ? document.getElementById('smtp_host').value : '',
    port: document.getElementById('smtp_port') ? Number(document.getElementById('smtp_port').value || 587) : 587,
    username: document.getElementById('smtp_username') ? document.getElementById('smtp_username').value : '',
    password: document.getElementById('smtp_password') ? document.getElementById('smtp_password').value : '',
    from_addr: document.getElementById('smtp_username') ? document.getElementById('smtp_username').value : '',
    to_addrs: document.getElementById('smtp_to_addrs') ? document.getElementById('smtp_to_addrs').value : '',
    use_tls: true
  };
  const waMode = document.getElementById('wa_mode') ? document.getElementById('wa_mode').value : 'callmebot';
  const waKey = document.getElementById('wa_apikey') ? document.getElementById('wa_apikey').value : '';
  appCfg.notifiers.whatsapp = {
    ...(appCfg.notifiers.whatsapp || {}),
    enabled: Boolean(document.getElementById('wa_enabled') && document.getElementById('wa_enabled').checked),
    mode: waMode,
    phone: document.getElementById('wa_phone') ? document.getElementById('wa_phone').value : '',
    callmebot_apikey: waMode === 'callmebot' ? waKey : ((appCfg.notifiers.whatsapp || {}).callmebot_apikey || ''),
    access_token: waMode === 'cloud_api' ? waKey : ((appCfg.notifiers.whatsapp || {}).access_token || ''),
    phone_number_id: document.getElementById('wa_phone_id') ? document.getElementById('wa_phone_id').value : ''
  };

  appCfg.cloud_storage = {
    ...(appCfg.cloud_storage || {}),
    enabled: Boolean(document.getElementById('cloud_enabled') && document.getElementById('cloud_enabled').checked),
    confirmed_only: Boolean(document.getElementById('cloud_confirmed_only') && document.getElementById('cloud_confirmed_only').checked),
    provider: document.getElementById('cloud_provider') ? document.getElementById('cloud_provider').value : 's3',
    s3: {
      ...((appCfg.cloud_storage || {}).s3 || {}),
      endpoint_url: document.getElementById('s3_endpoint') ? document.getElementById('s3_endpoint').value : '',
      bucket: document.getElementById('s3_bucket') ? document.getElementById('s3_bucket').value : '',
      access_key: document.getElementById('s3_access_key') ? document.getElementById('s3_access_key').value : '',
      secret_key: document.getElementById('s3_secret_key') ? document.getElementById('s3_secret_key').value : '',
      prefix: document.getElementById('s3_prefix') ? document.getElementById('s3_prefix').value : 'cctv_events'
    },
    rclone: {
      remote_name: document.getElementById('s3_bucket') ? document.getElementById('s3_bucket').value : '',
      prefix: document.getElementById('s3_prefix') ? document.getElementById('s3_prefix').value : 'cctv_events'
    }
  };

  const res = await fetch('/api/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(appCfg)
  });
  const d = await res.json();
  if (d.config) appCfg = d.config;
  if (showMsg) {
    const b = document.getElementById('apiTestBox');
    b.style.color = 'var(--status-online)';
    b.textContent = '✓ تم حفظ الإعدادات بنجاح';
  }
}

async function testNotifierChannel(providerName, btn) {
  await saveGlobalConfig(false);
  const msgEl = document.getElementById('notifierTestMsg');
  const orig = btn.textContent;
  btn.textContent = '...';
  try {
    const r = await fetch('/api/notifiers/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ provider: providerName })
    });
    const d = await r.json();
    msgEl.style.color = d.ok ? 'var(--status-online)' : 'var(--status-danger)';
    msgEl.textContent = d.message || d.error || '';
  } catch (e) {
    msgEl.style.color = 'var(--status-danger)';
    msgEl.textContent = 'تعذر اختبار المزود';
  }
  btn.textContent = orig;
}

async function refreshStorageStats() {
  try {
    const r = await fetch('/api/storage/stats');
    const st = await r.json();
    if (document.getElementById('stSnapMb')) document.getElementById('stSnapMb').textContent = `${st.snapshots_mb} MB (${st.snapshots_count})`;
    if (document.getElementById('stClipMb')) document.getElementById('stClipMb').textContent = `${st.clips_mb} MB (${st.clips_count})`;
    if (document.getElementById('stPlateMb')) document.getElementById('stPlateMb').textContent = `${st.plates_mb} MB (${st.plates_count})`;
    if (document.getElementById('stEvCount')) document.getElementById('stEvCount').textContent = String(st.events_count || 0);
    if (document.getElementById('stFreeGb')) document.getElementById('stFreeGb').textContent = `${st.free_disk_gb} GB`;
    if (document.getElementById('stEvRange')) document.getElementById('stEvRange').textContent = `${(st.oldest_event||'--').split(' ')[0]} → ${(st.newest_event||'--').split(' ')[0]}`;
  } catch (e) {}
}

async function openStorageClearConfirm(mode) {
  pendingStorageClearMode = mode;
  const days = Number((document.getElementById('clearOlderDaysInput') && document.getElementById('clearOlderDaysInput').value) || 7);
  const r = await fetch('/api/storage/clear', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ mode, days, preview: true })
  });
  const d = await r.json();
  const modal = document.getElementById('storageConfirmModal');
  const sumEl = document.getElementById('storageConfirmSummary');
  document.getElementById('storageConfirmInput').value = '';
  sumEl.textContent = `سيتم حذف ${d.files_count || 0} ملف (${d.size_mb || 0} MB) و ${d.events_count || 0} سجل حدث. اكتب كلمة «حذف» للتأكيد النهائي:`;
  modal.style.display = 'flex';
}

async function executeConfirmedStorageClear() {
  const confirmVal = (document.getElementById('storageConfirmInput').value || '').trim();
  if (confirmVal !== 'حذف') {
    alert('يرجى كتابة كلمة «حذف» بشكل مطابق لتأكيد العملية.');
    return;
  }
  const days = Number((document.getElementById('clearOlderDaysInput') && document.getElementById('clearOlderDaysInput').value) || 7);
  const r = await fetch('/api/storage/clear', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ mode: pendingStorageClearMode, days, confirm_text: confirmVal, preview: false })
  });
  const d = await r.json();
  document.getElementById('storageConfirmModal').style.display = 'none';
  await refreshStorageStats();
  await refreshStatus();
}

async function testAiApi() {
  await saveGlobalConfig(false);
  const b = document.getElementById('apiTestBox');
  b.style.color = 'var(--accent-primary)';
  b.textContent = 'جاري اختبار إرسال صورة فعلية لنموذج الرؤية...';
  const res = await fetch('/api/test_ai', {method: 'POST'});
  const data = await res.json();
  b.style.color = data.ok ? 'var(--status-online)' : 'var(--status-danger)';
  // v16 #R13: نعرض الموديل اللي أجاب فعلاً، وسبب الانتقال للاحتياط لو صار
  b.textContent = (data.reply || '') + (data.model_used ? ' • الموديل الفعلي: ' + data.model_used : '') + (data.fallback_note ? ' • ' + data.fallback_note : '');
  refreshStatus();
}

// v16 #R13: جلب الموديلات المتاحة من المزود وتعبئة القائمة
async function fetchAiModels(btn) {
  const v = id => ((document.getElementById(id) || {}).value || '');
  const info = document.getElementById('aiModelsInfo');
  const orig = btn.textContent;
  btn.textContent = 'جاري الجلب...';
  btn.disabled = true;
  try {
    const r = await fetch('/api/ai/models', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        provider: v('ai_provider'),
        universal_base_url: v('universal_base_url'),
        universal_api_key: v('universal_api_key'),
        gemini_api_key: v('gemini_api_key'),
        ollama_url: v('ollama_url')
      })
    });
    const d = await r.json();
    const dl = document.getElementById('aiModelList');
    dl.innerHTML = '';
    if (d.ok) {
      for (const m of d.models) {
        const o = document.createElement('option');
        o.value = m;
        dl.appendChild(o);
      }
      info.style.color = 'var(--status-online)';
      info.textContent = '✓ ' + d.models.length + ' موديل متاح — اضغط على خانة اسم النموذج واختر';
    } else {
      info.style.color = 'var(--status-danger)';
      info.textContent = '✕ ' + (d.error || 'تعذر جلب الموديلات');
    }
  } catch (e) {
    info.style.color = 'var(--status-danger)';
    info.textContent = '✕ تعذر الاتصال بالبرنامج';
  } finally {
    btn.textContent = orig;
    btn.disabled = false;
  }
}

async function testTelegramAlertLive(btn) {
  const resTxt = document.getElementById('tgTestResult');
  const orig = btn.innerHTML;
  btn.innerHTML = 'جاري الإرسال...';
  btn.disabled = true;
  try {
    const resp = await fetch('/api/test_telegram', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        telegram_bot_token: document.getElementById('telegram_bot_token').value,
        telegram_chat_id: document.getElementById('telegram_chat_id').value
      })
    });
    const data = await resp.json();
    resTxt.style.color = data.ok ? 'var(--status-online)' : 'var(--status-danger)';
    resTxt.textContent = data.ok ? data.message : data.error;
  } catch (e) {
    resTxt.style.color = 'var(--status-danger)';
    resTxt.textContent = 'تعذر إرسال طلب الفحص';
  }
  btn.innerHTML = orig;
  btn.disabled = false;
}

async function clearEvents() {
  await fetch('/api/clear_events', {method: 'POST'});
  refreshStatus();
}

async function refreshStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    camStatuses = data.cameras || {};
    // v16 #R16: حالة go2rtc — نعيد رسم الشبكة لو تغيّر توفر البث عالي الدقة
    go2rtcState = Object.assign({ running: false, streams: [], port: 1985 }, data.go2rtc || {});
    const firstStatus = !go2rtcKnown;
    go2rtcKnown = true;
    if (!drawToolMode && (firstStatus || tilesNeedRerender())) renderMatrixCells();
    updateLiveModeBtn();
    const lg = document.getElementById('loadGuardBar');
    if (lg) {
      const load = data.load || {};
      if (load.level > 0 && load.msg) {
        lg.innerHTML = ic('triangle-alert') + '<span>' + escapeHtml(load.msg) + '</span>';
        lg.title = 'السبب: ' + (load.cause || '') + ' | الإجراءات: ' + ((load.actions || []).join('؛ ') || '-');
        lg.style.display = 'flex';
      } else {
        lg.style.display = 'none';
      }
    }
    const g2Txt = document.getElementById('go2rtcStatusTxt');
    if (g2Txt) {
      g2Txt.textContent = go2rtcState.running ? 'يعمل' : (go2rtcState.status_msg || 'متوقف');
      g2Txt.style.color = go2rtcState.running ? 'var(--status-online)' : 'var(--status-warning)';
    }
    const detBadge = document.getElementById('detectorWarnBadge');
    if (detBadge) {
      detBadge.style.display = data.low_accuracy_warning ? 'inline-flex' : 'none';
      detBadge.title = data.detector_status || '';
    }

    const cams = appCfg.cameras || [];
    const totalCams = cams.length;
    let onlineCams = 0;

    for (const c of cams) {
      const cid = Number(c.id);
      const st = camStatuses[cid] || {};
      const isOnline = Boolean(st.connected);
      const isAnalyzing = Boolean(st.analyzing);
      if (isOnline) onlineCams++;

      const dotEl = document.getElementById('tileDot_' + cid);
      if (dotEl) {
        dotEl.className = 'status-dot ' + (isAnalyzing ? 'dot-warning' : (isOnline ? 'dot-online' : 'dot-danger'));
      }
      const tagEl = document.getElementById('tileTag_' + cid);
      if (tagEl) {
        if (isAnalyzing) {
          tagEl.className = 'tile-state-tag tag-analyzing';
          tagEl.textContent = 'تحليل...';
          tagEl.style.display = 'inline-flex';
        } else if (!isOnline) {
          tagEl.className = 'tile-state-tag tag-offline';
          tagEl.textContent = 'التحليل منقطع';
          tagEl.style.display = 'inline-flex';
        } else {
          tagEl.style.display = 'none';
        }
      }
      const offBox = document.getElementById('tileOfflineBox_' + cid);
      if (offBox) offBox.style.display = (isOnline || tileUsesHq(cid)) ? 'none' : 'block';
      const offMsg = document.getElementById('tileOfflineMsg_' + cid);
      if (offMsg && st.status_msg) offMsg.textContent = st.status_msg;

      const metaEl = document.getElementById('tileMeta_' + cid);
      if (metaEl) {
        const hqHost = document.getElementById('cellVidHost_' + cid);
        const hqVid = hqHost ? hqHost.querySelector('video') : null;
        if (hqHost) {
          const dims = (hqVid && hqVid.videoWidth) ? `${hqVid.videoWidth}×${hqVid.videoHeight}` : 'جاري الاتصال...';
          let liveFps = '';
          if (hqVid && hqVid.getVideoPlaybackQuality) {
            const q = hqVid.getVideoPlaybackQuality(), nowT = performance.now();
            const prev = window.__liveFpsSample && window.__liveFpsSample[cid];
            if (prev && nowT - prev.t > 900) liveFps = ` @ ${Math.max(0, Math.round((q.totalVideoFrames - prev.f) / ((nowT - prev.t) / 1000)))}fps`;
            window.__liveFpsSample = window.__liveFpsSample || {};
            window.__liveFpsSample[cid] = { f: q.totalVideoFrames, t: nowT };
          }
          metaEl.textContent = `${dims}${liveFps} • الكشف بالخلفية: ${(st.detect_fps || 0) >= 1 ? (st.detect_fps || 0).toFixed(0) + ' إطار/ث' : 'متوقف'}`;
        } else {
          metaEl.textContent = `بث احتياطي (go2rtc غير متاح) • الكشف بالخلفية: ${(st.detect_fps || 0) >= 1 ? (st.detect_fps || 0).toFixed(0) + ' إطار/ث' : 'متوقف'}`;
        }
        if (st.clip_source) metaEl.title = `مصدر مقاطع الأحداث: ${st.clip_source} (${(st.clip_fps || 0).toFixed(1)} fps)` + (st.via_relay ? ' • التحليل عبر وسيط go2rtc (جلسة وحدة على المسجّل)' : '') + (st.analysis_note ? ' • ' + st.analysis_note : '');
        const qn = document.getElementById('cam_detect_quality_note');
        if (qn && Number(cid) === Number(focusedCamId) && st.analysis_note) qn.textContent = st.analysis_note;
      }
    }

    // تحديث مؤشر الحالة المدمج بصدق دون إظهار "تعمل" عندما تكون الحالة غير مختبرة (untested)
    const aiState = data.ai_state || 'untested';
    const aiLat = data.ai_latency_ms ? `${data.ai_latency_ms} ms` : 'لم يُختبر';
    const aiErr = data.ai_last_error ? ` | سبب آخر خطأ: ${data.ai_last_error}` : '';
    let aiShort = 'غير مختبر';
    let sysDotCls = 'dot-warning';
    if (aiState === 'analyzing') {
      aiShort = 'جارٍ التحليل';
      sysDotCls = 'dot-warning';
    } else if (aiState === 'last_succeeded' || aiState === 'verified_vision') {
      aiShort = 'نجح آخر تحليل';
      sysDotCls = (onlineCams === 0) ? 'dot-warning' : 'dot-online';
    } else if (aiState === 'vision_unsupported') {
      aiShort = 'لا يدعم الصور';
      sysDotCls = 'dot-danger';
    } else if (aiState === 'last_failed' || aiState === 'error') {
      aiShort = 'فشل آخر طلب';
      sysDotCls = 'dot-danger';
    } else {
      aiShort = 'غير مختبر';
      sysDotCls = 'dot-warning';
    }

    // 1. تحديث مؤشر الكاميرات
    const camDotEl = document.getElementById('sysStatusDot');
    if (camDotEl) {
      camDotEl.className = 'status-dot ' + ((onlineCams === totalCams && totalCams > 0) ? 'dot-online' : (onlineCams > 0 ? 'dot-warning' : 'dot-danger'));
    }
    const camTxtEl = document.getElementById('sysStatusText');
    if (camTxtEl) {
      camTxtEl.innerHTML = `<strong class="mono">${onlineCams}/${totalCams}</strong> كاميرات`;
    }
    const camPill = document.getElementById('sysCombinedPill');
    if (camPill) {
      camPill.title = `الكاميرات المتصلة: ${onlineCams} من ${totalCams}`;
    }

    // 2. تحديث مؤشر الذكاء الاصطناعي (AI Status)
    const aiH = data.ai_health || {};
    const aiDotEl = document.getElementById('sysAiDot');
    const aiTxtEl = document.getElementById('sysAiText');
    const aiPillEl = document.getElementById('sysAiPill');
    if (aiDotEl && aiTxtEl) {
      aiDotEl.className = 'status-dot ' + (aiH.dot || (aiH.working ? 'dot-online' : 'dot-warning'));
      const activeModelShort = (aiH.last_model_used || aiH.model || data.active_model || 'Gemini 3.8')
        .replace('gemini-', 'Gemini ').replace('-preview', '');
      const aiStatusWord = aiH.working ? 'يعمل' : (aiState === 'analyzing' ? 'تحليل...' : 'متوقف');
      aiTxtEl.innerHTML = `الذكاء: <strong>${aiStatusWord}</strong> (<span class="mono">${escapeHtml(activeModelShort)}</span>)`;
      if (aiPillEl) aiPillEl.title = aiH.tooltip || `حالة الذكاء: ${aiStatusWord} | النموذج: ${activeModelShort}`;
    }

    // 3. تحديث مؤشر ربط تيليجرام (Telegram Status)
    const tgH = data.telegram_health || {};
    const tgDotEl = document.getElementById('sysTgDot');
    const tgTxtEl = document.getElementById('sysTgText');
    const tgPillEl = document.getElementById('sysTgPill');
    if (tgDotEl && tgTxtEl) {
      const isTgOnline = Boolean(tgH.working);
      tgDotEl.className = 'status-dot ' + (isTgOnline ? 'dot-online' : (tgH.configured ? 'dot-warning' : 'dot-danger'));
      const botTag = tgH.bot_username ? ` (<span class="mono">@${escapeHtml(tgH.bot_username)}</span>)` : '';
      const tgStatusWord = isTgOnline ? 'متصل ويعمل' : (tgH.configured ? 'معطل' : 'غير مدخل');
      tgTxtEl.innerHTML = `تيليجرام: <strong>${tgStatusWord}</strong>${botTag}`;
      if (tgPillEl) tgPillEl.title = tgH.tooltip || `ربط تيليجرام: ${tgStatusWord}`;
    }

    const aiSetBadge = document.getElementById('aiSettingsBadge');
    if (aiSetBadge) {
      aiSetBadge.textContent = aiShort;
      aiSetBadge.style.color = (aiState === 'last_succeeded' || aiState === 'verified_vision')
        ? 'var(--status-online)'
        : ((aiState === 'untested' || aiState === 'analyzing') ? 'var(--status-warning)' : 'var(--status-danger)');
    }

    if (data.ai_latency_ms > 0) {
      document.getElementById('inspAi').innerHTML = `<span class="mono">${Number(data.ai_latency_ms)} ms</span>`;
    }

    updateInspectorValues();

    const newEvents = data.events || [];
    const sig = list => JSON.stringify(list.map(e => [e.id, e.ai_status, e.clip_status, e.telegram_status, e.mp4_url, e.clip_duration, e.description, e.plate_url, e.plate_text, e.plate_status, e.plate_conf, e.model_used, e.ai_plate_proposal, e.evidence_status, e.evidence_source, e.evidence_resolution, e.notified_plate_status]));
    const idsChanged = JSON.stringify(newEvents.map(e => e.id)) !== JSON.stringify(cachedEvents.map(e => e.id));
    const changed = sig(newEvents) !== sig(cachedEvents);
    const prevFirstId = cachedEvents.length ? cachedEvents[0].id : null;
    cachedEvents = newEvents;
    if (changed) {
      renderAllEventViews();
      if (selectedEventId) selectEventById(selectedEventId, true);
      // v16 #R16: نتابع أحدث حدث تلقائياً بس لو المستخدم ما اختار حدث قديم
      if (idsChanged && cachedEvents.length > 0 && (!selectedEventId || selectedEventId === prevFirstId)) {
        selectEventById(cachedEvents[0].id);
      }
      if (idsChanged && document.getElementById('view_events').classList.contains('active')) loadEventsArchive();
    }
    renderDevicesView();
  } catch (e) {}
}

async function loadInitialData() {
  try {
    const res = await fetch('/api/config');
    appCfg = await res.json();
    for (const k of GLOBAL_FIELDS) {
      if (document.getElementById(k) && appCfg[k] !== undefined) {
        document.getElementById(k).value = appCfg[k];
      }
    }
    if (appCfg.hikconnect) {
      if (appCfg.hikconnect.api_url) document.getElementById('hik_api_url').value = appCfg.hikconnect.api_url;
      if (appCfg.hikconnect.app_key) document.getElementById('hik_app_key').value = appCfg.hikconnect.app_key;
      if (appCfg.hikconnect.app_secret) document.getElementById('hik_app_secret').value = appCfg.hikconnect.app_secret;
    }
    if (appCfg.notifiers) {
      const nt = appCfg.notifiers.ntfy || {};
      if (document.getElementById('ntfy_enabled')) document.getElementById('ntfy_enabled').checked = Boolean(nt.enabled);
      if (document.getElementById('ntfy_server_url') && nt.server_url) document.getElementById('ntfy_server_url').value = nt.server_url;
      if (document.getElementById('ntfy_topic') && nt.topic) document.getElementById('ntfy_topic').value = nt.topic;
      if (document.getElementById('ntfy_token') && nt.token) document.getElementById('ntfy_token').value = nt.token;
      const dc = appCfg.notifiers.discord || {};
      if (document.getElementById('discord_enabled')) document.getElementById('discord_enabled').checked = Boolean(dc.enabled);
      if (document.getElementById('discord_webhook_url') && dc.webhook_url) document.getElementById('discord_webhook_url').value = dc.webhook_url;
      const sm = appCfg.notifiers.smtp || {};
      if (document.getElementById('smtp_enabled')) document.getElementById('smtp_enabled').checked = Boolean(sm.enabled);
      if (document.getElementById('smtp_host') && sm.host) document.getElementById('smtp_host').value = sm.host;
      if (document.getElementById('smtp_port') && sm.port) document.getElementById('smtp_port').value = sm.port;
      if (document.getElementById('smtp_username') && sm.username) document.getElementById('smtp_username').value = sm.username;
      if (document.getElementById('smtp_password') && sm.password) document.getElementById('smtp_password').value = sm.password;
      if (document.getElementById('smtp_to_addrs') && sm.to_addrs) document.getElementById('smtp_to_addrs').value = sm.to_addrs;
      const wa = appCfg.notifiers.whatsapp || {};
      if (document.getElementById('wa_enabled')) document.getElementById('wa_enabled').checked = Boolean(wa.enabled);
      if (document.getElementById('wa_mode') && wa.mode) document.getElementById('wa_mode').value = wa.mode;
      if (document.getElementById('wa_phone') && wa.phone) document.getElementById('wa_phone').value = wa.phone;
      if (document.getElementById('wa_apikey')) document.getElementById('wa_apikey').value = wa.callmebot_apikey || wa.access_token || '';
      if (document.getElementById('wa_phone_id') && wa.phone_number_id) document.getElementById('wa_phone_id').value = wa.phone_number_id;
    }
    if (appCfg.cloud_storage) {
      const cs = appCfg.cloud_storage;
      if (document.getElementById('cloud_enabled')) document.getElementById('cloud_enabled').checked = Boolean(cs.enabled);
      if (document.getElementById('cloud_confirmed_only')) document.getElementById('cloud_confirmed_only').checked = cs.confirmed_only !== false;
      if (document.getElementById('cloud_provider') && cs.provider) document.getElementById('cloud_provider').value = cs.provider;
      const s3 = cs.s3 || {};
      if (document.getElementById('s3_endpoint') && s3.endpoint_url) document.getElementById('s3_endpoint').value = s3.endpoint_url;
      if (document.getElementById('s3_bucket') && s3.bucket) document.getElementById('s3_bucket').value = s3.bucket;
      if (document.getElementById('s3_access_key') && s3.access_key) document.getElementById('s3_access_key').value = s3.access_key;
      if (document.getElementById('s3_secret_key') && s3.secret_key) document.getElementById('s3_secret_key').value = s3.secret_key;
      if (document.getElementById('s3_prefix') && s3.prefix) document.getElementById('s3_prefix').value = s3.prefix;
    }
    refreshStorageStats();
    onProviderChange(true);
    const camCount = (appCfg.cameras || []).length;
    currentGrid = camCount <= 1 ? 1 : (camCount <= 4 ? 4 : (camCount <= 9 ? 9 : (camCount <= 16 ? 16 : 32)));
    setGridLayout(currentGrid);
    selectCameraById(1);
    renderDevicesView();
    await refreshStatus();
  } catch (e) {}
}

function applyResponsiveDefaults() {
  const panel = document.getElementById('eventsSidePanel');
  const btn = document.getElementById('btnEventsPanelToggle');
  if (window.innerWidth <= 1366) {
    panel.classList.add('closed');
    if (btn) btn.classList.remove('active');
  }
}

window.addEventListener('hashchange', handleHashChange);
window.addEventListener('resize', applyResponsiveDefaults);
// v16 #R11: تكبير مشغل الحدث وملء الشاشة
function togglePlayerExpand(btn) {
  const grid = document.querySelector('.events-master-detail');
  if (!grid) return;
  const on = grid.classList.toggle('player-expanded');
  const b = btn || document.getElementById('playerExpandBtn');
  if (b) {
    b.classList.toggle('active', on);
    const t = b.querySelector('.btn-text');
    if (t) t.textContent = on ? 'تصغير' : 'تكبير';
  }
}

function togglePlayerFullscreen() {
  const v = document.getElementById('playerViewport');
  if (!v) return;
  if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
  } else if (v.requestFullscreen) {
    v.requestFullscreen().catch(() => {});
  }
}

document.addEventListener('click', () => {
  const menu = document.getElementById('moreGridMenu');
  if (menu) menu.classList.remove('open');
  const roiMenu = document.getElementById('roiClearMenu');
  if (roiMenu) roiMenu.classList.remove('open');
});

// v16 #9: مسح خط العبور / منطقة المراقبة مع تأكيد، للكاميرا المحددة أو لكل الكاميرات
function toggleRoiClearMenu(e) {
  e.stopPropagation();
  const gm = document.getElementById('moreGridMenu');
  if (gm) gm.classList.remove('open');
  document.getElementById('roiClearMenu').classList.toggle('open');
}

async function resetRoi(scope, which) {
  const menu = document.getElementById('roiClearMenu');
  if (menu) menu.classList.remove('open');
  const camLabel = 'CAM-' + String(focusedCamId).padStart(2, '0');
  const whatTxt = which === 'line' ? 'خط العبور' : (which === 'zone' ? 'منطقة المراقبة' : 'خط العبور ومنطقة المراقبة');
  const question = scope === 'all'
    ? ('مسح ' + whatTxt + ' من كل الكاميرات؟ بتحتاج ترسمها من جديد.')
    : ('مسح ' + whatTxt + ' من ' + camLabel + '؟');
  if (!confirm(question)) return;

  drawToolMode = null;
  polygonDraftPts = [];
  document.getElementById('drawLineBtn').classList.remove('active');
  document.getElementById('drawZoneBtn').classList.remove('active');
  document.querySelectorAll('.roi-canvas').forEach(cv => {
    cv.classList.remove('drawing-enabled');
    const cctx = cv.getContext('2d');
    cctx.clearRect(0, 0, cv.width, cv.height);
  });

  let data = null;
  try {
    const res = await fetch('/api/reset_roi', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ cam_id: scope === 'all' ? 'all' : focusedCamId, which: which })
    });
    data = await res.json();
  } catch (err) {
    data = null;
  }
  if (data && data.ok && Array.isArray(data.cameras)) {
    for (const u of data.cameras) {
      const c = (appCfg.cameras || []).find(x => Number(x.id) === Number(u.id));
      if (c) c.roi = u.roi;
    }
    showBannerNotice('✓ تم مسح ' + whatTxt + (scope === 'all' ? (' من ' + data.count + ' كاميرا') : (' من ' + camLabel)));
  } else {
    showBannerNotice('✕ تعذر المسح، حاول مرة ثانية');
  }
  setTimeout(() => showBannerNotice(''), 3000);
  renderDevicesView();
}

// ===================== v16 #R16: الإحصائيات (Chart.js محلي) =====================
let chartJsPromise = null;
function ensureChartJs() {
  if (window.Chart) return Promise.resolve(true);
  if (!chartJsPromise) {
    chartJsPromise = new Promise(resolve => {
      const s = document.createElement('script');
      s.src = '/assets/chart.umd.min.js';
      s.onload = () => resolve(true);
      s.onerror = () => { chartJsPromise = null; resolve(false); };
      document.head.appendChild(s);
    });
  }
  return chartJsPromise;
}

const CAT_COLORS = { person: '#2563EB', vehicle: '#C9A44A', animal: '#2E7D4F', other: '#A8A08C' };
const CAT_LABELS = { person: 'أشخاص', vehicle: 'مركبات', animal: 'حيوانات', other: 'أخرى' };

async function loadStatsView() {
  const daysEl = document.getElementById('statsDays');
  const days = Number(daysEl ? daysEl.value : 7) || 7;
  let data;
  try {
    data = await (await fetch('/api/stats?days=' + days)).json();
  } catch (e) {
    return;
  }
  const kpi = (icon, label, val) => `<div class="kpi-card"><span class="kpi-label">${ic(icon)}${label}</span><span class="kpi-val mono">${val}</span></div>`;
  const pct = data.total ? Math.round(data.confirmed * 100 / data.total) + '%' : '--';
  const sec = ms => !ms ? '--' : (ms < 1000 ? ms + ' ms' : (ms / 1000).toFixed(1) + ' ث');
  document.getElementById('statsKpis').innerHTML =
    kpi('activity', 'إجمالي الأحداث', data.total) +
    kpi('bell', 'أحداث مهمة', data.important) +
    kpi('shield-check', 'نسبة التأكيد', pct) +
    kpi('car', 'لوحات مقروءة', data.plates) +
    kpi('cpu', 'متوسط زمن التحليل', sec(data.avg_ai_ms)) +
    kpi('zap', 'متوسط زمن التنبيه', sec(data.avg_alert_ms));
  const note = document.getElementById('statsNote');
  note.textContent = `محسوبة من ${data.stored_events} حدث محفوظ في السجل (الأحداث اللي انحذفت بالتنظيف التلقائي ما تدخل).`;
  if (!(await ensureChartJs())) {
    note.textContent += ' • تعذر تحميل ملف الرسوم البيانية (cctv_agent_data/static/chart.umd.min.js).';
    return;
  }
  Chart.defaults.font.family = "'IBM Plex Sans Arabic', 'Segoe UI', Tahoma, sans-serif";
  Chart.defaults.color = '#6F6A5E';
  const cats = ['person', 'vehicle', 'animal', 'other'];
  const mk = (id, cfg) => {
    if (statsCharts[id]) statsCharts[id].destroy();
    const cv = document.getElementById(id);
    if (cv) statsCharts[id] = new Chart(cv, cfg);
  };
  const legend = { position: 'bottom', rtl: true, labels: { boxWidth: 10, boxHeight: 10 } };
  mk('chHourly', {
    type: 'bar',
    data: { labels: data.hourly.labels, datasets: cats.map(c => ({ label: CAT_LABELS[c], data: data.hourly.series[c], backgroundColor: CAT_COLORS[c], borderRadius: 3, stack: 's' })) },
    options: { maintainAspectRatio: false, plugins: { legend },
      scales: { x: { stacked: true, grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 8 } }, y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } } } }
  });
  mk('chCats', {
    type: 'doughnut',
    data: { labels: cats.map(c => CAT_LABELS[c]), datasets: [{ data: cats.map(c => data.by_category[c]), backgroundColor: cats.map(c => CAT_COLORS[c]), borderWidth: 2, borderColor: '#FFFFFF' }] },
    options: { maintainAspectRatio: false, cutout: '62%', plugins: { legend } }
  });
  mk('chHourOfDay', {
    type: 'bar',
    data: { labels: Array.from({ length: 24 }, (_, h) => String(h).padStart(2, '0')), datasets: [{ label: 'الأحداث', data: data.hour_of_day, backgroundColor: '#C9A44A', borderRadius: 3 }] },
    options: { maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { grid: { display: false } }, y: { beginAtZero: true, ticks: { precision: 0 } } } }
  });
  const camKeys = Object.keys(data.by_camera || {});
  mk('chCams', {
    type: 'bar',
    data: { labels: camKeys, datasets: [{ label: 'الأحداث', data: camKeys.map(k => data.by_camera[k]), backgroundColor: '#1C1A16', borderRadius: 4 }] },
    options: { indexAxis: 'y', maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { beginAtZero: true, ticks: { precision: 0 } }, y: { grid: { display: false } } } }
  });
}

// ===================== v16 #R16: معاينة الحدث عند تمرير الماوس (فكرة Frigate) =====================
(function initEventHoverPreview() {
  let cur = null;
  const box = () => document.getElementById('evHoverPreview');
  const place = e => {
    const b = box();
    if (!b) return;
    const w = 332, h = 240;
    let x = e.clientX + 18, y = e.clientY + 14;
    if (x + w > window.innerWidth) x = e.clientX - w - 12;
    if (y + h > window.innerHeight) y = window.innerHeight - h - 8;
    b.style.left = Math.max(8, x) + 'px';
    b.style.top = Math.max(8, y) + 'px';
  };
  document.addEventListener('mouseover', e => {
    const t = e.target && e.target.closest ? e.target.closest('[data-prev-id]') : null;
    if (!t || t === cur) return;
    if (!window.matchMedia || !window.matchMedia('(hover: hover)').matches) return;
    const ev = findEvent(t.dataset.prevId);
    const b = box();
    if (!ev || !b || !ev.image_url) return;
    cur = t;
    b.querySelector('img').src = ev.image_url;
    b.querySelector('.hp-cap').textContent = `CAM-${String(ev.cam_id || 1).padStart(2, '0')} • ${ev.timestamp || ''} — ${ev.trigger || ev.description || ''}`;
    b.style.display = 'block';
    place(e);
  });
  document.addEventListener('mousemove', e => { if (cur) place(e); });
  document.addEventListener('mouseout', e => {
    if (cur && !(e.relatedTarget && cur.contains(e.relatedTarget))) {
      cur = null;
      const b = box();
      if (b) b.style.display = 'none';
    }
  });
})();

window.addEventListener('DOMContentLoaded', () => {
  applyResponsiveDefaults();
  handleHashChange();
  loadInitialData();
  setInterval(refreshStatus, 1400);
});
