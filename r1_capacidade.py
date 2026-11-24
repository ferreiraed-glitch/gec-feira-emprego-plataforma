#!/usr/bin/env python3
"""
R1 — Capacidade por empresa (FE2026, Formbricks).

Conta, directamente pelas respostas terminadas do formulário, quantos
candidatos escolheram cada empresa (campo "empresas_master", identificada pelo
prefixo do rótulo gravado). Quando uma empresa atinge a capacidade
máxima (nº de candidatos +30%, já prevendo faltas), RETIRA a opção dessa
empresa do ecrã "escolha até 3 empresas" do formulário publicado, e com ela as
regras de salto que a referem (a validação do Formbricks recusa lógica que
aponte para uma opção inexistente). O bloco de ofertas da empresa fica no
formulário, só deixa de ser alcançável.

Até 25 Set 2026 só se acrescentava " · INSCRIÇÕES ENCERRADAS" ao rótulo — o
Formbricks não tem opção "desactivada" numa escolha múltipla e o aviso não
impedia novas inscrições (4 empresas chegaram a ultrapassar a capacidade).

O que é retirado fica guardado em ENCERRADAS_FILE; se a contagem voltar a
ficar abaixo da capacidade (ex. capacidade aumentada), a opção e as regras são
repostas. Antes de cada alteração guarda-se uma cópia do formulário em
BACKUP_DIR.

Limite que fica: quem já tinha o formulário aberto antes do fecho ainda vê a
empresa e pode submeter — pequeno excesso resolvido na validação/lista de espera.

Uso:
    python3 r1_capacidade.py            # aplica fechos/reaberturas em falta
    python3 r1_capacidade.py --dry-run  # só mostra o que faria
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
import urllib.error

FORMBRICKS_URL = os.environ.get("FORMBRICKS_GEC_URL", "https://formularios.exemplo.org")
ENV_FILE = os.environ.get("FE2026_ENV_FILE", os.path.expanduser("~/.config/jfc/formbricks-gec-admin.env"))
HERE = os.path.dirname(os.path.abspath(__file__))
MAPPING_FILE = os.path.join(HERE, "formbricks-empresas-mapping.json")
# Desde 1 Out 2026 o R1 corre no CT129, ao lado da BD do dashboard, e guarda
# o estado em /var/lib/gec-fe2026/r1/ (FE2026_R1_DIR); sem a variável fica
# junto do script, como no CT127.
R1_DIR = os.environ.get("FE2026_R1_DIR", HERE)
ENCERRADAS_FILE = os.path.join(R1_DIR, "r1_encerradas.json")
BACKUP_DIR = os.path.join(R1_DIR, "backups-formulario")
CLOSED_SUFFIX = " · INSCRIÇÕES ENCERRADAS"

# Capacidade máxima por empresa = nº de candidatos com +30% (já prevê faltas),
# fornecida pelo utilizador a 17 Set 2026.
CAPACITY = {
    "EL CORTE INGLÉS": 35,
    "MERISTEMA": 23,
    "PRIMEFOOD": 12,
    "SANTA CASA DA MISERICÓRDIA DE LISBOA (SCML)": 23,
    "SCML Babysitting": 31,
    "HORAS DE SONHO": 16,
    "PLATEFORM": 47,
    "STRONG CHARON": 16,
    "INDITEX": 8,  # 11 entre 30 Set e 1 Out 2026; a empresa confirmou depois que o máximo é 8
    "LIDL": 10,  # 12 até 6 Out 2026: menos 2 vagas, a pedido da coordenação
    "STEAM": 16,
    "DOMINO'S PIZZA": 31,
    "HORTO DO CAMPO GRANDE": 47,
    "McDONALD'S": 31,
}

# Empresas que desistiram da Feira: ficam retiradas do formulário seja qual for
# a contagem (a capacidade acima mantém-se, é a que o exportador usa).
# McDONALD'S desistiu a 25 Set 2026.
DESISTENCIAS = {"McDONALD'S"}


def load_env():
    env = {}
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k] = v
    return env


def api_request(method, path, api_key, body=None):
    url = f"{FORMBRICKS_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("x-api-key", api_key)
    req.add_header("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) gec-fe2026-r1-script")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8")
        raise RuntimeError(f"{method} {path} -> {e.code}: {detail}") from e


def fetch_all_responses(api_key, survey_id):
    responses = []
    skip = 0
    limit = 250
    while True:
        page = api_request(
            "GET",
            f"/api/v2/management/responses?surveyId={survey_id}&limit={limit}&skip={skip}",
            api_key,
        )
        batch = page.get("data", [])
        responses.extend(batch)
        if len(batch) < limit:
            break
        skip += limit
    return responses


def company_from_label(label, companies):
    """Empresa a que pertence o rótulo gravado na resposta.

    O Formbricks guarda nas respostas o TEXTO da opção escolhida, não o seu id
    (confirmado a 23 Set 2026 com as respostas reais — contar por id dava sempre
    0). O rótulo tem a forma "EMPRESA — ofertas..." e pode ter ganho depois o
    sufixo de encerramento, por isso identifica-se a empresa pelo prefixo; em
    caso de ambiguidade ganha o nome mais longo.
    """
    matches = [c for c in companies if label == c or label.startswith(c + " —")]
    return max(matches, key=len) if matches else None


def norm_nif(v):
    return re.sub(r"\D", "", v or "")


def norm_doc(v):
    return re.sub(r"[^0-9A-Za-z]", "", v or "").upper()


# ── Inscrições repetidas (R3, revisto a 1 Out 2026) ──────────────────────
#
# Um candidato (mesmo NIF ou mesmo nº de documento) pode ser inscrito mais de
# uma vez, desde que no total não passe de MAX_EMPRESAS_POR_CANDIDATO empresas
# e não repita uma empresa. Só a candidatura a mais é anulada, não a inscrição
# inteira. NIFs e documentos obviamente fictícios (000000000, 123456789…),
# usados pelos técnicos para não dar o dado, não identificam ninguém.
#
# O estado da BD do dashboard manda sobre a regra: uma candidatura anulada à
# mão liberta o lugar NA EMPRESA, mas continua a ser uma das 3 do candidato
# (o limite é uma regra de princípio, decisão do utilizador a 1 Out 2026; a
# única excepção são as empresas em DESISTENCIAS, que não contam para as 3); e
# uma que a coordenação ou um técnico já tratou (validada, reposta) ocupa
# sempre lugar.

MAX_EMPRESAS_POR_CANDIDATO = 3
ANULADO_DUPLICADO = "Automático (duplicado)"
ANULADA_A_MAO = "Anulada no dashboard"
_SEQ = "0123456789" * 2


def _sequencia(digitos):
    return len(digitos) >= 5 and (digitos in _SEQ or digitos in _SEQ[::-1])


def nif_ficticio(n):
    """NIF normalizado que não identifica ninguém: vazio, sem 9 algarismos,
    com dígito de controlo errado, só um algarismo repetido ou sequência."""
    if not re.fullmatch(r"\d{9}", n or ""):
        return True
    if len(set(n)) <= 2 or _sequencia(n):
        return True
    c = 11 - sum(int(d) * (9 - i) for i, d in enumerate(n[:8])) % 11
    return int(n[8]) != (0 if c >= 10 else c)


def doc_ficticio(d):
    """Documento normalizado que não identifica ninguém: curto, só um ou dois
    caracteres repetidos (000, XXXXXXXXXX), ou a começar por uma sequência
    (123456789ZZ1 é o exemplo do formulário)."""
    if len(d or "") < 6 or len(set(d)) <= 2:
        return True
    inicio = re.match(r"\d*", d).group()
    return _sequencia(inicio)


def alterada_a_mao(por):
    return bool(por) and not por.startswith("Automático")


def classificar(inscricoes, estados=None):
    """Decide que candidaturas ocupam lugar.

    inscricoes: [{"id", "criada_em", "nif", "doc", "empresas": [...]}], só de
    respostas terminadas, com NIF e documento já normalizados.
    estados: {(id, empresa): (validacao, validacao_alterada_por)} da BD, se
    houver. Devolve {(id, empresa): motivo}; motivo None = ocupa lugar na
    empresa.
    """
    estados = estados or {}
    pai = {i["id"]: i["id"] for i in inscricoes}

    def raiz(x):
        while pai[x] != x:
            pai[x] = pai[pai[x]]
            x = pai[x]
        return x

    primeiro = {}
    for i in inscricoes:
        for chave in (("nif", i["nif"]) if not nif_ficticio(i["nif"]) else None,
                      ("doc", i["doc"]) if not doc_ficticio(i["doc"]) else None):
            if chave is None:
                continue
            if chave in primeiro:
                pai[raiz(i["id"])] = raiz(primeiro[chave])
            else:
                primeiro[chave] = i["id"]

    grupos = {}
    for i in inscricoes:
        grupos.setdefault(raiz(i["id"]), []).append(i)

    res = {}
    for grupo in grupos.values():
        grupo.sort(key=lambda i: (i["criada_em"] or "", i["id"]))
        escolhidas = {}                                # empresa -> inscrição (as 3 do candidato)
        for i in grupo:                                # por ordem de chegada
            for emp in i["empresas"]:
                validacao, por = estados.get((i["id"], emp), ("Por validar", None))
                if emp in DESISTENCIAS:
                    # Única excepção ao limite: a empresa desistiu da Feira, a
                    # candidatura não conta para as 3 do candidato.
                    res[(i["id"], emp)] = ANULADA_A_MAO if validacao == "Anulado" else None
                    continue
                cabe =emp not in escolhidas and len(escolhidas) < MAX_EMPRESAS_POR_CANDIDATO
                if alterada_a_mao(por):
                    # Já tratada à mão: a regra não decide. Anulada não ocupa
                    # lugar na empresa, mas conta para as 3 se lá couber.
                    res[(i["id"], emp)] = ANULADA_A_MAO if validacao == "Anulado" else None
                    if cabe or (validacao != "Anulado" and emp not in escolhidas):
                        escolhidas[emp] = i["id"]
                elif emp in escolhidas:
                    res[(i["id"], emp)] = f"repete {emp}, já pedida na inscrição {escolhidas[emp]}"
                elif not cabe:
                    res[(i["id"], emp)] = (f"o candidato já tem {MAX_EMPRESAS_POR_CANDIDATO} empresas "
                                           f"({', '.join(sorted(escolhidas))})")
                else:
                    res[(i["id"], emp)] = None
                    escolhidas[emp] = i["id"]
    return res


def inscricoes_das_respostas(responses, master_element_id, companies):
    """Respostas terminadas → inscrições para classificar(). Uma resposta
    parcial (formulário abandonado a meio) não é uma inscrição."""
    inscricoes, unknown = [], []
    for r in responses:
        if not r.get("finished"):
            continue
        data = r.get("data") or {}
        selected = data.get(master_element_id) or []
        empresas = []
        for label in selected if isinstance(selected, list) else []:
            company = company_from_label(label, companies)
            if company is None:
                unknown.append(label)
            elif company not in empresas:
                empresas.append(company)
        inscricoes.append({"id": r.get("id"), "criada_em": r.get("createdAt"),
                           "nif": norm_nif(data.get("cand_nif")), "doc": norm_doc(data.get("cand_num_doc")),
                           "empresas": empresas})
    if unknown:
        print(f"AVISO: {len(unknown)} rótulo(s) sem empresa reconhecida: {sorted(set(unknown))}", file=sys.stderr)
    return inscricoes


def count_selections(responses, master_element_id, companies, estados=None):
    """Nº de candidaturas por empresa que ocupam lugar (ver classificar())."""
    inscricoes = inscricoes_das_respostas(responses, master_element_id, companies)
    counts = {}
    for (_, company), motivo in classificar(inscricoes, estados).items():
        if motivo is None:
            counts[company] = counts.get(company, 0) + 1
    return counts


def ler_estados(db_path):
    """Estado das candidaturas na BD do dashboard (só leitura)."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        return {(r[0], r[1]): (r[2], r[3]) for r in con.execute(
            "SELECT id_submissao, empresa, validacao, validacao_alterada_por FROM candidaturas WHERE removida = 0")}
    finally:
        con.close()


