@echo off
chcp 65001 >nul
title Hikvision V18.1 - تثبيت متطلبات النظام
echo ===================================================
echo     Hikvision AI Security Center V18.1
echo     تثبيت مكتبات بايثون والمتطلبات الذكية
echo ===================================================
echo.
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo.
echo ===================================================
echo تم الانتهاء من التثبيت!
echo لتأكيد الجاهزية، شغّل: CHECK_SYSTEM.cmd
echo ===================================================
pause
