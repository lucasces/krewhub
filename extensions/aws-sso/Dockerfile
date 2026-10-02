# Imagem da extensão AWS SSO do KrewHub, usada em dois papéis do mesmo Pod:
# sidecar (aws-sso-cli + supervisor) e initContainer `aws-sso-tools`, que
# copia o AWS CLI v2 (/opt/krewhub-tools) pro volume lido pelo `kirocrew`.
# Build (a partir de extensions/aws-sso/):
#   docker buildx build --platform linux/amd64,linux/arm64 -t <registry>/krewhub-ext-aws-sso:<tag> .
FROM debian:bookworm-slim AS fetch
ARG TARGETARCH
ARG AWS_SSO_VERSION=2.3.2
ARG SHA256_AMD64=0347a91f65680db4ab5f6bde85afc4265e677389581ac920bb2536bd05a6f5de
ARG SHA256_ARM64=c1c336a75e2ca82631a2a32b09c185b1d961d8efcaf949fd82baf4f82dd82fc3
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) sum="$SHA256_AMD64" ;; \
      arm64) sum="$SHA256_ARM64" ;; \
      *) echo "arquitetura não suportada: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /aws-sso \
      "https://github.com/synfinatic/aws-sso-cli/releases/download/v${AWS_SSO_VERSION}/aws-sso-${AWS_SSO_VERSION}-linux-${TARGETARCH}"; \
    echo "${sum}  /aws-sso" | sha256sum -c -; \
    chmod 0755 /aws-sso

# AWS CLI v2 (bundle autocontido, precisa só de glibc): versão e SHA-256 fixos
# por arquitetura, baixados de awscli.amazonaws.com. Pra atualizar, troque a
# versão e recalcule os dois hashes dos zips.
FROM debian:bookworm-slim AS awscli
ARG TARGETARCH
ARG AWSCLI_VERSION=2.37.8
ARG AWSCLI_SHA256_AMD64=6a2f98fee0901ff623ff9abb5c80f4d67c40e5c0ac5e2ad7afa2ba756a7d5088
ARG AWSCLI_SHA256_ARM64=00c4bac1cac8624b728d991f821428d5de1e0cb56e3803045b85bff63bedee8b
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip \
 && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) arch=x86_64; sum="$AWSCLI_SHA256_AMD64" ;; \
      arm64) arch=aarch64; sum="$AWSCLI_SHA256_ARM64" ;; \
      *) echo "arquitetura não suportada: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /awscli.zip \
      "https://awscli.amazonaws.com/awscli-exe-linux-${arch}-${AWSCLI_VERSION}.zip"; \
    echo "${sum}  /awscli.zip" | sha256sum -c -; \
    unzip -q /awscli.zip -d /unzipped; \
    mkdir -p /out/bin; \
    cp -a /unzipped/aws/dist /out/aws-cli; \
    ln -s ../aws-cli/aws /out/bin/aws

FROM python:3.12-slim-bookworm
COPY --from=fetch /aws-sso /usr/local/bin/aws-sso
COPY --from=awscli /out /opt/krewhub-tools
COPY supervisor.py /usr/local/bin/krewhub-aws-sso-supervisor
RUN chmod 0755 /usr/local/bin/krewhub-aws-sso-supervisor
USER 1000:1000
ENTRYPOINT ["python3", "/usr/local/bin/krewhub-aws-sso-supervisor"]
