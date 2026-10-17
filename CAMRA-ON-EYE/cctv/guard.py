# -*- coding: utf-8 -*-
"""cctv.guard — الحماية الذاتية (v17 #R21: مرتبطة بمصدر الحمل، مع إظهار السبب والإجراء الفعلي)"""
import time
import threading


class LoadGuard:
    '''
    يقيس المعالج وكرت الشاشة كل 5 ثواني. لو ظلوا فوق الحد 20 ثانية:
      - لو فيه كاميرات بث تحليلها "الرئيسي" (4K/12MP تنفك في بايثون) → يحوّلها مؤقتاً للفرعي (أكبر مصدر حمل) ويقول ذلك.
      - وإلا يخفّض معدل الكشف (النصف ثم الربع).
    ويرجع تدريجياً بعد 60 ثانية هدوء. التسجيل (ffmpeg) ما يتأثر. القراءة غير المتاحة تظهر "غير متاح" مو صفر.
    '''
    CPU_HOT = 90.0
    GPU_HOT = 95.0

    def __init__(self, engine, start_thread=True):
        self.engine = engine
        self.cpu = None
        self.gpu = None
        self.level = 0
        self.det_scale = 1.0
        self.msg = ""
        self.cause = ""
        self.actions = []
        self.hot_since = None
        self.cool_since = None
        self._nvml = None
        self._switched = []
        if start_thread:
            threading.Thread(target=self._loop, daemon=True, name="load-guard").start()

    def _gpu_util(self):
        try:
            if self._nvml is None:
                import pynvml
                pynvml.nvmlInit()
                self._nvml = (pynvml, pynvml.nvmlDeviceGetHandleByIndex(0))
            pynvml, h = self._nvml
            return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)
        except Exception:
            self._nvml = False
            return None

    def _sample(self):
        try:
            import psutil
            self.cpu = float(psutil.cpu_percent(interval=None))
        except Exception:
            self.cpu = None
        self.gpu = self._gpu_util() if self._nvml is not False else None

    def _main_cams(self):
        try:
            return [cid for cid, w in list(self.engine.workers.items())
                    if w.running and w.analysis_stream_effective() == "main" and not w.analysis_override]
        except Exception:
            return []

    def _switch_to_sub(self, cams):
        done = []
        for cid in cams:
            try:
                if self.engine.workers[cid].set_analysis_override("sub"):
                    done.append(cid)
            except Exception:
                pass
        return done

    def _restore(self):
        for cid in list(self._switched):
            try:
                self.engine.workers[cid].set_analysis_override(None)
            except Exception:
                pass
        self._switched = []

    def apply(self, cpu, gpu, now, main_cams=None, switcher=None):
        '''منطق القرار (معزول للاختبار). main_cams: كاميرات بث تحليلها الرئيسي؛ switcher: دالة تحويلها للفرعي'''
        self.cpu, self.gpu = cpu, gpu
        hot = (cpu is not None and cpu >= self.CPU_HOT) or (gpu is not None and gpu >= self.GPU_HOT)
        prev_level = self.level
        if hot:
            self.cool_since = None
            self.hot_since = self.hot_since or now
            if now - self.hot_since >= 20 and self.level < 2:
                self.level += 1
                self.hot_since = now
        else:
            self.hot_since = None
            if self.level > 0:
                self.cool_since = self.cool_since or now
                if now - self.cool_since >= 60:
                    self.level -= 1
                    self.cool_since = now
        if self.level > prev_level and main_cams:
            switched = switcher(main_cams) if switcher else list(main_cams)
            self._switched = sorted(set(self._switched) | set(switched))
            if switched:
                self.actions = [f"تحويل بث التحليل مؤقتاً إلى الفرعي للكاميرات {', '.join('CAM-%02d' % c for c in switched)}"]
                # التحويل نفسه هو الإجراء؛ ما نخفّض الكشف في نفس الدرجة
                self.level = max(1, self.level)
        if self.level == 0:
            if self._switched:
                if switcher is None:
                    self._switched = []
                else:
                    self._restore()
            self.det_scale = 1.0
            self.msg, self.cause, self.actions = "", "", []
            return self.level
        if self._switched and self.level == 1:
            self.det_scale = 1.0
        else:
            self.det_scale = {1: 0.5, 2: 0.25}[self.level]
            self.actions = [a for a in self.actions if not a.startswith("خفض")] + [f"خفض معدل الكشف إلى {int(self.det_scale * 100)}%"]
        cpu_s = "غير متاح" if cpu is None else f"{int(cpu)}%"
        gpu_s = "غير متاح" if gpu is None else f"{int(gpu)}%"
        if main_cams or self._switched:
            self.cause = "بث التحليل على البث الرئيسي (4K/12MP تنفك في بايثون)"
        elif gpu is not None and gpu >= self.GPU_HOT:
            self.cause = "كرت الشاشة مشغول (كاميرات كثيرة أو معدل كشف عالي)"
        else:
            self.cause = "المعالج مشغول (برامج أخرى أو تبويبات MJPEG كثيرة أو مقاطع بالطريقة القديمة)"
        self.msg = f"حمل مرتفع (المعالج {cpu_s} / كرت الشاشة {gpu_s}). السبب المرجّح: {self.cause}. الإجراء: {'؛ '.join(self.actions) or 'مراقبة'}"
        return self.level

    def _loop(self):
        try:
            import psutil
            psutil.cpu_percent(interval=None)
        except Exception:
            pass
        while True:
            time.sleep(5.0)
            try:
                self._sample()
                self.apply(self.cpu, self.gpu, time.time(), main_cams=self._main_cams(), switcher=self._switch_to_sub)
            except Exception:
                pass

    def status(self):
        return {"cpu": (None if self.cpu is None else round(self.cpu)), "gpu": (None if self.gpu is None else round(self.gpu)),
                "cpu_available": self.cpu is not None, "gpu_available": self.gpu is not None,
                "level": self.level, "det_scale": self.det_scale, "msg": self.msg, "cause": self.cause,
                "actions": list(self.actions), "switched_cams": list(self._switched)}
