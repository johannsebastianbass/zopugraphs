@echo off
REM Setup completo do painel ZOPU numa maquina nova.
REM Requer um arquivo .env preenchido (copie de .env.example).
cd /d %~dp0
set PYTHONIOENCODING=utf-8

if not exist .env (
    echo [!] Arquivo .env nao encontrado. Copie .env.example para .env e preencha.
    copy .env.example .env >nul
    echo [i] Criei um .env a partir do exemplo. Edite-o e rode setup.bat de novo.
    exit /b 1
)

echo [*] Instalando dependencias...
python -m pip install -q -r requirements.txt

echo [*] Configurando banco e sincronizando...
python bootstrap.py
