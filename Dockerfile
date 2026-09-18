# KrewHub central -- imagem mínima pra rodar o serviço dentro do cluster
# (antes rodava só localmente, lendo o kubeconfig pessoal do operador --
# ver README, seção "Deploy no cluster").
FROM docker.io/library/python:3.12-slim

# Sem root em runtime -- mesma régua de hardening já usada em kirocrew/CHP
# (drop de privilégio, non-root, sem escrita no rootfs além de /tmp).
RUN groupadd --gid 1000 krewhub && \
    useradd --uid 1000 --gid krewhub --create-home --shell /usr/sbin/nologin krewhub

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

USER krewhub

EXPOSE 8080

# in-cluster: kubernetes.config.load_incluster_config() (ver app/k8s_manager.py)
# -- sem kubeconfig montado, usa o token do ServiceAccount automaticamente.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