def find_master_element(blocks, master_block_id, master_element_id):
    block = next((b for b in blocks if b["id"] == master_block_id), None)
    if block is None:
        return None
    return next((e for e in block["elements"] if e["id"] == master_element_id), None)


def rule_references(rule, choice_id):
    for cond in rule.get("conditions", {}).get("conditions", []):
        value = (cond.get("rightOperand") or {}).get("value")
        if value == choice_id or (isinstance(value, list) and choice_id in value):
            return True
    return False


def close_choice(blocks, master_element, choice_id):
    """Retira a opção do mestre e todas as regras de salto que a referem.
    Devolve o registo necessário para a repor (mexe em `blocks` no sítio)."""
    choices = master_element["choices"]
    index = next(i for i, c in enumerate(choices) if c["id"] == choice_id)
    choice = choices.pop(index)
    for lang, text in choice["label"].items():
        if text.endswith(CLOSED_SUFFIX):  # marca do método anterior
            choice["label"][lang] = text[: -len(CLOSED_SUFFIX)]
    removed = []
    for block in blocks:
        logic = block.get("logic") or []
        keep = [r for r in logic if not rule_references(r, choice_id)]
        removed += [{"block_id": block["id"], "rule": r} for r in logic if rule_references(r, choice_id)]
        if len(keep) != len(logic):
            block["logic"] = keep
    return {"choice": choice, "index": index, "rules": removed}


