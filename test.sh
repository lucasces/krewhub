#!/usr/bin/env bash
# Roda a suite de testes (rapida, offline -- sem cluster real, sem rede
# real, ver tests/conftest.py) com UMA linha:
#
#   ./test.sh                 # roda tudo
#   ./test.sh -k lobby        # so os testes que batem "lobby"
#   ./test.sh -x -v           # para no primeiro erro, verboso
#
# python3 nao esta' no PATH por padrao neste NixOS (ver skill
# nix-develop) -- se ~/.venv ainda nao existir, este script bootstrapa
# um via `nix develop` (devshell node-22, que inclui python3.13) antes
# de instalar as dependencias de teste nele. Depois disso, roda tudo
# direto pelo .venv/bin/python (que e' auto-contido -- nao precisa mais
# de nix develop nas chamadas seguintes).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

VENV_PY=".venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
    echo "==> .venv/ nao existe -- criando (python3 via nix develop, nao esta' no PATH por padrao)"
    if command -v python3 >/dev/null 2>&1; then
        python3 -m venv .venv
    else
        nix develop ~/personal/nixos#node-22 --command python3 -m venv .venv
    fi
fi

if ! "$VENV_PY" -m pytest --version >/dev/null 2>&1; then
    echo "==> instalando dependencias (requirements.txt + requirements-dev.txt) no .venv/"
    "$VENV_PY" -m pip install --quiet -r requirements.txt -r requirements-dev.txt
fi

exec "$VENV_PY" -m pytest "$@"
