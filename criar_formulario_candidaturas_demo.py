#!/usr/bin/env python3
"""Cria no Formbricks o formulário DEMO de candidaturas (a versão da Feira de 2026, com dados fictícios).

Reproduz a estrutura do formulário real: dados da entidade e do técnico, dados do candidato (NIF e
documento validados por expressão regular, CV em PDF), escolha de empresas (máximo 3), ecrãs de
ofertas só para as empresas escolhidas (saltos condicionais) e declarações de protecção de dados.
As empresas e ofertas são fictícias. É uma demonstração: não liga ao R1 nem aos dashboards.

    FORMBRICKS_GEC_URL=... FORMBRICKS_GEC_API_KEY=... FORMBRICKS_GEC_WORKSPACE_ID=... \
        python3 criar_formulario_candidaturas_demo.py [--validar]

Com --validar só pede ao Formbricks que valide a estrutura (POST /api/v3/surveys/validate).
"""
import json
import os
import secrets
import sys
import urllib.error
import urllib.request

URL = os.environ["FORMBRICKS_GEC_URL"].rstrip("/")
CHAVE = os.environ["FORMBRICKS_GEC_API_KEY"]
WORKSPACE = os.environ["FORMBRICKS_GEC_WORKSPACE_ID"]
L = "pt-PT"

# (id, nome, ofertas, várias ofertas possíveis?)
EMPRESAS = [
    ("emp_demo_supermercado", "SUPERMERCADO DEMO",
     ["Caixa (part-time)", "Repositor/a", "Operador/a de talho"], True),
    ("emp_demo_restauracao", "RESTAURAÇÃO DEMO",
     ["Cozinheiro/a", "Empregado/a de mesa", "Empregado/a de balcão"], True),
    ("emp_demo_jardinagem", "JARDINS DEMO",
     ["Auxiliar de jardinagem", "Jardineiro/a"], True),
    ("emp_demo_cuidados", "CUIDADOS DEMO",
     ["Auxiliar de geriatria", "Técnico/a de ação educativa"], False),
    ("emp_demo_seguranca", "SEGURANÇA DEMO",
     ["Vigilante de segurança privada", "Operador/a de central de alarmes"], False),
]


def t(texto):
    return {L: texto}


def negrito(texto):
    return t(f'<p class="fb-editor-paragraph"><b><strong class="fb-editor-text-bold">{texto}</strong></b></p>')


def paragrafo(texto):
    return t(f'<p class="fb-editor-paragraph">{texto}</p>')


def cuid():
    return "b" + secrets.token_hex(11)


def texto(id_, titulo, tipo="text", ajuda=None, exemplo=None, padrao=None, regra=None):
    e = {"id": id_, "type": "openText", "headline": t(titulo), "required": True,
         "charLimit": {"enabled": False}, "inputType": tipo, "insightsEnabled": False}
    if ajuda:
        e["subheader"] = t(ajuda)
    if exemplo:
        e["placeholder"] = t(exemplo)
    if padrao:
        e["validation"] = {"logic": "and", "rules": [{"id": regra, "type": "pattern", "params": {"pattern": padrao}}]}
    return e


def consentimento(id_, titulo, rotulo, ajuda=None):
    e = {"id": id_, "type": "consent", "headline": t(titulo), "label": t(rotulo), "required": True}
    if ajuda:
        e["subheader"] = t(ajuda)
    return e


def bloco(nome, elementos, logica=None, destino_por_omissao=None):
    b = {"id": cuid(), "name": nome, "elements": elementos}
    if logica:
        b["logic"] = logica
    if destino_por_omissao:
        b["logicFallback"] = destino_por_omissao
    return b


def separador(nome, titulo, subtitulo):
    return bloco(f"Transição — {nome}", [{"id": f"sep_{nome.lower()}", "type": "cta", "headline": negrito(titulo),
                 "subheader": paragrafo(subtitulo), "required": False, "buttonExternal": False}])


