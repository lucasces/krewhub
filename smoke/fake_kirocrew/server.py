#!/usr/bin/env python3
"""Servidor HTTP mínimo (só stdlib) que imita o suficiente do dashboard
real do kirocrew (`ghcr.io/kirodotdev/kirocrew`) pro smoke-test do
KrewHub: probes de health e uma página `/` reconhecível. NÃO reimplementa
autenticação/sandbox/CLI reais -- só o suficiente pra provar que o
reconcile do KrewHub e o roteamento do CHP funcionam de ponta a ponta
sem depender da imagem real (pesada, licenciada, com device-flow que
exige um humano)."""

from __future__ import annotations

import http.server
import os

PORT = int(os.environ.get("KIROCREW_PORT", "5476"))


class Handler(http.server.BaseHTTPRequestHandler):
    def _ok(self, body: bytes, content_type: str = "text/plain") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (nome exigido pela stdlib)
        if self.path in ("/api/health", "/api/ready", "/api/live"):
            self._ok(b'{"ok": true, "fake": true}', "application/json")
            return
        if self.path.startswith("/"):
            owner = os.environ.get("KIROCREW_OWNER_ID", "unknown")
            body = (
                f"<html><body><h1>fake-kirocrew dashboard</h1>"
                f"<p>owner={owner}</p></body></html>"
            ).encode()
            self._ok(body, "text/html")
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:  # silencia access log
        pass


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
