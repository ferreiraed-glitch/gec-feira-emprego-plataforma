#!/usr/bin/env python3
"""Fecha o formulário de candidaturas da FE2026 a uma hora marcada (7 Out 2026).

As inscrições terminam a 9 Out 2026 às 13:00 (decisão do utilizador). A API v3
do Formbricks não expõe a data de fecho (closeOnDate), e no self-hosted o fecho
por data depende de um cron interno; por isso o cron do CT129 corre este script
nessa hora e muda o estado do inquérito para «completed». O link público passa
a mostrar o formulário como encerrado e não aceita mais respostas.

É idempotente: não faz nada antes da hora nem se o formulário já não estiver
«inProgress». Guarda uma cópia do inquérito antes de o alterar, relê o estado
depois e sai com erro se não ficou fechado.

    fechar_formulario.py [--depois "2026-10-09 13:00"] [--simular]
"""
import argparse
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import r1_capacidade as r1

TZ = ZoneInfo("Europe/Lisbon")
DEPOIS = os.environ.get("FE2026_FECHO_FORMULARIO", "2026-10-09 13:00")


def estado(survey_id, chave):
    return r1.api_request("GET", f"/api/v3/surveys/{survey_id}", chave)["data"]


def main():
    p = argparse.ArgumentParser(description="Fecha o formulário FE2026 a partir de uma hora")
    p.add_argument("--depois", default=DEPOIS, help="'AAAA-MM-DD HH:MM', hora de Lisboa")
    p.add_argument("--simular", action="store_true", help="só mostra o que faria")
    a = p.parse_args()

    env = r1.load_env()
    r1.FORMBRICKS_URL = env.get("FORMBRICKS_GEC_URL", r1.FORMBRICKS_URL)
    chave = env["FORMBRICKS_GEC_API_KEY"]
    with open(r1.MAPPING_FILE) as f:
        survey_id = json.load(f)["survey_id"]

    agora = datetime.now(TZ)
    limite = datetime.strptime(a.depois, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    survey = estado(survey_id, chave)
    print(f"{agora:%F %T} formulário {survey_id}: estado {survey['status']!r}; fecho a partir de {a.depois}")
    if survey["status"] != "inProgress":
        print("Nada a fazer: o formulário não está aberto.")
        return 0
    if agora < limite:
        print("Nada a fazer: ainda não chegou a hora.")
        return 0
    if a.simular:
        print("--simular: fecharia agora.")
        return 0

    print(f"Cópia antes de fechar: {r1.backup_survey(survey)}")
    try:
        r1.api_request("PATCH", f"/api/v3/surveys/{survey_id}", chave, {"status": "completed"})
    except RuntimeError as e:
        # A API de gestão v1 aceita o estado se a v3 o recusar.
        print(f"AVISO: v3 recusou ({e}); a tentar a v1", file=sys.stderr)
        r1.api_request("PUT", f"/api/v1/management/surveys/{survey_id}", chave, {"status": "completed"})
    depois = estado(survey_id, chave)["status"]
    if depois != "completed":
        print(f"ERRO: o formulário continua {depois!r}. Fechar à mão no Formbricks (Resumo → Concluir).",
              file=sys.stderr)
        return 1
    print("Formulário fechado (completed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
