#!/usr/bin/env python3
"""
Exportação Formbricks → SQLite (FE2026).

Lê as respostas do formulário de candidaturas e mantém uma base SQLite que
alimenta os dashboards (técnicos validadores, coordenação e empresas). Formato
"longo" da especificação (§2): uma linha por par candidato + empresa.

Tabelas:
  submissoes   uma linha por resposta do Formbricks (dados da entidade e do
               candidato). Refrescada a cada sincronização: o Formbricks é a
               fonte de verdade destes campos. As colunas *_canon e
               tecnico_id unificam as grafias da entidade e do técnico
               (entidades.py).
  candidaturas uma linha por (submissão, empresa). As ofertas vêm do
               Formbricks; as colunas de trabalho (id_tecnico, validacao,
               horario_entrevista, observacoes) pertencem aos dashboards e a
               sincronização NUNCA as altera. Única excepção: uma
               candidatura ainda sem técnico recebe o técnico da empresa
               definido em empresas-config.json (atribuir_tecnicos()). As
               regras das inscrições repetidas (anular_duplicados()) escrevem
               a validação e uma nota, e a nota sai das observações quando a
               candidatura deixa de estar anulada (limpar_notas_automaticas()).
  empresas     configuração por empresa (capacidade, horas de início e fim,
               duração da entrevista, nº de entrevistadores). Os valores vêm
               de empresas-config.json; um campo que o ficheiro não defina
               mantém o valor que já está na BD. Guarda também, lidos do
               formulário, as ofertas e se a empresa ainda lá está (vagas).
  sincronizacoes  registo de cada execução.
  v_candidaturas  vista plana (tabela longa com todas as colunas), a usar
               pelos dashboards e pela exportação CSV.

Uma resposta ou empresa que desapareça do Formbricks não é apagada: fica com
removida=1, para não perder validações ou horários já marcados. As
candidaturas criadas à mão (manual=1, p. ex. entrevistas feitas no dia numa
empresa onde o candidato não se tinha inscrito) não vêm do Formbricks e a
sincronização não as remove.

Uso:
    python3 exportar_candidaturas.py                    # sincroniza para a BD por omissão
    python3 exportar_candidaturas.py --db caminho.db
    python3 exportar_candidaturas.py --csv saida.csv    # sincroniza e exporta v_candidaturas
    python3 exportar_candidaturas.py --dry-run          # só mostra o resumo, não escreve
"""
import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Capacidade por empresa e identificação da empresa pelo rótulo: as mesmas do
# R1. A capacidade só é usada ao criar a linha da empresa pela 1ª vez.
import entidades
from dashboard.notas import sem_notas_automaticas
from r1_capacidade import (ANULADO_DUPLICADO, CAPACITY, DESISTENCIAS, alterada_a_mao, classificar,
                           company_from_label, doc_ficticio, nif_ficticio, norm_doc, norm_nif)

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.environ.get("FE2026_ENV_FILE", os.path.expanduser("~/.config/jfc/formbricks-gec-admin.env"))
MAPPING_FILE = os.path.join(HERE, "formbricks-empresas-mapping.json")
CONFIG_FILE = os.path.join(HERE, "empresas-config.json")
DEFAULT_DB = os.environ.get("FE2026_DB", os.path.join(HERE, "dados", "fe2026.db"))
TZ = ZoneInfo("Europe/Lisbon")

MASTER_ELEMENT = "empresas_master"
MAX_EMPRESAS = 3
NENHUMA_FALLBACK = "Não me candidato a esta oferta"

