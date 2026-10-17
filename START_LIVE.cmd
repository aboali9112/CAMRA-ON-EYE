@echo off
chcp 65001 >nul
title Hikvision V18.1 - Live Security Center
echo ===================================================
echo     Hikvision AI Security Center V18.1
echo     تشغيل البث المباشر الكامل مع الكاميرات
echo ===================================================
echo.
python hikvision_pro_v18.py --live
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [تنبيه] حدث خطأ أثناء التشغيل. يرجى التأكد من تشغيل INSTALL_DEPENDENCIES.cmd
    pause
)
