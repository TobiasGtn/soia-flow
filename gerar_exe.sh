#!/usr/bin/env bash
# Gera o executável dist/SOIAFlow.exe — rodar no Git Bash.
set -e
cd "$(dirname "$0")"

./venv/Scripts/python gerar_icone.py
./venv/Scripts/pyinstaller --onefile --noconsole \
  --name SOIAFlow \
  --icon soiaflow.ico \
  --hidden-import keyring.backends.Windows \
  transcritor.py

echo ""
echo "Pronto: dist/SOIAFlow.exe"