SUBMISSAO_CAMPOS = {
    # coluna na BD: id do elemento no Formbricks
    "entidade_nome": "entidade_nome",
    "entidade_tecnico": "entidade_tecnico",
    "entidade_email": "entidade_email",
    "entidade_telefone": "entidade_telefone",
    "cand_nome": "cand_nome",
    "cand_telefone": "cand_telefone",
    "cand_email": "cand_email",
    "cand_localidade": "cand_localidade",
    "cand_habilitacoes": "cand_habilitacoes",
    "cand_nif": "cand_nif",
    "cand_tipo_doc": "cand_tipo_doc",
    "cand_num_doc": "cand_num_doc",
}
RGPD_ELEMENTS = ("rgpd_1", "rgpd_2", "rgpd_3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissoes (
    id_submissao      TEXT PRIMARY KEY,
    criada_em         TEXT NOT NULL,          -- UTC ISO, do Formbricks
    criada_em_local   TEXT NOT NULL,          -- Europe/Lisbon, 'AAAA-MM-DD HH:MM:SS'
    actualizada_em    TEXT,
    entidade_nome     TEXT,
    entidade_tecnico  TEXT,
    entidade_email    TEXT,
    entidade_telefone TEXT,
    cand_nome         TEXT,
    cand_telefone     TEXT,
    cand_email        TEXT,
    cand_localidade   TEXT,
    cand_habilitacoes TEXT,
    cand_nif          TEXT,
    cand_tipo_doc     TEXT,
    cand_num_doc      TEXT,
    cand_nif_norm     TEXT,                   -- só dígitos, para detectar duplicados
    cand_num_doc_norm TEXT,                   -- sem espaços/pontuação, maiúsculas
    cv_url            TEXT,
    rgpd_aceite       INTEGER NOT NULL,       -- 1 se as 3 declarações foram aceites
    n_empresas        INTEGER NOT NULL,
    flags             TEXT NOT NULL DEFAULT '', -- ex. 'Duplicado; Mais de 3 empresas'
    duplicado_de      TEXT NOT NULL DEFAULT '', -- ids das outras submissões com o mesmo NIF ou documento
    removida          INTEGER NOT NULL DEFAULT 0,
    sincronizada_em   TEXT NOT NULL,
    -- Entidade e técnico unificados (entidades.py, 6 Out 2026)
    entidade_canon      TEXT,
    tecnico_id          TEXT,                 -- id da submissão mais antiga do mesmo técnico
    tecnico_canon       TEXT,
    tecnico_email_canon TEXT,
    tecnico_tel_canon   TEXT
);

CREATE TABLE IF NOT EXISTS candidaturas (
    id_submissao         TEXT NOT NULL REFERENCES submissoes(id_submissao),
    empresa              TEXT NOT NULL REFERENCES empresas(empresa),
    ofertas_seleccionadas TEXT NOT NULL DEFAULT '',  -- ofertas separadas por ' | '
    sem_oferta           INTEGER NOT NULL DEFAULT 0, -- 1 se não escolheu nenhuma oferta da empresa
    ordem_empresa        INTEGER NOT NULL,           -- nº de ordem da candidatura nessa empresa (por data)
    excede_capacidade    INTEGER NOT NULL DEFAULT 0,
    removida             INTEGER NOT NULL DEFAULT 0,
    criada_em            TEXT NOT NULL,              -- quando a linha entrou na BD
    -- Colunas de trabalho dos dashboards: a sincronização nunca as altera.
    id_tecnico           TEXT,
    validacao            TEXT NOT NULL DEFAULT 'Por validar'
                         CHECK (validacao IN ('Por validar', 'Validado', 'Anulado')),
    horario_entrevista   TEXT,                       -- 'HH:MM'
    observacoes          TEXT,
    validacao_alterada_em  TEXT,
    validacao_alterada_por TEXT,
    manual               INTEGER NOT NULL DEFAULT 0, -- 1 se a candidatura foi criada à mão (não vem do Formbricks)
    PRIMARY KEY (id_submissao, empresa)
);
CREATE INDEX IF NOT EXISTS idx_candidaturas_empresa ON candidaturas(empresa);
CREATE INDEX IF NOT EXISTS idx_candidaturas_tecnico ON candidaturas(id_tecnico);

CREATE TABLE IF NOT EXISTS empresas (
    empresa                   TEXT PRIMARY KEY,
    choice_id                 TEXT NOT NULL,
    capacidade_maxima         INTEGER,
    hora_inicio               TEXT,      -- 'HH:MM'
    hora_fim                  TEXT,      -- 'HH:MM', fim do período de entrevistas
    duracao_entrevista_min    INTEGER,
    n_entrevistadores         INTEGER,
    periodo                   TEXT,
    requisitos                TEXT,
    removida                  INTEGER NOT NULL DEFAULT 0,
    slots_tolerancia          INTEGER NOT NULL DEFAULT 0, -- horários a mais depois de hora_fim
    ofertas                   TEXT,      -- ofertas no formulário, separadas por ' | '
    no_formulario             INTEGER,   -- 1 se a empresa está no ecrã mestre (o R1 retira as esgotadas)
    desistiu                  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sincronizacoes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    em                TEXT NOT NULL,
    respostas_lidas   INTEGER NOT NULL,
    submissoes        INTEGER NOT NULL,
    candidaturas      INTEGER NOT NULL,
    novas             INTEGER NOT NULL,
    removidas         INTEGER NOT NULL,
    avisos            TEXT NOT NULL DEFAULT ''
);
"""

# Depois das colunas acrescentadas em connect(): a vista usa-as.
VIEW = """
DROP VIEW IF EXISTS v_candidaturas;
CREATE VIEW v_candidaturas AS
SELECT
    c.id_submissao, s.criada_em_local AS timestamp,
    s.entidade_nome, s.entidade_tecnico, s.entidade_email, s.entidade_telefone,
    s.cand_nome, s.cand_telefone, s.cand_email, s.cand_localidade, s.cand_habilitacoes,
    s.cand_nif, s.cand_tipo_doc, s.cand_num_doc, s.cv_url,
    c.empresa, c.ofertas_seleccionadas, c.sem_oferta,
    c.ordem_empresa, e.capacidade_maxima, c.excede_capacidade,
    c.id_tecnico, c.validacao, c.horario_entrevista, c.observacoes,
    trim(s.flags
         || CASE WHEN c.sem_oferta = 1 THEN '; Sem oferta' ELSE '' END
         || CASE WHEN c.excede_capacidade = 1 THEN '; Excede capacidade' ELSE '' END,
         '; ') AS flags,
    s.duplicado_de,
    s.entidade_canon AS entidade, s.tecnico_canon AS tecnico_entidade
