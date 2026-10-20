@echo off
cd /d "%~dp0"
echo 正在启动 天行 AI-CEO 经营早报 ...
echo 浏览器打开 http://127.0.0.1:8000
"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe" app.py 8000
pause
