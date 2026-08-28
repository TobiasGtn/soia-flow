#!/usr/bin/env bash
# Instalação do Transcritor Desktop — rodar uma única vez no Git Bash.
set -e
cd "$(dirname "$0")"

echo "1/3 — Criando ambiente Python..."
python -m venv venv

echo "2/3 — Instalando dependências..."
./venv/Scripts/python -m pip install --upgrade pip -q
./venv/Scripts/python -m pip install -r requirements.txt -q

echo "3/3 — Pronto!"
echo ""
echo "Para abrir o aplicativo: dê dois cliques em Transcritor.bat"