FROM candidaturas c
JOIN submissoes s ON s.id_submissao = c.id_submissao
LEFT JOIN empresas e ON e.empresa = c.empresa
WHERE c.removida = 0 AND s.removida = 0
ORDER BY s.criada_em, c.empresa;
"""


# ── Formbricks ────────────────────────────────────────────────────────────

def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


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


def api_get(base_url, path, api_key):
    req = urllib.request.Request(f"{base_url}{path}")
    req.add_header("x-api-key", api_key)
    req.add_header("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) gec-fe2026-exportar")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GET {path} -> {e.code}: {e.read().decode('utf-8')}") from e


def fetch_responses(base_url, api_key, survey_id):
    responses, skip, limit = [], 0, 250
    while True:
        page = api_get(base_url, f"/api/v2/management/responses?surveyId={survey_id}&limit={limit}&skip={skip}", api_key)
        batch = page.get("data", [])
        responses.extend(batch)
        if len(batch) < limit:
            return responses
        skip += limit


def survey_structure(survey, company_blocks):
    """Da definição do formulário: elemento de ofertas de cada empresa e os
    rótulos da alternativa «Não me candidato» (as respostas guardam rótulos)."""
    block_to_company = {v: k for k, v in company_blocks.items()}
    offer_element = {}      # id do elemento → empresa
    nenhuma_labels = {NENHUMA_FALLBACK}
    for block in survey["blocks"]:
        company = block_to_company.get(block["id"])
        if company is None:
            continue
        for el in block["elements"]:
            offer_element[el["id"]] = company
            for ch in el.get("choices") or []:
                if ch["id"].endswith("_nenhuma"):
                    nenhuma_labels.update(ch["label"].values())
    return offer_element, nenhuma_labels


def estado_formulario(survey, mapping):
    """{empresa: {"ofertas": "a | b", "no_formulario": 0/1}} tal como o
    formulário está agora. Serve a listagem de vagas do dashboard: o R1 retira
    do ecrã mestre as empresas esgotadas ou que desistiram."""
    block_to_company = {v: k for k, v in mapping["company_blocks"].items()}
    choice_to_company = {v: k for k, v in mapping["company_choice_ids"].items()}
    res = {c: {"ofertas": None, "no_formulario": 0} for c in mapping["company_choice_ids"]}
    for block in survey["blocks"]:
        company = block_to_company.get(block["id"])
        for el in block["elements"]:
            if el["id"] == mapping.get("master_element_id", MASTER_ELEMENT):
                for ch in el.get("choices") or []:
                    if ch["id"] in choice_to_company:
                        res[choice_to_company[ch["id"]]]["no_formulario"] = 1
            elif company is not None:
                ofertas = [next(iter(ch["label"].values()), "") for ch in el.get("choices") or []
                           if not ch["id"].endswith("_nenhuma")]
                res[company]["ofertas"] = " | ".join(o.strip() for o in ofertas if o.strip())
    return res


# ── Transformação ─────────────────────────────────────────────────────────

def to_local(iso_utc):
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")


def as_list(v):
    if v is None or v == "":
        return []
    return v if isinstance(v, list) else [v]


def transform(responses, companies, offer_element, nenhuma_labels):
    """Respostas terminadas → (submissões, candidaturas, avisos).

    Uma resposta parcial ainda não é uma inscrição (e pode nem ter o RGPD
    aceite), por isso fica de fora, tal como no R1.
    """
    subs, cands, avisos = [], [], []
    for r in responses:
        if not r.get("finished"):
            continue
        data = r.get("data") or {}
        sid = r["id"]
        empresas = []
        for label in as_list(data.get(MASTER_ELEMENT)):
            company = company_from_label(label, companies)
            if company is None:
                avisos.append(f"{sid}: empresa não reconhecida no rótulo {label!r}")
            elif company not in empresas:
                empresas.append(company)

        # Manda o ecrã mestre. Se o técnico voltou atrás e desmarcou uma
        # empresa, o Formbricks guarda na mesma as ofertas que já tinham sido
        # escolhidas para ela (visto a 23 Set 2026): essas são ignoradas.
        ofertas = {c: [] for c in empresas}
        for el_id, company in offer_element.items():
            if company not in ofertas:
                continue
            for label in as_list(data.get(el_id)):
                if label not in nenhuma_labels:
                    ofertas[company].append(label)

        cv = as_list(data.get("cand_cv"))
        sub = {col: (data.get(el) or "").strip() if isinstance(data.get(el), str) else data.get(el)
               for col, el in SUBMISSAO_CAMPOS.items()}
        sub.update(
            id_submissao=sid,
            criada_em=r["createdAt"],
            criada_em_local=to_local(r["createdAt"]),
            actualizada_em=r.get("updatedAt"),
            cand_nif_norm=norm_nif(sub["cand_nif"]),
            cand_num_doc_norm=norm_doc(sub["cand_num_doc"]),
            cv_url=cv[0] if cv else None,
            rgpd_aceite=int(all(data.get(e) == "accepted" for e in RGPD_ELEMENTS)),
            n_empresas=len(empresas),
        )
        subs.append(sub)
        for company in empresas:
            cands.append({
                "id_submissao": sid,
                "empresa": company,
                "ofertas_seleccionadas": " | ".join(ofertas[company]),
                "sem_oferta": int(not ofertas[company]),
            })
    return subs, cands, avisos


def compute_flags(subs):
    """R2 (mais de 3 empresas), R3 (duplicado por NIF OU nº de documento) e
    RGPD por aceitar. Não bloqueia nada: só sinaliza para revisão."""
    by_nif, by_doc = {}, {}
    for s in subs:
        if not nif_ficticio(s["cand_nif_norm"]):
            by_nif.setdefault(s["cand_nif_norm"], []).append(s["id_submissao"])
        if not doc_ficticio(s["cand_num_doc_norm"]):
            by_doc.setdefault(s["cand_num_doc_norm"], []).append(s["id_submissao"])
    for s in subs:
        outros = set(by_nif.get(s["cand_nif_norm"], [])) | set(by_doc.get(s["cand_num_doc_norm"], []))
        outros.discard(s["id_submissao"])
        flags = []
        if outros:
            flags.append("Duplicado")
        if s["n_empresas"] > MAX_EMPRESAS:
            flags.append(f"Mais de {MAX_EMPRESAS} empresas")
        if not s["rgpd_aceite"]:
            flags.append("RGPD por aceitar")
        s["flags"] = "; ".join(flags)
        s["duplicado_de"] = ", ".join(sorted(outros))


# ── SQLite ────────────────────────────────────────────────────────────────

def connect(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")   # os dashboards lêem enquanto se sincroniza
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    # Colunas acrescentadas depois de a BD existir no CT129.
    cols = {r[1] for r in con.execute("PRAGMA table_info(submissoes)")}
    if "duplicado_tratado" not in cols:
        con.execute("ALTER TABLE submissoes ADD COLUMN duplicado_tratado INTEGER NOT NULL DEFAULT 0")
    if "manual" not in {r[1] for r in con.execute("PRAGMA table_info(candidaturas)")}:
        con.execute("ALTER TABLE candidaturas ADD COLUMN manual INTEGER NOT NULL DEFAULT 0")
    cols = {r[1] for r in con.execute("PRAGMA table_info(empresas)")}
    for col, tipo in (("slots_tolerancia", "INTEGER NOT NULL DEFAULT 0"), ("ofertas", "TEXT"),
                      ("no_formulario", "INTEGER"), ("desistiu", "INTEGER NOT NULL DEFAULT 0")):
        if col not in cols:
            con.execute(f"ALTER TABLE empresas ADD COLUMN {col} {tipo}")
    cols = {r[1] for r in con.execute("PRAGMA table_info(submissoes)")}
    for col in entidades.CAMPOS:
        if col not in cols:
            con.execute(f"ALTER TABLE submissoes ADD COLUMN {col} TEXT")
    con.executescript(VIEW)
    return con


def anular_duplicados(con, agora):
    """R3 (revisto a 1 Out 2026, ver classificar() no r1_capacidade.py): o
    mesmo candidato pode ter várias inscrições até 3 empresas no total; só a
    candidatura que repete uma empresa ou passa das 3 é anulada. Toca apenas
    no que ninguém tratou à mão (validacao_alterada_por vazio ou
    "Automático…"): uma candidatura por validar que a regra recusa passa a
    Anulado; uma anulada automaticamente que a regra já aceita (regra nova)
    volta a Por validar. Uma anulação à mão continua a ser uma das 3
    empresas do candidato: só liberta a vaga na empresa. O que a
    coordenação reverte fica como está, porque deixa de ser automático.
    Devolve (anuladas, repostas)."""
    subs = {r[0]: {"id": r[0], "criada_em": r[1], "nif": r[2] or "", "doc": r[3] or "", "empresas": []}
            for r in con.execute("SELECT id_submissao, criada_em, cand_nif_norm, cand_num_doc_norm "
                                 "FROM submissoes WHERE removida = 0")}
    estados = {}
    for sid, emp, val, por in con.execute(
            "SELECT id_submissao, empresa, validacao, validacao_alterada_por FROM candidaturas WHERE removida = 0"):
        if sid in subs:
            subs[sid]["empresas"].append(emp)
            estados[(sid, emp)] = (val, por)
    anuladas = repostas = 0
    for (sid, emp), motivo in classificar(list(subs.values()), estados).items():
        val, por = estados[(sid, emp)]
        if alterada_a_mao(por):
            continue
        if motivo and val == "Por validar":
            nota = f"Inscrição repetida: {motivo}. Anulada automaticamente."
            por_novo, val_novo = ANULADO_DUPLICADO, "Anulado"
            anuladas += 1
        elif motivo is None and val == "Anulado" and por == ANULADO_DUPLICADO:
            nota = "Anulação automática revista: o candidato fica dentro das 3 empresas, sem repetir nenhuma."
            por_novo, val_novo = "Automático (duplicado revisto)", "Por validar"
            repostas += 1
        else:
            continue
        con.execute(
            "UPDATE candidaturas SET validacao = ?, horario_entrevista = NULL, "
            "validacao_alterada_em = ?, validacao_alterada_por = ?, "
            "observacoes = CASE WHEN observacoes IS NULL OR observacoes = '' THEN ? ELSE observacoes || char(10) || ? END "
            "WHERE id_submissao = ? AND empresa = ?",
            (val_novo, agora, por_novo, nota, nota, sid, emp))
    return anuladas, repostas


def limpar_notas_automaticas(con):
    """Tira das observações as notas de anulação automática das candidaturas
    que entretanto voltaram a «Por validar» ou foram validadas, pela
    coordenação ou pela revisão automática (7 Out 2026). O resto do texto
    fica. Devolve o n.º de candidaturas alteradas."""
    n = 0
    for sid, emp, obs in con.execute(
            "SELECT id_submissao, empresa, observacoes FROM candidaturas "
            "WHERE validacao IN ('Por validar', 'Validado') AND observacoes IS NOT NULL").fetchall():
        nova = sem_notas_automaticas(obs)
        if nova != obs:
            con.execute("UPDATE candidaturas SET observacoes = ? WHERE id_submissao = ? AND empresa = ?",
                        (nova, sid, emp))
            n += 1
    return n


ATRIBUIDO_AUTO = "Automático (sincronização)"


def atribuir_tecnicos(con, config, agora):
    """Atribuição automática (2 Out 2026): uma candidatura sem técnico recebe
    o técnico validador da empresa, dado por "tecnicos" em
    empresas-config.json (lista de id_tecnico). Com mais de um, vai para o
    que tem menos candidaturas nessa empresa e, em empate, para o primeiro
    da lista. Só conta um id com link activo na tabela tokens do dashboard,
    para nenhuma candidatura ficar com alguém que não a consegue ver. Nunca
    muda um técnico já atribuído: a coordenação continua a poder reatribuir
    no dashboard. Devolve ({empresa: {id_tecnico: n}}, avisos)."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tokens'").fetchone():
        return {}, ["sem tabela tokens: atribuição automática de técnicos não aplicada"]
    activos = {r[0] for r in con.execute(
        "SELECT id_tecnico FROM tokens WHERE revogado = 0 AND id_tecnico IS NOT NULL AND expira_em > ?", (agora,))}
    feitas, avisos = {}, []
    for company, cfg in config["empresas"].items():
        lista = cfg.get("tecnicos") or []
        sem_link = [t for t in lista if t not in activos]
        if sem_link:
            avisos.append(f"{company}: técnicos sem link activo, não recebem candidaturas: {', '.join(sem_link)}")
        lista = [t for t in lista if t in activos]
        if not lista:
            continue
        carga = dict.fromkeys(lista, 0)
        for t, n in con.execute(
                "SELECT id_tecnico, count(*) FROM candidaturas WHERE empresa = ? AND removida = 0 "
                "GROUP BY id_tecnico", (company,)):
            if t in carga:
                carga[t] = n
        pendentes = con.execute(
            "SELECT c.id_submissao FROM candidaturas c JOIN submissoes s ON s.id_submissao = c.id_submissao "
            "WHERE c.empresa = ? AND c.removida = 0 AND s.removida = 0 "
            "AND (c.id_tecnico IS NULL OR c.id_tecnico = '') ORDER BY s.criada_em, c.id_submissao",
            (company,)).fetchall()
        for (sid,) in pendentes:
            t = min(lista, key=lambda x: (carga[x], lista.index(x)))
            con.execute("UPDATE candidaturas SET id_tecnico = ? WHERE id_submissao = ? AND empresa = ?",
                        (t, sid, company))
            carga[t] += 1
            feitas.setdefault(company, {}).setdefault(t, 0)
            feitas[company][t] += 1
    # Fica no registo de acessos do dashboard, como as atribuições à mão.
    if feitas and con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'acessos'").fetchone():
        for company, por_tecnico in feitas.items():
            for t, n in por_tecnico.items():
                con.execute("INSERT INTO acessos (em, nome, accao, empresa, detalhe) VALUES (?, ?, 'atribuir', ?, ?)",
                            (agora, ATRIBUIDO_AUTO, company, f"{n} candidaturas → {t}"))
    return feitas, avisos


