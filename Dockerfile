# KrewHub central -- imagem minima pra rodar o servico dentro do cluster
# (antes rodava so localmente, lendo o kubeconfig pessoal do operador --
# ver README, secao "Deploy no cluster").
FROM docker.io/library/python:3.12-slim

# uv (binario estatico, so copiado da imagem oficial publicada pela
# Astral -- nao e' um build stage nosso, so uma COPY --from= de uma
# imagem pronta) gerencia deps a partir de pyproject.toml/uv.lock (ver
# README, secao "Rodando local"). Versao pinada pra bater com a usada
# pra gerar o uv.lock commitado.
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /uvx /usr/local/bin/

# Sem root em runtime -- mesma regua de hardening ja usada em kirocrew/CHP
# (drop de privilegio, non-root, sem escrita no rootfs alem de /tmp).
RUN groupadd --gid 1000 krewhub && \
    useradd --uid 1000 --gid krewhub --create-home --shell /usr/sbin/nologin krewhub

WORKDIR /app

# So pyproject.toml/uv.lock primeiro -- cache de layer do Docker: um
# `COPY app/` depois nao invalida a camada de deps se so o codigo mudou.
# --frozen: falha em vez de atualizar o lock se pyproject.toml/uv.lock
# saírem de sincronia -- build reprodutivel, nunca resolve versao nova
# na imagem. --no-dev: pytest/httpx2 (grupo dev) ficam de fora, mesmo
# principio do requirements-dev.txt antigo (nunca vao pra imagem).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app/ ./app/

USER krewhub

# .venv/bin na frente do PATH -- roda uvicorn (e qualquer python) direto
# do venv gerenciado pelo uv, sem precisar de `uv run` em runtime.
ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8080

# in-cluster: kubernetes.config.load_incluster_config() (ver app/k8s_manager.py)
# -- sem kubeconfig montado, usa o token do ServiceAccount automaticamente.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
