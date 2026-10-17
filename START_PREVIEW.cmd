@echo off
chcp 65001 >nul
title Hikvision V18.1 - Preview Mode
echo ===================================================
echo     Hikvision AI Security Center V18.1
echo     تشغيل وضع المعاينة وتجربة الواجهة (بدون كاميرات)
echo ===================================================
echo.
python hikvision_pro_v18.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [تنبيه] حدث خطأ أثناء التشغيل.
    pause
)
