@echo off
chcp 65001 >nul
title Hikvision V18.1 - System Check
echo ===================================================
echo     Hikvision AI Security Center V18.1
echo     فحص بيئة العمل والمكتبات والأدوات والنماذج
echo ===================================================
echo.
python hikvision_pro_v18.py --check
echo.
echo ===================================================
echo إذا ظهرت النتيجة "local_dependencies_ready: true" فالنظام جاهز تماماً!
echo ===================================================
pause
