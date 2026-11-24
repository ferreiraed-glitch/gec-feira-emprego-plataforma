#!/usr/bin/env python3
"""Cria no Formbricks o formulário DEMO de inscrição das empresas (proposta de evolução).

Não existiu em 2026 (ver docs/index.html, «Evolução proposta»). Este formulário mostra
que dados pedir às empresas para gerar depois o formulário de candidaturas, a agenda e
os perfis dos dashboards. É uma demonstração: não liga a nada e não deve receber dados reais.

    FORMBRICKS_GEC_URL=... FORMBRICKS_GEC_API_KEY=... FORMBRICKS_GEC_WORKSPACE_ID=... \
        python3 criar_formulario_empresas_demo.py [--validar]

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


def t(texto):
    return {L: texto}


def cuid():
    return "b" + secrets.token_hex(11)


def texto(id_, titulo, tipo="text", obrigatorio=True, ajuda=None, longo=False):
    e = {"id": id_, "type": "openText", "headline": t(titulo), "required": obrigatorio,
         "charLimit": {"enabled": False}, "inputType": tipo, "insightsEnabled": False}
    if ajuda:
        e["subheader"] = t(ajuda)
    if longo:
        e["longAnswer"] = True
    return e


def escolha(id_, titulo, opcoes, multi=False, obrigatorio=True, ajuda=None):
    e = {"id": id_, "type": "multipleChoiceMulti" if multi else "multipleChoiceSingle",
         "headline": t(titulo), "required": obrigatorio,
         "choices": [{"id": f"{id_}_{i}", "label": t(o)} for i, o in enumerate(opcoes)]}
    if ajuda:
        e["subheader"] = t(ajuda)
    return e


def consentimento(id_, titulo, rotulo):
    return {"id": id_, "type": "consent", "headline": t(titulo), "label": t(rotulo), "required": True}


def separador(id_, titulo, texto_):
    return bloco("Transição", [{"id": id_, "type": "cta", "headline": t(titulo), "subheader": t(texto_),
            "required": False, "buttonExternal": False}])


def bloco(nome, elementos):
    return {"id": cuid(), "name": nome, "elements": elementos}


def oferta(n, obrigatoria):
    p = f"of{n}_"
    o = obrigatoria
    return bloco(f"Oferta {n}", [
        texto(p + "titulo", f"Oferta {n}: designação da função", obrigatorio=o),
        texto(p + "vagas", "Número de vagas", "number", o),
        texto(p + "local", "Local de trabalho", obrigatorio=o),
        escolha(p + "contrato", "Tipo de contrato", ["Sem termo", "Termo certo", "Termo incerto",
                "Trabalho temporário", "Estágio", "Outro"], obrigatorio=o),
        texto(p + "horario", "Regime e horário (ex.: part-time, turnos)", obrigatorio=False),
        texto(p + "salario", "Remuneração (valor, intervalo ou «a combinar»)", obrigatorio=False),
        texto(p + "inicio", "Data de início prevista", obrigatorio=False),
        texto(p + "req_elim", "Requisitos eliminatórios", obrigatorio=o, longo=True,
              ajuda="Só o que impede mesmo a candidatura: habilitações mínimas, carta de condução, documentos."),
        texto(p + "req_desej", "Requisitos desejáveis", obrigatorio=False, longo=True),
        escolha(p + "linguas", "Línguas necessárias", ["Português", "Inglês", "Francês", "Espanhol", "Outra"],
                multi=True, obrigatorio=False),
        escolha(p + "sem_exp", "Aceita pessoas sem experiência?", ["Sim", "Não"], obrigatorio=o),
        escolha(p + "deficiencia", "Aberta a pessoas com deficiência ou incapacidade?",
                ["Sim", "Sim, com adaptações", "Não"], obrigatorio=o),
        texto(p + "triagem", "Perguntas de triagem para o técnico fazer ao candidato (até 3)",
              obrigatorio=False, longo=True),
    ])


BLOCOS = [
    bloco("Empresa", [
        texto("emp_nome", "Nome da empresa"),
        texto("emp_setor", "Setor de atividade"),
        texto("emp_site", "Página na internet", "url", False),
        texto("emp_contacto", "Pessoa de contacto (RH)"),
        texto("emp_email", "E-mail", "email"),
        texto("emp_telefone", "Telefone", "phone"),
    ]),
    separador("sep_presenca", "Presença na Feira",
              "Estes dados servem para construir a agenda de entrevistas e calcular quantos candidatos podem ser aceites."),
    bloco("Presença na Feira", [
        texto("pres_rh", "Número de pessoas de RH presentes na Feira", "number"),
        texto("pres_inicio", "Início da disponibilidade (HH:MM)"),
        texto("pres_fim", "Fim da disponibilidade (HH:MM)"),
        escolha("pres_duracao", "Duração média de cada entrevista",
                ["10 minutos", "15 minutos", "20 minutos", "30 minutos", "Mais de 30 minutos"]),
        texto("pres_limite", "Limite de entrevistas no período", "number",
              ajuda="Se não souber, deixe o número que resulta de RH × período ÷ duração; a coordenação confirma."),
        escolha("pres_tolerancia", "Aceita entrevistar fora do período indicado, por pouco tempo?", ["Sim", "Não"]),
        texto("pres_pausas", "Pausas ou horários em que não pode entrevistar", obrigatorio=False, longo=True),
    ]),
    separador("sep_ofertas", "Ofertas de emprego",
              "Pode inscrever até 3 ofertas. A primeira é obrigatória."),
    oferta(1, True),
    oferta(2, False),
    oferta(3, False),
    bloco("Logística e responsável", [
        escolha("log_necessidades", "O que precisa no espaço da Feira",
                ["Mesa e cadeiras", "Ligação à internet", "Tomadas", "Cartaz ou placa", "Acesso para pessoas com mobilidade reduzida"],
                multi=True, obrigatorio=False),
        texto("log_seguimento", "Contacto para o seguimento dos candidatos depois da Feira", obrigatorio=False),
    ]),
    bloco("Autorizações", [
        consentimento("aut_publicar", "Publicação",
                      "Autorizo a publicação do nome, do logótipo e das ofertas da empresa no catálogo e no formulário de candidaturas."),
        consentimento("aut_rgpd", "Dados dos candidatos",
                      "Compreendo que só recebo os dados de candidatos validados para a minha empresa, "
                      "que os trato apenas para recrutamento e que os elimino no prazo comunicado pela organização."),
        consentimento("aut_desistencia", "Desistência",
                      "Comprometo-me a avisar a organização com pelo menos 7 dias de antecedência se desistir da Feira."),
    ]),
]

SURVEY = {
    "workspaceId": WORKSPACE,
    "name": "DEMO — Inscrição de empresas (proposta de evolução)",
    "type": "link",
    "status": "inProgress",
    "defaultLanguage": L,
    "languages": [{"code": L, "default": True, "enabled": True}],
    "welcomeCard": {
        "enabled": True,
        "headline": t("DEMONSTRAÇÃO — Inscrição de empresas na Feira de Emprego"),
        "subheader": t("Formulário de demonstração, para teste. Não use dados reais nem de pessoas: "
                       "tudo o que preencher é apagado. Em produção, as respostas gerariam a agenda, "
                       "o formulário de candidaturas e os perfis das empresas."),
        "timeToFinish": True, "showResponseCount": False,
    },
    "blocks": BLOCOS,
    "endings": [{"id": cuid(), "type": "endScreen",
                 "headline": t("Inscrição de demonstração recebida"),
                 "subheader": t("Obrigado por testar. Nenhum dado foi usado para além desta demonstração.")}],
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