def salto(escolha_id, destino):
    """Se o ecrã das empresas incluir a escolha, salta para o bloco de destino."""
    return {"id": cuid(), "actions": [{"id": cuid(), "target": destino, "objective": "jumpToBlock"}],
            "conditions": {"id": cuid(), "connector": "and", "conditions": [
                {"id": cuid(), "operator": "includesOneOf",
                 "leftOperand": {"type": "element", "value": "empresas_master"},
                 "rightOperand": {"type": "static", "value": [escolha_id]}}]}}


def construir():
    cabeca = [
        bloco("Entidade", [
            texto("entidade_nome", "Nome da entidade"),
            texto("entidade_tecnico", "Nome do/a técnico/a"),
            texto("entidade_email", "E-mail", "email"),
            texto("entidade_telefone", "Contacto telefónico", "phone"),
        ]),
        separador("Candidato", "Dados do Candidato",
                  "Terminámos os dados da entidade. A partir daqui são os dados da pessoa candidata que o técnico está a inscrever."),
        bloco("Candidato", [
            texto("cand_nome", "Nome Candidato"),
            texto("cand_telefone", "Contacto telefónico", "phone"),
            texto("cand_email", "E-mail", "email"),
            texto("cand_localidade", "Localidade e freguesia"),
            texto("cand_habilitacoes", "Habilitações literárias"),
            texto("cand_nif", "NIF", ajuda="9 algarismos, sem espaços nem pontos (ex.: 123456789).",
                  exemplo="123456789", padrao="^[0-9]{9}$", regra="cand_nif_formato"),
            {"id": "cand_tipo_doc", "type": "multipleChoiceSingle", "required": True,
             "headline": t("Tipo de documento de identificação"),
             "choices": [{"id": "cc", "label": t("Cartão de Cidadão")},
                         {"id": "titulo_residencia", "label": t("Título de residência")},
                         {"id": "passaporte", "label": t("Passaporte")}]},
            texto("cand_num_doc", "Nº do documento de identificação",
                  ajuda="Só letras e algarismos, sem espaços, pontos ou hífenes (ex.: 123456789ZZ1).",
                  exemplo="123456789ZZ1", padrao="^[0-9A-Za-z]+$", regra="cand_num_doc_formato"),
            {"id": "cand_cv", "type": "fileUpload", "required": True, "headline": t("CV em PDF"),
             "subheader": t("O nome do ficheiro deve ser o nome do candidato (ex.: Joao-Manuel-Silva.pdf). "
                            "Na demonstração, use um PDF qualquer, sem dados pessoais."),
             "allowMultipleFiles": False, "allowedFileExtensions": ["pdf"]},
        ]),
        separador("Ofertas", "Ofertas de Emprego",
                  "Terminámos os dados do candidato. Agora escolhem-se a(s) empresa(s) recrutadoras e as ofertas concretas a que se candidata."),
    ]

    sep_rgpd = separador("RGPD", "Protecção de Dados",
                         "Por fim, as declarações obrigatórias de protecção de dados, antes de submeter a candidatura.")
    rgpd = bloco("RGPD", [
        consentimento("rgpd_1", "Protecção de dados — declaração do/a técnico/a",
                      "Declaro que obtive autorização do/a candidato/a que estou a inscrever para registar os seus dados "
                      "pessoais e para os partilhar com entidades empregadoras, para efeitos de candidatura a ofertas de "
                      "emprego e entrevistas, e que essa autorização se encontra documentada e arquivada na minha entidade.",
                      "DEMONSTRAÇÃO. Num formulário real, aqui consta a identificação do responsável pelo tratamento, "
                      "a finalidade, os destinatários e o prazo de conservação dos dados."),
        consentimento("rgpd_2", "Conformidade legal",
                      "Confirmo que o tratamento dos dados deste/a candidato/a pela minha entidade é feito em "
                      "conformidade com o Regulamento (UE) 2016/679 (RGPD) e demais legislação aplicável."),
        consentimento("rgpd_3", "Veracidade dos dados",
                      "Confirmo que os dados introduzidos foram fornecidos pelo/a próprio/a candidato/a e correspondem "
                      "à informação por ele/a prestada."),
    ])

    # Ecrãs de ofertas: um por empresa; cada um salta para a próxima empresa escolhida, ou para o RGPD.
    ofertas = []
    for id_emp, nome, lista, varias in EMPRESAS:
        el = {"id": "ofertas_" + id_emp[4:], "type": "multipleChoiceMulti" if varias else "multipleChoiceSingle",
              "headline": negrito(f"{nome} — escolha a(s) oferta(s)" if varias else f"{nome} — escolha a oferta"),
              "required": False,
              "choices": [{"id": f"ofertas_{id_emp[4:]}_{i}", "label": t(o)} for i, o in enumerate(lista)]}
        ofertas.append(bloco(f"Ofertas — {nome}", [el]))
    for i, b in enumerate(ofertas):
        regras = [salto(EMPRESAS[j][0], ofertas[j]["id"]) for j in range(i + 1, len(EMPRESAS))]
        if regras:  # o último ecrã segue em sequência para o RGPD
            b["logic"] = regras
            b["logicFallback"] = sep_rgpd["id"]

    master = bloco("Ofertas de Emprego — Escolha de empresas", [{
        "id": "empresas_master", "type": "multipleChoiceMulti", "required": True,
        "headline": negrito("Ofertas de Emprego — escolha até 3 empresas"),
        "subheader": paragrafo("Cada candidato pode candidatar-se no máximo a 3 empresas (sem limite de ofertas dentro "
                               "de cada uma). Ao marcar a 3ª empresa, as restantes ficam indisponíveis. "
                               "As ofertas concretas de cada empresa escolhida são seleccionadas nos ecrãs seguintes."),
        "choices": [{"id": id_emp, "label": t(f"{nome} — " + " | ".join(lista))} for id_emp, nome, lista, _ in EMPRESAS],
        "validation": {"logic": "and", "rules": [{"id": "max3empresas", "type": "maxSelections", "params": {"max": 3}}]},
    }], [salto(EMPRESAS[j][0], ofertas[j]["id"]) for j in range(len(EMPRESAS))], sep_rgpd["id"])

    return cabeca + [master] + ofertas + [sep_rgpd, rgpd]


