#!/usr/bin/env python3
"""Smoke-test do KrewHub central contra um cluster Kubernetes EFÊMERO
(descartável) -- diferente das outras duas camadas de teste já existentes:

  1. `test.sh` (pytest, offline, ~1.4s) -- tudo mockado, nenhum cluster.
  2. Smoke-test MANUAL contra o cluster REAL (`galaxy-far-far-away`) --
     owner descartável, documentado nas seções "testado ao vivo" do
     README principal.

Esta camada prova o mesmo fluxo ponta a ponta (provision -> rota no CHP
-> acesso ao dashboard -> close -> logout -> cleanup) SEM tocar no
cluster real e SEM depender da imagem pesada/licenciada do kirocrew
real -- usa `smoke/fake_kirocrew/` no lugar dela (ver módulo).

Engine de cluster efêmero é PLUGÁVEL (ver `smoke/engines/`) -- selecionado
via `KREWHUB_SMOKE_K8S_ENGINE`, SEM default silencioso: rodar sem essa env
var setada lista as opções conhecidas (com `is_available()` de cada uma)
e sai, pedindo pra escolher. Isso é deliberado -- ver `engines/base.py`.

Uso:
    KREWHUB_SMOKE_K8S_ENGINE=podman-machine python3 smoke/run_smoke.py
    python3 smoke/run_smoke.py --list-engines
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # app/
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # engines/

from engines import ENGINES, EngineError  # noqa: E402

MANIFESTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manifests.yaml")
FAKE_KIROCREW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_kirocrew")

OWNER_ID = "smoke-test@krewhub.local.test"
KREWHUB_PORT = 9200  # processo local do krewhub-central (não confundir com a porta pública via CHP)
CHP_LOCAL_PORT = 18080  # kubectl port-forward local pro Service do CHP
SESSION_SECRET = "smoke-test-only-session-secret"


class SmokeFailure(RuntimeError):
    pass


def _print_engine_menu() -> None:
    print("KREWHUB_SMOKE_K8S_ENGINE não setado -- escolha um explicitamente. "
          "Opções conhecidas:\n")
    for name, cls in ENGINES.items():
        availability = cls().is_available()
        mark = "OK" if availability.ok else "indisponível"
        print(f"  {name:16s} [{mark}] {availability.reason}")
    print("\nExemplo: KREWHUB_SMOKE_K8S_ENGINE=podman-machine python3 smoke/run_smoke.py")


def _kubectl(handle, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    cmd = ["kubectl", "--kubeconfig", handle.kubeconfig_path, "--context", handle.context, *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise SmokeFailure(f"kubectl falhou ({' '.join(args)}): {result.stderr}")
    return result


def _wait_http(url: str, *, timeout_s: int = 60, headers: dict | None = None) -> None:
    deadline = time.time() + timeout_s
    last_err = None
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status < 500:
                    return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        time.sleep(2)
    raise SmokeFailure(f"timeout esperando {url} responder: {last_err}")


def _http(method: str, url: str, *, headers: dict | None = None, timeout: int = 30) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main() -> int:
    if "--list-engines" in sys.argv:
        _print_engine_menu()
        return 0

    engine_name = os.environ.get("KREWHUB_SMOKE_K8S_ENGINE")
    if not engine_name:
        _print_engine_menu()
        return 2
    if engine_name not in ENGINES:
        print(f"engine desconhecido: {engine_name!r} -- opções: {list(ENGINES)}")
        return 2

    engine = ENGINES[engine_name]()
    availability = engine.is_available()
    print(f"[engine={engine_name}] is_available() -> ok={availability.ok} reason={availability.reason}")
    if not availability.ok:
        print("engine indisponível -- abortando sem tentar up()")
        return 1

    krewhub_proc: subprocess.Popen | None = None
    pf_proc: subprocess.Popen | None = None
    handle = None
    steps_ok: list[str] = []
    try:
        print(f"[1/8] engine.up() ({engine_name}) ...")
        handle = engine.up()
        print(f"      kubeconfig={handle.kubeconfig_path} context={handle.context}")
        steps_ok.append("engine_up")

        print("[2/8] aplicando manifests.yaml (namespaces + CHP) ...")
        _kubectl(handle, "apply", "-f", MANIFESTS_PATH)
        _kubectl(handle, "-n", "kirohub", "wait", "--for=condition=available",
                 "deployment/configurable-http-proxy", "--timeout=120s")
        steps_ok.append("chp_ready")

        print("[3/8] load_image(fake-kirocrew) ...")
        image_ref = engine.load_image(FAKE_KIROCREW_DIR, "fake-kirocrew:smoke")
        print(f"      imagem disponível no cluster efêmero como {image_ref}")
        steps_ok.append("fake_image_loaded")

        print("[4/8] subindo krewhub-central local, apontado pro cluster efêmero ...")
        env = os.environ.copy()
        env.update(
            KREWHUB_KUBECONFIG=handle.kubeconfig_path,
            KREWHUB_K8S_CONTEXT=handle.context,
            KREWHUB_DEV_NAMESPACE="krewhub-devs",
            KREWHUB_BASE_DOMAIN="smoke.internal",
            KREWHUB_PUBLIC_PORT=str(CHP_LOCAL_PORT),
            KREWHUB_KIROCREW_IMAGE=image_ref,
            KREWHUB_STORAGE_CLASS="local-path",
            KREWHUB_STORAGE_SIZE="256Mi",
            KREWHUB_CHP_NAMESPACE="kirohub",
            KREWHUB_CHP_ADMIN_PORT="8001",
            KREWHUB_DB_PATH="/tmp/krewhub-smoke.db",
            KREWHUB_SESSION_TTL="1h",
            KREWHUB_SESSION_SECRET=SESSION_SECRET,
            KREWHUB_AUTH_TOKEN_TTL_SECONDS="3600",
            KREWHUB_SELF_HOST="",
        )
        if os.path.isfile(env["KREWHUB_DB_PATH"]):
            os.remove(env["KREWHUB_DB_PATH"])
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        python_bin = os.path.join(repo_root, ".venv", "bin", "python")
        if not os.path.isfile(python_bin):
            raise SmokeFailure(f"{python_bin} não existe -- rode ./test.sh uma vez pra bootstrapar o .venv")
        krewhub_proc = subprocess.Popen(
            [python_bin, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(KREWHUB_PORT)],
            cwd=repo_root,
            env=env,
            stdout=open("/tmp/krewhub-smoke-central.log", "w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _wait_http(f"http://127.0.0.1:{KREWHUB_PORT}/healthz", timeout_s=30)
        steps_ok.append("krewhub_central_up")

        print("[5/8] provision (reconcile + CHP route + token de sessão) ...")
        # app/auth.py so usa stdlib (hmac/hashlib/json/base64/time) -- da
        # pra importar com QUALQUER python3 rodando este script (nao
        # precisa ser o .venv usado pro processo do krewhub-central
        # abaixo, que ESSE sim precisa de fastapi/kubernetes).
        from app import auth

        bearer = auth.sign_session(OWNER_ID, secret=SESSION_SECRET, ttl_seconds=3600)
        status, body = _http(
            "POST",
            f"http://127.0.0.1:{KREWHUB_PORT}/devs/{urllib.parse.quote(OWNER_ID, safe='')}/provision?wait=true",
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=180,
        )
        if status != 200:
            raise SmokeFailure(f"/provision -> {status}: {body!r}")
        provision_result = json.loads(body)
        print(f"      provision ok: steps={provision_result.get('steps')} route={provision_result.get('route')}")
        dashboard_url = provision_result.get("dashboard_url_with_token")
        if not dashboard_url:
            raise SmokeFailure("provision não retornou dashboard_url_with_token")
        steps_ok.append("provision")

        print("[6/8] port-forward pro Service do CHP + acesso real ao dashboard fake ...")
        pf_proc = subprocess.Popen(
            [
                "kubectl",
                "--kubeconfig",
                handle.kubeconfig_path,
                "--context",
                handle.context,
                "-n",
                "kirohub",
                "port-forward",
                "svc/configurable-http-proxy",
                f"{CHP_LOCAL_PORT}:8000",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(3)
        host_header = dashboard_url.split("//", 1)[1].split("/", 1)[0]
        token = dashboard_url.split("?token=", 1)[1]
        status, body = _http(
            "GET",
            f"http://127.0.0.1:{CHP_LOCAL_PORT}/?token={token}",
            headers={"Host": host_header},
        )
        if status != 200 or b"fake-kirocrew dashboard" not in body:
            raise SmokeFailure(f"acesso ao dashboard fake via CHP falhou: status={status} body={body[:300]!r}")
        print("      200 OK através do CHP, HTML do fake-kirocrew confirmado")
        steps_ok.append("dashboard_via_chp")

        print("[7/8] close (revoga sessão do kirocrew) + logout (limpa cookie do KrewHub) ...")
        status, body = _http(
            "GET",
            f"http://127.0.0.1:{KREWHUB_PORT}/close",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        if status != 200:
            raise SmokeFailure(f"/close -> {status}: {body!r}")
        steps_ok.append("close")

        status, body = _http(
            "GET",
            f"http://127.0.0.1:{KREWHUB_PORT}/logout",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        # /logout responde 302 -- urllib segue o redirect por padrão; um
        # 200 final (na tela de /login, ou 501 se OIDC não configurado
        # aqui, o que é esperado) confirma que o endpoint não quebrou.
        if status not in (200, 501):
            raise SmokeFailure(f"/logout -> {status} inesperado: {body!r}")
        steps_ok.append("logout")

        print("\n✅ SMOKE-TEST PASSOU -- todos os passos:", steps_ok)
        return 0

    except (SmokeFailure, EngineError) as exc:
        print(f"\n❌ SMOKE-TEST FALHOU no passo após {steps_ok}: {exc}")
        return 1
    finally:
        print("[8/8] cleanup ...")
        if pf_proc is not None:
            pf_proc.terminate()
        if krewhub_proc is not None:
            krewhub_proc.terminate()
            try:
                krewhub_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                krewhub_proc.kill()
        if handle is not None:
            try:
                _kubectl(handle, "delete", "-f", MANIFESTS_PATH, "--ignore-not-found", "--wait=false")
            except SmokeFailure as exc:
                print(f"      aviso: cleanup dos manifests falhou (engine.down() ainda roda): {exc}")
        try:
            engine.down()
        except EngineError as exc:
            print(f"      aviso: engine.down() falhou: {exc}")


if __name__ == "__main__":
    sys.exit(main())
