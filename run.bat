@echo off
chcp 65001 >nul
cd /d "%~dp0"
title بوت تحميل الفيديوهات

:loop
echo [%date% %time%] تشغيل البوت...
python -m bot
echo [%date% %time%] توقف البوت، إعادة التشغيل بعد 5 ثوانٍ...
timeout /t 5 /nobreak >nul
goto loop