def reopen_choice(blocks, master_element, record, choice_order):
    """Inverso de close_choice. As opções voltam à ordem de `choice_order` (a do
    mapeamento, igual à do mestre); as regras de cada bloco ficam ordenadas pela
    posição do bloco de destino (a primeira regra verdadeira ganha, e o desenho
    é saltar para a PRÓXIMA empresa escolhida)."""
    choices = master_element["choices"]
    choices.insert(min(record["index"], len(choices)), record["choice"])
    rank = {cid: i for i, cid in enumerate(choice_order)}
    choices.sort(key=lambda c: rank.get(c["id"], len(rank)))
    position = {b["id"]: i for i, b in enumerate(blocks)}
    by_id = {b["id"]: b for b in blocks}
    touched = set()
    for item in record["rules"]:
        block = by_id.get(item["block_id"])
        if block is None:
            continue
        block.setdefault("logic", []).append(item["rule"])
        touched.add(block["id"])
    for block_id in touched:
        by_id[block_id]["logic"].sort(
            key=lambda r: min((position.get(a.get("target"), len(blocks)) for a in r["actions"]), default=len(blocks))
        )


def load_encerradas():
    try:
        with open(ENCERRADAS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_encerradas(encerradas):
    tmp = ENCERRADAS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(encerradas, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ENCERRADAS_FILE)


def backup_survey(survey):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"formulario-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(survey, f, ensure_ascii=False)
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--survey-id", help="outro formulário com a mesma estrutura (só para testes)")
    args = parser.parse_args()

    global FORMBRICKS_URL
    env = load_env()
    api_key = env["FORMBRICKS_GEC_API_KEY"]
    if env.get("FORMBRICKS_GEC_URL"):
        FORMBRICKS_URL = env["FORMBRICKS_GEC_URL"]
    mapping = json.load(open(MAPPING_FILE, encoding="utf-8"))
    survey_id = args.survey_id or mapping["survey_id"]
    master_block_id = mapping["master_block_id"]
    master_element_id = mapping["master_element_id"]
    company_choice_ids = mapping["company_choice_ids"]
    choice_id_to_company = {v: k for k, v in company_choice_ids.items()}

    missing_capacity = [c for c in company_choice_ids if c not in CAPACITY]
    if missing_capacity:
        print(f"AVISO: sem capacidade definida para: {missing_capacity}", file=sys.stderr)

    print("A obter respostas...")
    responses = fetch_all_responses(api_key, survey_id)
    print(f"{len(responses)} respostas encontradas.")

    # Com a BD do dashboard (CT129), as anuladas à mão libertam o lugar. Se a
    # BD estiver configurada mas não abrir, pára: contar sem ela fecharia
    # empresas que têm vagas.
    db_path = os.environ.get("FE2026_DB")
    estados = None
    if db_path:
        estados = ler_estados(db_path)
        print(f"{len(estados)} candidaturas lidas da BD do dashboard.")
    else:
        print("AVISO: sem FE2026_DB — as anulações feitas no dashboard não libertam lugar nesta contagem.",
              file=sys.stderr)
    counts = count_selections(responses, master_element_id, list(company_choice_ids), estados)

    print("\nContagem por empresa (respostas terminadas que ocupam lugar):")
    full = set()
    for company in company_choice_ids:
        n = counts.get(company, 0)
        cap = CAPACITY.get(company)
        status = ""
        if company in DESISTENCIAS:
            status = "  >>> DESISTIU"
            full.add(company)
        elif cap is not None:
            if n >= cap:
                status = "  >>> ATINGIU A CAPACIDADE"
                full.add(company)
            else:
                status = f"  ({cap - n} vagas)"
        print(f"  {company}: {n}/{cap if cap is not None else '?'}{status}")

    survey = api_request("GET", f"/api/v3/surveys/{survey_id}", api_key)["data"]
    blocks = survey["blocks"]
    master_element = find_master_element(blocks, master_block_id, master_element_id)
    if master_element is None:
        print("ERRO: pergunta mestre não encontrada no formulário actual.", file=sys.stderr)
        sys.exit(1)

    encerradas = load_encerradas()
    present = {choice_id_to_company.get(c["id"]) for c in master_element["choices"]}
    for company in list(encerradas):
        if company in present:
            # Reposta à mão no editor: o registo deixou de fazer sentido.
            print(f"AVISO: '{company}' consta como encerrada mas está no formulário — registo descartado.", file=sys.stderr)
            del encerradas[company]

    changes = []
    for company in company_choice_ids:
        if company in full and company not in encerradas:
            if company not in present:
                print(f"AVISO: '{company}' esgotada mas sem opção no formulário nem registo de fecho.", file=sys.stderr)
                continue
            encerradas[company] = close_choice(blocks, master_element, company_choice_ids[company])
            changes.append(f"FECHAR '{company}' (retirada do ecrã das empresas, "
                           f"{len(encerradas[company]['rules'])} regras de salto)")
        elif company not in full and company in encerradas:
            reopen_choice(blocks, master_element, encerradas.pop(company), list(company_choice_ids.values()))
            changes.append(f"REABRIR '{company}' (abaixo da capacidade)")

    if not changes:
        print("\nNada a alterar no formulário.")
        if not args.dry_run:
            save_encerradas(encerradas)
        return

    print("\nAlterações:")
    for c in changes:
        print(f"  {c}")

    if args.dry_run:
        print("\n--dry-run: não escrevi nada no formulário.")
        return

    backup = backup_survey(survey)
    print(f"\nCópia do formulário antes da alteração: {backup}")
    print("A publicar alteração...")
    api_request(
        "PATCH",
        f"/api/v3/surveys/{survey_id}",
        api_key,
        {"blocks": blocks},
    )
    save_encerradas(encerradas)
    print("Feito.")


if __name__ == "__main__":
    main()
