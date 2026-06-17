@echo off
REM Sincronizacao incremental ZOPU (Bitrix -> SQLite). Executado de hora em hora.
cd /d C:\zopugraphs
set PYTHONIOENCODING=utf-8
"C:\Users\puran\AppData\Local\Python\pythoncore-3.14-64\python.exe" sync.py >> C:\zopugraphs\sync.log 2>&1