def load_config(path, companies):
    """Configuração das empresas (empresas-config.json) e avisos de
    incoerência com o mapeamento e com a capacidade usada pelo R1."""
    if not os.path.exists(path):
        return {"empresas": {}}, [f"sem ficheiro de configuração {path}"]
    config = read_json(path)
    avisos = []
    for company, cfg in config["empresas"].items():
        if company not in companies:
            avisos.append(f"configuração de empresa inexistente no formulário: {company}")
        cap = cfg.get("capacidade_maxima")
        if cap is not None and CAPACITY.get(company) not in (None, cap):
            avisos.append(f"{company}: capacidade {cap} na configuração, {CAPACITY[company]} no R1")
        tec = cfg.get("tecnicos", [])
        if not (isinstance(tec, list) and all(isinstance(t, str) and t for t in tec)):
            avisos.append(f"{company}: \"tecnicos\" tem de ser uma lista de id_tecnico")
            cfg["tecnicos"] = []
    for company in companies:
        if company not in config["empresas"]:
            avisos.append(f"{company}: sem configuração de entrevistas")
    return config, avisos


def sync(con, subs, cands, company_choice_ids, n_responses, avisos, config=None, formulario=None, aliases=None):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    config = config or {"empresas": {}}
    formulario = formulario or {}
    # Entidade e técnico unificados: calculados sobre todas as submissões
    # terminadas, porque um grupo junta inscrições de dias diferentes.
    entidades.unificar(subs, entidades.carregar_aliases() if aliases is None else aliases)
    # BEGIN IMMEDIATE: tranca a escrita durante a sincronização, para não se
    # cruzar com uma marcação de horário feita no dashboard.
    con.execute("BEGIN IMMEDIATE")
    try:
        # O ficheiro de configuração manda nos campos que define; os que não
        # define mantêm o valor da BD. A capacidade do R1 só serve de valor
        # inicial.
        for company, choice_id in company_choice_ids.items():
            cfg = config["empresas"].get(company, {})
            p = {
                "empresa": company, "choice_id": choice_id,
                "cap": cfg.get("capacidade_maxima"), "cap_inicial": CAPACITY.get(company),
                "ini": cfg.get("hora_inicio", config.get("hora_inicio") if cfg else None),
                "fim": cfg.get("hora_fim", config.get("hora_fim") if cfg else None),
                "dur": cfg.get("duracao_entrevista_min"), "ent": cfg.get("n_entrevistadores"),
                "tol": cfg.get("slots_tolerancia", config.get("slots_tolerancia") if cfg else None),
                "ofertas": formulario.get(company, {}).get("ofertas"),
                "no_form": formulario.get(company, {}).get("no_formulario"),
                "desistiu": int(company in DESISTENCIAS),
            }
            con.execute(
                "INSERT INTO empresas (empresa, choice_id, capacidade_maxima, hora_inicio, hora_fim, "
                "duracao_entrevista_min, n_entrevistadores, slots_tolerancia, ofertas, no_formulario, desistiu) "
                "VALUES (:empresa, :choice_id, COALESCE(:cap, :cap_inicial), :ini, :fim, :dur, :ent, "
                "COALESCE(:tol, 0), :ofertas, :no_form, :desistiu) "
                "ON CONFLICT(empresa) DO UPDATE SET choice_id = excluded.choice_id, removida = 0, "
                "capacidade_maxima = COALESCE(:cap, capacidade_maxima), "
                "hora_inicio = COALESCE(:ini, hora_inicio), hora_fim = COALESCE(:fim, hora_fim), "
                "duracao_entrevista_min = COALESCE(:dur, duracao_entrevista_min), "
                "n_entrevistadores = COALESCE(:ent, n_entrevistadores), "
                "slots_tolerancia = COALESCE(:tol, slots_tolerancia), "
                "ofertas = COALESCE(:ofertas, ofertas), no_formulario = COALESCE(:no_form, no_formulario), "
                "desistiu = :desistiu",
                p,
            )
        con.execute(
            f"UPDATE empresas SET removida = 1 WHERE empresa NOT IN ({','.join('?' * len(company_choice_ids))})",
            list(company_choice_ids),
        )

        for s in subs:
            s["sincronizada_em"] = now
            names = list(s.keys())
            con.execute(
                f"INSERT INTO submissoes ({', '.join(names)}) VALUES ({', '.join('?' * len(names))}) "
                f"ON CONFLICT(id_submissao) DO UPDATE SET "
                + ", ".join(f"{n} = excluded.{n}" for n in names if n != "id_submissao")
                + ", removida = 0",
                [s[n] for n in names],
            )
        live_ids = {s["id_submissao"] for s in subs}
        gone = [(sid,) for (sid,) in con.execute("SELECT id_submissao FROM submissoes WHERE removida = 0")
                if sid not in live_ids]
        con.executemany("UPDATE submissoes SET removida = 1 WHERE id_submissao = ?", gone)
        removidas_sub = len(gone)

        existing = {(r[0], r[1]) for r in con.execute("SELECT id_submissao, empresa FROM candidaturas")}
        novas = 0
        for c in cands:
            key = (c["id_submissao"], c["empresa"])
            if key not in existing:
                novas += 1
            con.execute(
                "INSERT INTO candidaturas (id_submissao, empresa, ofertas_seleccionadas, sem_oferta, ordem_empresa, criada_em) "
                "VALUES (?, ?, ?, ?, 0, ?) "
                "ON CONFLICT(id_submissao, empresa) DO UPDATE SET "
                "ofertas_seleccionadas = excluded.ofertas_seleccionadas, sem_oferta = excluded.sem_oferta, removida = 0",
                (c["id_submissao"], c["empresa"], c["ofertas_seleccionadas"], c["sem_oferta"], now),
            )
        live_pairs = {(c["id_submissao"], c["empresa"]) for c in cands}
        gone = [pair for pair in con.execute("SELECT id_submissao, empresa FROM candidaturas WHERE removida = 0 AND manual = 0")
                if pair not in live_pairs]
        con.executemany("UPDATE candidaturas SET removida = 1 WHERE id_submissao = ? AND empresa = ?", gone)
        removidas_cand = len(gone)

        anuladas, repostas = anular_duplicados(con, datetime.now(timezone.utc).isoformat(timespec="microseconds"))
        notas_limpas = limpar_notas_automaticas(con)
        atribuidas, avisos_atrib = atribuir_tecnicos(con, config, now)
        n_atrib = sum(n for por_tecnico in atribuidas.values() for n in por_tecnico.values())

        # Ordem de chegada por empresa e excesso de capacidade (R1 na
        # validação): candidaturas anuladas ou removidas não ocupam lugar.
        con.execute("UPDATE candidaturas SET ordem_empresa = 0, excede_capacidade = 0")
        con.execute("""
            WITH ordenadas AS (
                SELECT c.rowid AS rid,
                       row_number() OVER (PARTITION BY c.empresa ORDER BY s.criada_em, c.id_submissao) AS n
                FROM candidaturas c JOIN submissoes s ON s.id_submissao = c.id_submissao
                WHERE c.removida = 0 AND s.removida = 0 AND c.validacao <> 'Anulado'
            )
            UPDATE candidaturas SET
                ordem_empresa = (SELECT n FROM ordenadas WHERE rid = candidaturas.rowid),
                excede_capacidade = COALESCE((
                    SELECT n > e.capacidade_maxima FROM ordenadas, empresas e
                    WHERE rid = candidaturas.rowid AND e.empresa = candidaturas.empresa
                      AND e.capacidade_maxima IS NOT NULL), 0)
            WHERE rowid IN (SELECT rid FROM ordenadas)
        """)

        con.execute(
            "INSERT INTO sincronizacoes (em, respostas_lidas, submissoes, candidaturas, novas, removidas, avisos) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, n_responses, len(subs), len(cands), novas, removidas_sub + removidas_cand,
             "\n".join(avisos + avisos_atrib
                       + ([f"{anuladas} candidaturas duplicadas anuladas"] if anuladas else [])
                       + ([f"{repostas} anulações automáticas revistas"] if repostas else [])
                       + ([f"{notas_limpas} notas de anulação automática retiradas"] if notas_limpas else [])
                       + ([f"{n_atrib} candidaturas atribuídas automaticamente"] if n_atrib else []))),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    for a in avisos_atrib:
        print(f"AVISO: {a}", file=sys.stderr)
    return novas, removidas_sub, removidas_cand, anuladas, atribuidas


