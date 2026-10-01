# Sidecar do KrewHub para a extensão AWS SSO: imagem oficial do
# aws-sso-cli (synfinatic/aws-sso-cli-ecs-server) + python3 + supervisor.
# A base oficial só existe para linux/amd64 (sem arm64).
# Build (a partir de extensions/aws-sso/):
#   docker buildx build --platform linux/amd64 -t <registry>/krewhub-ext-aws-sso:<tag> .
# Ao atualizar o aws-sso-cli, troque a tag E o digest juntos (o digest é
# o que de fato fixa a base; a tag só documenta a versão).
FROM docker.io/synfinatic/aws-sso-cli-ecs-server:v2.3.2@sha256:67b689d7bc014919150c0ae0eaa0bee3fc9911e3c0a1f29de97748966b013d2e

# python3: supervisor e driver pty de login (pod_exec.run_detached).
# O binário oficial fica em /app/aws-sso; o link o põe no PATH.
RUN apk add --no-cache python3 \
 && ln -s /app/aws-sso /usr/local/bin/aws-sso
COPY supervisor.py /usr/local/bin/krewhub-aws-sso-supervisor
RUN chmod 0755 /usr/local/bin/krewhub-aws-sso-supervisor

USER 1000:1000
# Substitui o ENTRYPOINT da base (`./aws-sso ecs server --docker`, que
# ignora a config e escuta em 0.0.0.0): o supervisor sobe o servidor ECS
# em 127.0.0.1 com a config e o bearer do KrewHub.
ENTRYPOINT ["python3", "/usr/local/bin/krewhub-aws-sso-supervisor"]
CMD []
