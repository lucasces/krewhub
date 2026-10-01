# Sidecar do KrewHub para a extensão AWS SSO: aws-sso-cli + supervisor.
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

FROM python:3.12-slim-bookworm
COPY --from=fetch /aws-sso /usr/local/bin/aws-sso
COPY supervisor.py /usr/local/bin/krewhub-aws-sso-supervisor
RUN chmod 0755 /usr/local/bin/krewhub-aws-sso-supervisor
USER 1000:1000
ENTRYPOINT ["python3", "/usr/local/bin/krewhub-aws-sso-supervisor"]