def export_csv(con, path):
    cur = con.execute("SELECT * FROM v_candidaturas")
    header = [d[0] for d in cur.description]
    # utf-8-sig e ';' para abrir directamente no Excel em português.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(header)
        n = 0
        for row in cur:
            w.writerow(row)
            n += 1
    return n


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--csv", help="exportar v_candidaturas para este ficheiro CSV")
    parser.add_argument("--dry-run", action="store_true", help="não escreve na BD")
    args = parser.parse_args()

    env = load_env()
    base_url = env.get("FORMBRICKS_GEC_URL", "https://formularios.exemplo.org")
    api_key = env["FORMBRICKS_GEC_API_KEY"]
    mapping = read_json(MAPPING_FILE)
    company_choice_ids = mapping["company_choice_ids"]
    companies = list(company_choice_ids)

    survey = api_get(base_url, f"/api/v3/surveys/{mapping['survey_id']}", api_key)["data"]
    offer_element, nenhuma_labels = survey_structure(survey, mapping["company_blocks"])
    formulario = estado_formulario(survey, mapping)
    missing = set(companies) - set(offer_element.values())
    if missing:
        print(f"AVISO: sem bloco de ofertas no formulário para: {sorted(missing)}", file=sys.stderr)
    config, avisos_config = load_config(CONFIG_FILE, companies)
    for a in avisos_config:
        print(f"AVISO: {a}", file=sys.stderr)

    responses = fetch_responses(base_url, api_key, mapping["survey_id"])
    subs, cands, avisos = transform(responses, companies, offer_element, nenhuma_labels)
    compute_flags(subs)

    print(f"{len(responses)} respostas lidas, {len(subs)} terminadas → {len(cands)} candidaturas (candidato + empresa).")
    dup = sum(1 for s in subs if "Duplicado" in s["flags"])
    print(f"Sinalizadas: {dup} submissões duplicadas, "
          f"{sum(1 for c in cands if c['sem_oferta'])} candidaturas sem oferta escolhida.")
    for a in avisos:
        print(f"AVISO: {a}", file=sys.stderr)

    if args.dry_run:
        print("--dry-run: nada escrito.")
        return

    con = connect(args.db)
    try:
        novas, rs, rc, anuladas, atribuidas = sync(con, subs, cands, company_choice_ids, len(responses),
                                                   avisos + avisos_config, config, formulario)
        print(f"BD {args.db}: {novas} candidaturas novas, {rs} submissões e {rc} candidaturas marcadas como removidas, "
              f"{anuladas} anuladas por duplicado.")
        for company, por_tecnico in atribuidas.items():
            print(f"Atribuídas: {company} → " + ", ".join(f"{t} {n}" for t, n in por_tecnico.items()))
        if args.csv:
            n = export_csv(con, args.csv)
            print(f"CSV {args.csv}: {n} linhas.")
    finally:
        con.close()


if __name__ == "__main__":
    main()
