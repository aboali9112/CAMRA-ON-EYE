// تشخيص النظام ووضع المعاينة (بدون عرض كلمات مرور أو روابط كاميرات سرية)
(function() {
  function initDiagnostics() {
    const navList = document.querySelector('.nav-list');
    if (!navList) return;

    const diagnosticsButton = document.createElement('button');
    diagnosticsButton.className = 'nav-item';
    diagnosticsButton.innerHTML = '<svg class="ico" viewBox="0 0 24 24"><path d="M12 20v2M12 2v2M17 20v2M17 2v2M2 12h2M2 17h2M2 7h2M20 12h2M20 17h2M20 7h2M7 20v2M7 2v2"/><rect x="4" y="4" width="16" height="16" rx="2"/></svg><span class="nav-label">فحص النظام</span>';
    diagnosticsButton.id = 'nav_diagnostics';
    diagnosticsButton.title = 'فحص صحة التسجيل، الكاميرات، ونماذج التحليل وجميع المكتبات';
    navList.appendChild(diagnosticsButton);

    const diagnosticsDialog = document.createElement('dialog');
    diagnosticsDialog.id = 'diagnosticsDialog';
    diagnosticsDialog.style.maxHeight = '90vh';
    diagnosticsDialog.style.overflowY = 'auto';
    diagnosticsDialog.innerHTML = '<form method="dialog" style="float:left;"><button class="btn-tool">إغلاق ✕</button></form><h2 style="margin-top:0;">حالة النظام · V18.1</h2><div id="diagnosticsContent">جارٍ فحص الحالة…</div>';
    document.body.appendChild(diagnosticsDialog);

    const openDiag = () => {
      diagnosticsDialog.showModal();
      refreshDiagnostics();
    };
    diagnosticsButton.addEventListener('click', openDiag);
    ['sysCombinedPill', 'sysAiPill', 'sysTgPill'].forEach(id => {
      const el = document.getElementById(id);
      if (el) {
        el.style.cursor = 'pointer';
        el.addEventListener('click', openDiag);
      }
    });
  }

  async function refreshDiagnostics() {
    const target = document.getElementById('diagnosticsContent');
    if (!target) return;
    try {
      const response = await fetch('/api/diagnostics');
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const d = await response.json();
      const esc = (typeof escapeHtml === 'function') ? escapeHtml : (s => String(s || ''));
      const aiH = d.ai_health || {};
      const tgH = d.telegram_health || {};
      const libs = d.system_libraries || [];
      const tools = (d.system_tools || {}).binaries || [];
      const models = (d.system_tools || {}).models || [];

      target.innerHTML = `
        <!-- 1. البطاقات الرئيسية للمحركات الأساسية -->
        <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:12px; margin-bottom:16px;">
          <div style="padding:12px 14px; background:var(--bg-surface-2); border-radius:10px; border:1px solid var(--border-subtle);">
            <div style="font-size:11.5px; color:var(--text-muted); margin-bottom:4px;">حالة الذكاء الاصطناعي (AI)</div>
            <div style="font-size:14px; font-weight:600; display:flex; align-items:center; gap:6px;">
              ${aiH.working ? '<span class="status-dot dot-online"></span><span style="color:#2ecc71;">يعمل بنجاح</span>' : '<span class="status-dot dot-danger"></span><span style="color:#e74c3c;">متوقف</span>'}
            </div>
            <div style="font-size:12px; margin-top:6px; color:var(--text-secondary);">
              النموذج: <span class="mono" style="font-weight:600; color:var(--text-primary);">${esc(aiH.model || 'Gemini 3.8')}</span>
              ${aiH.latency_ms ? ` · <span class="mono">${aiH.latency_ms} ms</span>` : ''}
            </div>
          </div>

          <div style="padding:12px 14px; background:var(--bg-surface-2); border-radius:10px; border:1px solid var(--border-subtle);">
            <div style="font-size:11.5px; color:var(--text-muted); margin-bottom:4px;">حالة ربط تيليجرام (Telegram)</div>
            <div style="font-size:14px; font-weight:600; display:flex; align-items:center; gap:6px;">
              ${tgH.working ? '<span class="status-dot dot-online"></span><span style="color:#2ecc71;">متصل وجاهز</span>' : '<span class="status-dot dot-danger"></span><span style="color:#e74c3c;">غير متصل</span>'}
            </div>
            <div style="font-size:12px; margin-top:6px; color:var(--text-secondary);">
              البوت: <span class="mono" style="font-weight:600; color:var(--text-primary);">${tgH.bot_username ? '@' + esc(tgH.bot_username) : 'غير محدد'}</span>
              ${tgH.last_accept_ms ? ` · <span class="mono">${tgH.last_accept_ms} ms</span>` : ''}
            </div>
          </div>

          <div style="padding:12px 14px; background:var(--bg-surface-2); border-radius:10px; border:1px solid var(--border-subtle);">
            <div style="font-size:11.5px; color:var(--text-muted); margin-bottom:4px;">وضع النظام والتسجيل</div>
            <div style="font-size:14px; font-weight:600; display:flex; align-items:center; gap:6px;">
              ${d.preview ? '<span class="status-dot dot-warning"></span><span style="color:#e67e22;">معاينة معزولة</span>' : '<span class="status-dot dot-online"></span><span style="color:#2ecc71;">تشغيل مباشر نشط</span>'}
            </div>
            <div style="font-size:12px; margin-top:6px; color:var(--text-secondary);">
              مسجّل FFmpeg: ${d.ffmpeg_available ? '<span style="color:#2ecc71; font-weight:600;">متاح (نسخ البث)</span>' : '<span style="color:#e74c3c;">غير متاح</span>'}
            </div>
          </div>
        </div>

        <!-- 2. جدول حالة عمل جميع المكتبات والتبعيات البرمجية -->
        <div style="margin-top:18px; margin-bottom:16px;">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
            <h3 style="font-size:13.5px; font-weight:700; margin:0; display:flex; align-items:center; gap:6px; color:var(--text-primary);">
              <svg class="ico" viewBox="0 0 24 24" style="width:16px; height:16px; color:var(--accent-primary);"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M9 9h6M9 13h6M9 17h4"/></svg>
              حالة عمل جميع المكتبات والتبعيات (System Libraries)
            </h3>
            <span style="font-size:11px; color:var(--text-muted); background:var(--bg-surface-2); padding:2px 8px; border-radius:6px; border:1px solid var(--border-subtle);">
              بيئة التشغيل: <strong class="mono">Python 3.12</strong>
            </span>
          </div>

          <div class="diagnostics-table" style="border:1px solid var(--border-subtle); border-radius:10px; overflow:hidden;">
            <table>
              <thead>
                <tr style="background:var(--bg-surface-2); font-size:12px;">
                  <th>المكتبة / الأداة</th>
                  <th>الإصدار</th>
                  <th>الحالة التشغيلية</th>
                  <th>التسريع العتادي / الملاحظات</th>
                  <th>الوظيفة في النظام</th>
                </tr>
              </thead>
              <tbody style="font-size:12.5px;">
                ${libs.map(lib => `
                  <tr>
                    <td><strong>${esc(lib.name)}</strong></td>
                    <td><span class="mono" style="color:var(--text-secondary);">${esc(lib.version)}</span></td>
                    <td>
                      <span class="status-dot ${esc(lib.dot)}"></span>
                      <span style="font-weight:600; color:${lib.working ? '#2ecc71' : '#e74c3c'};">${esc(lib.status_label)}</span>
                    </td>
                    <td><small style="color:var(--text-primary); font-weight:500;">${esc(lib.extra || 'طبيعي')}</small></td>
                    <td><small style="color:var(--text-muted);">${esc(lib.description)}</small></td>
                  </tr>
                `).join('')}

                ${tools.map(tool => `
                  <tr>
                    <td><strong>${esc(tool.name)}</strong></td>
                    <td><span class="mono" style="color:var(--text-muted);">أداة تنفيذية</span></td>
                    <td>
                      <span class="status-dot ${esc(tool.dot)}"></span>
                      <span style="font-weight:600; color:${tool.installed ? '#2ecc71' : '#e74c3c'};">${esc(tool.status_label)}</span>
                    </td>
                    <td><small style="color:var(--text-secondary);">مجلد bin</small></td>
                    <td><small style="color:var(--text-muted);">${esc(tool.description)}</small></td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>
        </div>

        <!-- 3. شريط نماذج الكشف المتوفرة -->
        <div style="padding:10px 14px; background:var(--bg-surface-2); border-radius:8px; border:1px solid var(--border-subtle); margin-bottom:18px; font-size:12px; display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px;">
          <div>
            <strong style="color:var(--text-primary);">نماذج YOLO المتوفرة محلياً:</strong>
            <span class="mono" style="margin-right:6px; color:var(--accent-primary); font-weight:600;">${(models.join(' · ') || 'لا توجد نماذج')}</span>
          </div>
          <div style="color:var(--text-muted);">
            المحرك العصبي: <strong style="color:#2ecc71;">Ultralytics YOLO11 (GPU Accelerated)</strong>
          </div>
        </div>

        <!-- 4. جدول الكاميرات المباشرة -->
        <p style="color:var(--text-muted); font-size:12px; margin-bottom:8px;">${esc(d.notification_note)}</p>
        <div class="diagnostics-table" style="border:1px solid var(--border-subtle); border-radius:10px; overflow:hidden;">
          <table>
            <thead>
              <tr style="background:var(--bg-surface-2); font-size:12px;">
                <th>الكاميرا</th>
                <th>بث التحليل</th>
                <th>حالة التسجيل</th>
                <th>طابور AI</th>
              </tr>
            </thead>
            <tbody style="font-size:12.5px;">
              ${(d.cameras || []).map(c => `
                <tr>
                  <td><b>CAM-${c.id}</b></td>
                  <td>
                    ${c.analysis_connected ? '<span style="color:#2ecc71;">● متصل</span>' : '<span style="color:#e74c3c;">● غير متصل</span>'}
                    <small>${esc(c.analysis_stream)} ${c.analysis_note ? '· ' + esc(c.analysis_note) : ''}</small>
                  </td>
                  <td>
                    ${c.recording_healthy ? '<span style="color:#2ecc71;">يكتب مقاطع سليمة</span>' : '<span style="color:#e67e22;">' + esc(c.recording_status) + '</span>'}
                  </td>
                  <td><span class="mono">${c.pending_ai}</span></td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      `;
    } catch(e) {
      target.textContent = 'تعذر جلب حالة النظام؛ تحقق من تشغيل الخادم.';
    }
  }

  fetch('/api/diagnostics').then(r => r.json()).then(d => {
    if (d.preview) {
      const banner = document.createElement('div');
      banner.className = 'v18-preview-banner';
      banner.textContent = '⚠️ وضع المعاينة المعزول — الكاميرات والإشعارات الخارجية متوقفة لمنع التعارض';
      const body = document.querySelector('.app-body');
      if (body) body.before(banner);
    }
  }).catch(() => {});

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initDiagnostics);
  } else {
    initDiagnostics();
  }
})();