SURVEY = {
    "workspaceId": WORKSPACE,
    "name": "DEMO — Inscrição de Candidatos (Feira de Emprego)",
    "type": "link",
    "status": "inProgress",
    "defaultLanguage": L,
    "languages": [{"code": L, "default": True, "enabled": True}],
    "welcomeCard": {
        "enabled": True,
        "headline": negrito("DEMONSTRAÇÃO — Feira de Emprego: inscrição de candidatos"),
        "subheader": paragrafo(
            "Versão de demonstração do formulário de candidaturas da Feira de Emprego do GEC, com empresas e ofertas "
            "fictícias. Pode preencher à vontade, mas <b>não use dados reais</b>: invente nomes, NIF (9 algarismos) e "
            "documento, e carregue um PDF qualquer. Nada deste formulário liga ao sistema da Feira e as respostas são apagadas."
            "<br><br>No formulário real, o técnico da entidade preenche-o em nome do candidato, e as empresas que "
            "atingem o limite de entrevistas desaparecem automaticamente."),
        "timeToFinish": True, "showResponseCount": False,
    },
    "blocks": construir(),
    "endings": [{"id": cuid(), "type": "endScreen", "headline": negrito("Candidatura de demonstração registada"),
                 "subheader": paragrafo("Obrigado por testar. Nenhum dado foi usado para além desta demonstração.")}],
}


def pedir(metodo, caminho, corpo=None):
    dados = json.dumps(corpo).encode() if corpo is not None else None
    r = urllib.request.Request(URL + caminho, data=dados, method=metodo)
    r.add_header("x-api-key", CHAVE)
    r.add_header("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) gec-feira-emprego")
    if dados:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"{metodo} {caminho} -> {e.code}: {e.read().decode()[:1500]}")


if __name__ == "__main__":
    if "--validar" in sys.argv:
        print(json.dumps(pedir("POST", "/api/v3/surveys/validate", {"operation": "create", "data": SURVEY}), ensure_ascii=False)[:800])
    else:
        d = pedir("POST", "/api/v3/surveys", SURVEY)["data"]
        print(d["id"], f"{URL}/s/{d['id']}")
