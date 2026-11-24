#!/usr/bin/env python3
"""Validação de formato do NIF e do nº de documento no formulário publicado.

Não impede inscrições repetidas: o Formbricks só compara respostas com
valores fixos, nunca com submissões anteriores. Serve para as repetições
serem reconhecidas depois (exportar_candidaturas.py anula a submissão
posterior), sem que "123 456 789" e "123456789" pareçam NIFs diferentes.

  NIF       exactamente 9 algarismos
  Documento só letras e algarismos, sem espaços, pontos nem hífenes

Como a regra não tem mensagem de erro própria, o formato é explicado no
texto de ajuda do campo e no exemplo (placeholder).

    python3 validar_nif_documento.py --dry-run   # mostra o que mudaria
    python3 validar_nif_documento.py             # aplica e confirma por releitura
"""
import argparse
import json
import sys

from exportar_candidaturas import load_env, read_json, MAPPING_FILE
import r1_capacidade as r1

REGRAS = {
    "cand_nif": ("^[0-9]{9}$", "9 algarismos, sem espaços nem pontos (ex.: 123456789).", "123456789"),
    "cand_num_doc": ("^[0-9A-Za-z]+$",
                     "Só letras e algarismos, sem espaços, pontos ou hífenes (ex.: 123456789ZZ1).",
                     "123456789ZZ1"),
}


def aplicar(blocks):
    mudou = []
    for b in blocks:
        for e in b["elements"]:
            if e["id"] in REGRAS:
                pat, ajuda, exemplo = REGRAS[e["id"]]
                novo = {"validation": {"rules": [{"id": f"{e['id']}_formato", "type": "pattern",
                                                  "params": {"pattern": pat}}], "logic": "and"},
                        "subheader": {"pt-PT": ajuda}, "placeholder": {"pt-PT": exemplo}}
                if any(e.get(k) != v for k, v in novo.items()):
                    e.update(novo)
                    mudou.append(e["id"])
    return mudou


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    env = load_env()
    r1.FORMBRICKS_URL = env.get("FORMBRICKS_GEC_URL", r1.FORMBRICKS_URL)
    key = env["FORMBRICKS_GEC_API_KEY"]
    sid = read_json(MAPPING_FILE)["survey_id"]
    survey = r1.api_request("GET", f"/api/v3/surveys/{sid}", key)["data"]
    mudou = aplicar(survey["blocks"])
    if not mudou:
        print("Já estava aplicado. Nada a fazer.")
        return
    print(f"A alterar: {', '.join(mudou)}")
    if a.dry_run:
        print("--dry-run: nada escrito.")
        return
    r1.api_request("PATCH", f"/api/v3/surveys/{sid}", key, {"blocks": survey["blocks"]})
    depois = r1.api_request("GET", f"/api/v3/surveys/{sid}", key)["data"]
    falta = aplicar(json.loads(json.dumps(depois["blocks"])))
    if falta:
        sys.exit(f"ERRO: a releitura não mostra a alteração em {falta}")
    print(f"Aplicado e confirmado por releitura (estado do formulário: {depois['status']}).")


if __name__ == "__main__":
    main()
