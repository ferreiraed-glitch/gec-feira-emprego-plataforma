"""Horários de entrevista e regras de marcação (§5.2 da especificação).

- A grelha de cada empresa são horários contíguos de `duracao_entrevista_min`,
  de `hora_inicio` até `hora_fim`.
- Cada horário aceita até `n_entrevistadores` candidatos dessa empresa.
- O mesmo candidato (mesmo NIF ou nº de documento, ver `duplicado_de`) não
  pode ter duas entrevistas cujos intervalos [início, início + duração) se
  sobreponham, em nenhuma empresa.
- Sem sobremarcação: quando a grelha enche, os validados seguintes ficam sem
  horário, em lista de espera por ordem de validação (decisão de 23 Set 2026).

As funções que decidem uma marcação devem ser chamadas dentro da mesma
transacção `BEGIN IMMEDIATE` que a grava, para dois técnicos não marcarem o
mesmo lugar ao mesmo tempo.
"""
from collections import Counter

ACTIVAS = "c.removida = 0 AND s.removida = 0"


def para_minutos(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def para_hhmm(minutos):
    return f"{minutos // 60:02d}:{minutos % 60:02d}"


def grelha(empresa):
    """Lista de horários 'HH:MM' da empresa (linha da tabela empresas),
    seguida dos `slots_tolerancia` horários de tolerância depois do último
    que acaba até `hora_fim` (RH que aceitam prolongar, 29 Set 2026)."""
    ini, fim, dur = empresa["hora_inicio"], empresa["hora_fim"], empresa["duracao_entrevista_min"]
    if not (ini and fim and dur):
        return []
    normais = list(range(para_minutos(ini), para_minutos(fim) - dur + 1, dur))
    seguinte = normais[-1] + dur if normais else para_minutos(ini)
    extra = [seguinte + i * dur for i in range(tolerancia(empresa))]
    return [para_hhmm(t) for t in normais + extra]


def tolerancia(empresa):
    return empresa["slots_tolerancia"] if "slots_tolerancia" in empresa.keys() and empresa["slots_tolerancia"] else 0


def e_tolerancia(empresa, horario):
    """Horário que acaba depois de `hora_fim`: é de tolerância."""
    fim, dur = empresa["hora_fim"], empresa["duracao_entrevista_min"]
    return bool(horario and fim and dur) and para_minutos(horario) + dur > para_minutos(fim)


def sobrepoe(ini_a, dur_a, ini_b, dur_b):
    a, b = para_minutos(ini_a), para_minutos(ini_b)
    return a < b + dur_b and b < a + dur_a


def submissoes_da_pessoa(con, id_submissao):
    """A própria submissão e as outras do mesmo candidato (mesmo NIF ou documento)."""
    row = con.execute("SELECT duplicado_de FROM submissoes WHERE id_submissao = ?", (id_submissao,)).fetchone()
    outras = [s.strip() for s in (row["duplicado_de"] if row else "").split(",") if s.strip()]
    return [id_submissao] + outras


def marcacoes_da_pessoa(con, id_submissao, excluir_empresa=None):
    """Entrevistas já marcadas ao candidato, excepto a candidatura (id_submissao, excluir_empresa)."""
    sids = submissoes_da_pessoa(con, id_submissao)
    rows = con.execute(
        f"""SELECT c.id_submissao, c.empresa, c.horario_entrevista AS horario, e.duracao_entrevista_min AS dur
            FROM candidaturas c
            JOIN submissoes s ON s.id_submissao = c.id_submissao
            JOIN empresas e ON e.empresa = c.empresa
            WHERE {ACTIVAS} AND c.horario_entrevista IS NOT NULL
              AND c.id_submissao IN ({','.join('?' * len(sids))})""",
        sids,
    ).fetchall()
    return [r for r in rows if not (r["id_submissao"] == id_submissao and r["empresa"] == excluir_empresa)]


def ocupacao(con, empresa, excluir_submissao=None):
    """Nº de candidatos marcados em cada horário da empresa."""
    rows = con.execute(
        f"""SELECT c.id_submissao, c.horario_entrevista FROM candidaturas c
            JOIN submissoes s ON s.id_submissao = c.id_submissao
            WHERE {ACTIVAS} AND c.empresa = ? AND c.horario_entrevista IS NOT NULL""",
        (empresa,),
    ).fetchall()
    return Counter(r["horario_entrevista"] for r in rows if r["id_submissao"] != excluir_submissao)


def empresa_por_nome(con, empresa):
    return con.execute("SELECT * FROM empresas WHERE empresa = ?", (empresa,)).fetchone()


def disponibilidade(con, id_submissao, empresa):
    """Para cada horário da empresa: (horario, livre, motivo, lugares_livres)."""
    emp = empresa_por_nome(con, empresa)
    if emp is None:
        return []
    lugares = emp["n_entrevistadores"] or 1
    dur = emp["duracao_entrevista_min"]
    occ = ocupacao(con, empresa, excluir_submissao=id_submissao)
    outras = marcacoes_da_pessoa(con, id_submissao, excluir_empresa=empresa)
    res = []
    for h in grelha(emp):
        livres = lugares - occ[h]
        motivo = None
        if livres <= 0:
            motivo = "ocupado nesta empresa"
        else:
            for o in outras:
                if o["dur"] and sobrepoe(h, dur, o["horario"], o["dur"]):
                    motivo = f"candidato já marcado às {o['horario']} em {o['empresa']}"
                    break
        res.append({"horario": h, "livre": motivo is None, "motivo": motivo, "lugares_livres": max(livres, 0),
                    "tolerancia": e_tolerancia(emp, h)})
    return res


def motivo_recusa(con, id_submissao, empresa, horario):
    """None se o horário pode ser marcado; senão o motivo."""
    for d in disponibilidade(con, id_submissao, empresa):
        if d["horario"] == horario:
            return d["motivo"]
    return "horário fora da grelha desta empresa"


def lista_de_espera(con, empresa):
    """Validados sem horário, pela ordem em que foram validados."""
    return con.execute(
        f"""SELECT c.id_submissao, c.empresa, c.validacao_alterada_em, s.cand_nome
            FROM candidaturas c JOIN submissoes s ON s.id_submissao = c.id_submissao
            WHERE {ACTIVAS} AND c.empresa = ? AND c.validacao = 'Validado'
              AND c.horario_entrevista IS NULL
            ORDER BY c.validacao_alterada_em IS NULL, c.validacao_alterada_em, s.criada_em""",
        (empresa,),
    ).fetchall()


def vagas(con):
    """Vagas de inscrição por empresa, contadas como o R1 (desde 1 Out 2026):
    ocupam lugar as candidaturas de respostas terminadas que não estão
    anuladas. A sincronização já anula as inscrições repetidas (mais de 3
    empresas por candidato, ou a mesma empresa duas vezes), e uma anulação
    feita à mão liberta o lugar. Diferença possível: as respostas que ainda
    não entraram na BD (até 5 min), que o R1 já conta."""
    contagem = Counter(
        r["empresa"] for r in con.execute(
            f"SELECT c.empresa FROM candidaturas c "
            f"JOIN submissoes s ON s.id_submissao = c.id_submissao WHERE {ACTIVAS} AND c.validacao <> 'Anulado'"))
    res = []
    for e in con.execute("SELECT * FROM empresas WHERE removida = 0 ORDER BY empresa"):
        n = contagem[e["empresa"]]
        cap = e["capacidade_maxima"]
        livres = max(cap - n, 0) if cap is not None else None
        if e["desistiu"]:
            situacao, livres = "Desistiu da Feira", 0
        elif livres == 0:
            situacao = "Esgotada"
        elif e["no_formulario"] == 0:
            situacao = "Retirada do formulário"
        else:
            situacao = "Aberta"
        res.append({"e": e, "inscritas": n, "capacidade": cap, "livres": livres, "situacao": situacao,
                    "aberta": situacao == "Aberta"})
    return res


def posicoes_em_espera(con):
    """{(id_submissao, empresa): posição na lista de espera}."""
    pos = {}
    for (empresa,) in con.execute("SELECT empresa FROM empresas").fetchall():
        for i, r in enumerate(lista_de_espera(con, empresa), 1):
            pos[(r["id_submissao"], empresa)] = i
    return pos
