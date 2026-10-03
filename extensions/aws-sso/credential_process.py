#!/usr/bin/env python3
"""`credential_process` dos perfis gerenciados pela extensão aws-sso.

Uso (escrito no arquivo de perfis pelo supervisor):

    krewhub-aws-sso-creds <perfil> [--default]

Busca as credenciais do papel no `aws-sso ecs server` local (slot nomeado, ou
o endpoint padrão com `--default`) e as imprime no formato `Version: 1` que o
AWS CLI e os SDKs esperam. Só stdlib: roda com o python3 do `kirocrew`. Erros
vão pra stderr, em inglês, pro agente repassar ao usuário."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import NoReturn

ENDPOINT = os.environ.get("KREWHUB_AWS_SSO_ENDPOINT", "http://127.0.0.1:4144")
TIMEOUT = 10


def fail(msg: str) -> NoReturn:
    print(f"krewhub-aws-sso-creds: {msg}", file=sys.stderr)
    sys.exit(1)


def fetch(profile: str, default: bool) -> dict:
    path = "/" if default else "/slot/" + urllib.parse.quote(profile, safe="")
    req = urllib.request.Request(ENDPOINT + path)
    token = os.environ.get("KREWHUB_AWS_SSO_TOKEN", "")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            fail(
                f"credentials for '{profile}' are not loaded. Ask the user to open the "
                "AWS SSO card in KrewHub (sign in, select the role, or reload the credentials)."
            )
        fail(f"credentials endpoint answered HTTP {exc.code} for '{profile}'.")
    except (urllib.error.URLError, OSError, ValueError):
        fail("credentials endpoint is unreachable. Ask the user to check the AWS SSO card in KrewHub.")


def main(argv: list[str]) -> None:
    args = [a for a in argv if a != "--default"]
    if len(args) != 1:
        fail("usage: krewhub-aws-sso-creds <profile> [--default]")
    data = fetch(args[0], "--default" in argv)
    try:
        out = {
            "Version": 1,
            "AccessKeyId": data["AccessKeyId"],
            "SecretAccessKey": data["SecretAccessKey"],
            "SessionToken": data["Token"],
            "Expiration": data["Expiration"],
        }
    except (KeyError, TypeError):
        fail("credentials endpoint returned an unexpected response.")
    print(json.dumps(out))


if __name__ == "__main__":
    main(sys.argv[1:])
