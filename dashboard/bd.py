"""Acesso à BD SQLite do FE2026 a partir do dashboard.

As tabelas submissoes, candidaturas e empresas são criadas e alimentadas por
exportar_candidaturas.py. O dashboard só acrescenta as suas: tokens de acesso,
registo de acessos (§5 e §5.1 da especificação) e o seguimento dos
candidatos pelas empresas depois da Feira (6 Out 2026).
"""
import hashlib
import os
import sqlite3
import unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DB_PATH = os.environ.get("FE2026_DB", "/var/lib/gec-fe2026/fe2026.db")
TZ = ZoneInfo("Europe/Lisbon")
# entidade (6 Out 2026): o técnico de uma entidade que inscreveu candidatos;
# só leitura, e só dos candidatos que inscreveu.
# rececao (7 Out 2026): o check-in no dia da Feira, e nada mais.
PERFIS = ("coordenador", "tecnico", "empresa", "entidade", "rececao")

TOKENS = """
CREATE TABLE IF NOT EXISTS {nome} (
    token_hash   TEXT PRIMARY KEY,          -- sha256 do token; o token em si não fica guardado
    prefixo      TEXT NOT NULL,             -- primeiros caracteres, para o identificar em listagens
    perfil       TEXT NOT NULL CHECK (perfil IN ('coordenador', 'tecnico', 'empresa', 'entidade', 'rececao')),
    nome         TEXT NOT NULL,             -- pessoa ou empresa a quem foi entregue
    id_tecnico   TEXT,                      -- perfil tecnico: valor de candidaturas.id_tecnico
    empresa      TEXT REFERENCES empresas(empresa),  -- perfil empresa
    criado_em    TEXT NOT NULL,
    expira_em    TEXT NOT NULL,             -- UTC ISO
    revogado     INTEGER NOT NULL DEFAULT 0,
    ultimo_acesso TEXT,
    ref_submissao TEXT,                     -- perfil entidade: uma submissão do técnico; o âmbito é o
                                            -- submissoes.tecnico_id dessa submissão
    email        TEXT,                      -- perfil entidade: e-mail do técnico, só para referência
    CHECK (perfil <> 'tecnico' OR id_tecnico IS NOT NULL),
    CHECK (perfil <> 'empresa' OR empresa IS NOT NULL),
    CHECK (perfil <> 'entidade' OR ref_submissao IS NOT NULL)
);
"""

SCHEMA = TOKENS.format(nome="tokens") + """
CREATE UNIQUE INDEX IF NOT EXISTS idx_tokens_tecnico ON tokens(id_tecnico) WHERE revogado = 0 AND id_tecnico IS NOT NULL;

CREATE TABLE IF NOT EXISTS acessos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    em           TEXT NOT NULL,
    prefixo      TEXT,
    perfil       TEXT,
    nome         TEXT,
    accao        TEXT NOT NULL,             -- ex. entrar, ver, cv, alterar, exportar, recusado
    id_submissao TEXT,
    empresa      TEXT,
    detalhe      TEXT,
    ip           TEXT
);
CREATE INDEX IF NOT EXISTS idx_acessos_em ON acessos(em);

-- Seguimento pelas empresas depois da Feira (6 Out 2026): preenchido pelos RH
-- no dashboard da empresa, uma linha por candidatura validada ou entrevista
-- acrescentada no dia; desde 13 Out 2026 também pelo técnico da entidade.
CREATE TABLE IF NOT EXISTS seguimento (
    id_submissao     TEXT NOT NULL,
    empresa          TEXT NOT NULL,
    nova_entrevista  TEXT CHECK (nova_entrevista IN ('Sim', 'Não')),
    selecionado      TEXT CHECK (selecionado IN ('Sim', 'Não')),
    contratado       TEXT CHECK (contratado IN ('Sim', 'Não')),
    tipo_contrato    TEXT,
    duracao_contrato TEXT,
    observacoes      TEXT,
    alterado_em      TEXT NOT NULL,
    alterado_por     TEXT NOT NULL,
    alterado_perfil  TEXT,                  -- empresa, entidade ou coordenador (13 Out 2026: o técnico
                                            -- da entidade também actualiza o seguimento)
    PRIMARY KEY (id_submissao, empresa)
);

-- E-mails enviados por correio.py (7 Out 2026): só o registo, sem o conteúdo
-- nem os links.
CREATE TABLE IF NOT EXISTS envios (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    em           TEXT NOT NULL,
    tipo         TEXT NOT NULL,             -- estado, link (e as variantes -teste)
    destinatario TEXT NOT NULL,
    ref          TEXT,                      -- estado: submissoes.tecnico_id; link: e-mail|nome do técnico
    assunto      TEXT,
    linhas       INTEGER,                   -- estado: n.º de candidaturas no e-mail
    resultado    TEXT NOT NULL,             -- enviado, ou erro: …
    assinatura   TEXT                       -- resumo do que foi comunicado; igual ao último = não se repete (8 Out)
);
CREATE INDEX IF NOT EXISTS idx_envios_ref ON envios(tipo, ref);

-- Avaliação do evento pelas empresas (13 Out 2026): ver AVALIACAO, abaixo.

-- Candidatos que aparecem na receção sem inscrição (13 Out 2026). As
-- entrevistas ficam na tabela rececao, como «extra», com id_submissao
-- 'avulso-<id>'; não entram nas candidaturas nem na sincronização.
CREATE TABLE IF NOT EXISTS rececao_avulsos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nome        TEXT NOT NULL,
    telefone    TEXT,
    criado_em   TEXT NOT NULL,
    criado_por  TEXT NOT NULL
);

-- Check-in no dia da Feira (7 Out 2026): a receção marca quem chegou e as
-- entrevistas feitas, e pode acertar a empresa e a hora no próprio dia. Uma
-- linha por candidatura validada; as colunas de trabalho das candidaturas e a
-- sincronização não são tocadas.
CREATE TABLE IF NOT EXISTS rececao (
    id_submissao TEXT NOT NULL,
    empresa      TEXT NOT NULL,             -- a empresa da candidatura
    presente     INTEGER NOT NULL DEFAULT 0,
    entrevistado INTEGER NOT NULL DEFAULT 0,
    extra        INTEGER NOT NULL DEFAULT 0, -- entrevista acrescentada no dia, sem candidatura
    faltou       INTEGER NOT NULL DEFAULT 0, -- «Não compareceu», marcado no fecho do dia (13 Out 2026)
    empresa_dia  TEXT,                      -- acerto no dia: entrevistado por outra empresa
    horario_dia  TEXT,                      -- acerto no dia: outra hora (HH:MM)
    alterado_em  TEXT NOT NULL,
    alterado_por TEXT NOT NULL,
    PRIMARY KEY (id_submissao, empresa)
);

-- Fecho do dia na receção (13 Out 2026): quem não estava presente passa a
-- «Não compareceu» e prepara-se um e-mail final a cada técnico de entidade.
-- O conteúdo de cada e-mail fica aqui no momento do fecho (o que a receção
-- confirmou); correio.py fecho envia os que estão por enviar.
CREATE TABLE IF NOT EXISTS rececao_fecho (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    em          TEXT NOT NULL,
    por         TEXT NOT NULL,
    faltas      INTEGER NOT NULL,           -- candidatos marcados «Não compareceu»
    emails      INTEGER NOT NULL            -- e-mails preparados
);
CREATE TABLE IF NOT EXISTS fecho_emails (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fecho       INTEGER NOT NULL REFERENCES rececao_fecho(id),
    ref         TEXT NOT NULL,              -- submissoes.tecnico_id
    email       TEXT NOT NULL,
    nome        TEXT,
    entidade    TEXT,
    dados       TEXT NOT NULL,              -- JSON com o que o e-mail comunica
    enviado_em  TEXT,
    tentativas  INTEGER NOT NULL DEFAULT 0,
    resultado   TEXT
);

-- Avisos de acompanhamento ao técnico da entidade (14 Out 2026, correio.py
-- acompanhamento): a versão de cada linha do seguimento já comunicada. Há
-- alteração por avisar quando os valores da linha diferem dos comunicados.
CREATE TABLE IF NOT EXISTS acompanhamento_avisos (
    id_submissao TEXT NOT NULL,
    empresa      TEXT NOT NULL,
    alterado_em  TEXT NOT NULL,             -- seguimento.alterado_em da versão comunicada
    valores      TEXT NOT NULL,             -- JSON dos campos comunicados
    avisado_em   TEXT NOT NULL,
    PRIMARY KEY (id_submissao, empresa)
);

-- Avaliação da organização da Feira pelos parceiros (19 Out 2026): técnicos de
-- encaminhamento (perfil entidade), de triagem (tecnico) e a coordenação. Uma
-- resposta por pessoa, que pode ser revista: a mesma pessoa tem muitas vezes
-- dois links (triagem e entidade), e a chave é o 1.º e o último nome (chave_pessoa).
-- Escala 1 Muito mau … 5 Muito bom; 0 = Não aplicável / sem conhecimento suficiente.
CREATE TABLE IF NOT EXISTS avaliacao_feira (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    pessoa         TEXT NOT NULL UNIQUE,
    nome           TEXT NOT NULL,
    grupo          TEXT NOT NULL,           -- coordenacao, triagem ou encaminhamento (o papel principal)
    planeamento    INTEGER CHECK (planeamento BETWEEN 0 AND 5),
    comunicacao    INTEGER CHECK (comunicacao BETWEEN 0 AND 5),
    logistica      INTEGER CHECK (logistica BETWEEN 0 AND 5),
    dia_evento     INTEGER CHECK (dia_evento BETWEEN 0 AND 5),
    entidades      INTEGER CHECK (entidades BETWEEN 0 AND 5),
    global         INTEGER CHECK (global BETWEEN 0 AND 5),
    plataforma_uso INTEGER CHECK (plataforma_uso BETWEEN 0 AND 5),
    plataforma_util INTEGER CHECK (plataforma_util BETWEEN 0 AND 5),
    correu_bem     TEXT,
    melhorar       TEXT,
    comentarios    TEXT,
    obs_notas      TEXT,                    -- JSON {pergunta: porquê}, opcional nas notas 1 e 2 (19 Out 2026)
    criada_em      TEXT NOT NULL,
    alterado_em    TEXT NOT NULL,
    alterado_por   TEXT NOT NULL,
    alterado_perfil TEXT NOT NULL
);
"""


# Blocos da avaliação da organização só para a triagem e só para a coordenação
# (19 Out 2026): colunas acrescentadas por ALTER TABLE, vazias em quem respondeu
# antes. 0 = N/A, como nas comuns; tri_comparacao vai de 1 Muito pior a 5 Muito melhor.
COLUNAS_TRIAGEM = ("tri_criterios", "tri_inscricoes", "tri_tempo", "tri_marcacao", "tri_automatico",
                   "tri_comunicacao", "tri_apoio", "tri_comparacao")
COLUNAS_COORDENACAO = ("coord_empresas", "coord_entidades", "coord_recursos", "coord_divulgacao")


def chave_pessoa(nome):
    """1.º e último nome, sem acentos nem maiúsculas: «Ana Margarida Abrantes»
    (link da entidade) e «Ana Abrantes» (link de triagem) são a mesma pessoa."""
    palavras = unicodedata.normalize("NFKD", nome or "").encode("ascii", "ignore").decode().casefold().split()
    return f"{palavras[0]} {palavras[-1]}" if palavras else ""


# Avaliação do evento pelas empresas (13 Out 2026): o questionário de avaliação
# final da qualidade. Várias respostas por empresa (uma por pessoa de RH), que
# podem ser revistas. Escala 1 Insatisfeito … 4 Muito satisfeito.
AVALIACAO = """
CREATE TABLE IF NOT EXISTS {nome} (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    empresa        TEXT NOT NULL,
    atendimento    INTEGER CHECK (atendimento BETWEEN 1 AND 4),
    interesse      INTEGER CHECK (interesse BETWEEN 1 AND 4),
    horario        INTEGER CHECK (horario BETWEEN 1 AND 4),
    duracao        INTEGER CHECK (duracao BETWEEN 1 AND 4),
    espaco         INTEGER CHECK (espaco BETWEEN 1 AND 4),
    equipamento    INTEGER CHECK (equipamento BETWEEN 1 AND 4),
    divulgacao     INTEGER CHECK (divulgacao BETWEEN 1 AND 4),
    encaminhamento INTEGER CHECK (encaminhamento BETWEEN 1 AND 4),
    matching       INTEGER CHECK (matching BETWEEN 1 AND 4),
    plataforma     INTEGER CHECK (plataforma BETWEEN 1 AND 4),  -- o dashboard: agenda e seguimento
    global         INTEGER CHECK (global BETWEEN 1 AND 4),
    positivos      TEXT,
    negativos      TEXT,
    sugestoes      TEXT,
    obs_notas      TEXT,                    -- JSON {{pergunta: motivo}}, obrigatório nas notas abaixo de 3
    respondido_por TEXT NOT NULL,           -- nome de quem responde (substitui a assinatura do papel)
    criada_em      TEXT NOT NULL,
    alterado_em    TEXT NOT NULL,
    alterado_por   TEXT NOT NULL
)"""


def migrar_avaliacao(con):
    """A primeira versão (13 Out 2026) tinha uma resposta por empresa, com a
    empresa como chave. Copia-se para a tabela com id, mantendo as respostas."""
    def cols():
        return [r[1] for r in con.execute("PRAGMA table_info(avaliacao)")]
    if cols() and "id" in cols():
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        antigas = cols()
        if not antigas:
            con.execute(AVALIACAO.format(nome="avaliacao"))
        elif "id" not in antigas:
            con.execute(AVALIACAO.format(nome="avaliacao_nova"))
            comuns = [c for c in antigas if c != "criada_em"]
            lista = ", ".join(comuns)
            con.execute(f"INSERT INTO avaliacao_nova ({lista}, criada_em) SELECT {lista}, alterado_em FROM avaliacao")
            con.execute("DROP TABLE avaliacao")
            con.execute("ALTER TABLE avaliacao_nova RENAME TO avaliacao")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    con.execute("CREATE INDEX IF NOT EXISTS idx_avaliacao_empresa ON avaliacao(empresa)")


def agora_utc(preciso=False):
    """Hora UTC em ISO. `preciso` (microssegundos) para ordenações, como a
    lista de espera, onde duas validações no mesmo segundo não podem empatar."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds" if preciso else "seconds")


def para_local(iso_utc, fmt="%d/%m %H:%M"):
    if not iso_utc:
        return ""
    return datetime.fromisoformat(iso_utc).astimezone(TZ).strftime(fmt)


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ligar(path=None):
    con = sqlite3.connect(path or DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


# Colunas da unificação de entidades e técnicos (6 Out 2026). Quem as preenche é
# a sincronização; o dashboard só as cria, para uma BD que não passa pelo
# exportador novo (a da demonstração) continuar a funcionar, com os nomes
# originais como estão.
COLUNAS_UNIFICACAO = ("entidade_canon", "tecnico_id", "tecnico_canon", "tecnico_email_canon", "tecnico_tel_canon")


def garantir_schema(con):
    migrar_tokens(con)
    con.executescript(SCHEMA)
    if "assinatura" not in {r[1] for r in con.execute("PRAGMA table_info(envios)")}:
        try:
            con.execute("ALTER TABLE envios ADD COLUMN assinatura TEXT")
        except sqlite3.OperationalError:     # o outro worker acabou de a criar
            pass
    migrar_avaliacao(con)
    for tabela, coluna, tipo in (("rececao", "extra", "INTEGER NOT NULL DEFAULT 0"),
                                 ("rececao", "faltou", "INTEGER NOT NULL DEFAULT 0"),
                                 ("seguimento", "alterado_perfil", "TEXT"),
                                 ("avaliacao_feira", "obs_notas", "TEXT"),
                                 *(("avaliacao_feira", c, "INTEGER") for c in COLUNAS_TRIAGEM + COLUNAS_COORDENACAO),
                                 ("avaliacao_feira", "tri_sugestao", "TEXT"),
                                 ("avaliacao_feira", "coord_decisao", "TEXT")):
        if coluna not in {r[1] for r in con.execute(f"PRAGMA table_info({tabela})")}:
            try:
                con.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}")
            except sqlite3.OperationalError:     # o outro worker acabou de a criar
                pass
    def em_falta():
        cols = {r[1] for r in con.execute("PRAGMA table_info(submissoes)")}
        return [c for c in COLUNAS_UNIFICACAO if cols and c not in cols]
    if em_falta():
        con.execute("BEGIN IMMEDIATE")
        try:
            for c in em_falta():
                con.execute(f"ALTER TABLE submissoes ADD COLUMN {c} TEXT")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise


def migrar_tokens(con):
    """Um perfil novo (entidade a 6 Out 2026, rececao a 7 Out) não cabe no
    CHECK da tabela tokens criada antes, e o SQLite não altera um CHECK:
    copia-se a tabela, com as colunas que a antiga tiver. Corre trancada,
    porque os dois workers do gunicorn arrancam ao mesmo tempo."""
    def precisa():
        row = con.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tokens'").fetchone()
        return row is not None and f"'{PERFIS[-1]}'" not in row[0]
    if not precisa():
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        if precisa():
            con.execute(TOKENS.format(nome="tokens_novo"))
            colunas = ", ".join(r[1] for r in con.execute("PRAGMA table_info(tokens)"))
            con.execute(f"INSERT INTO tokens_novo ({colunas}) SELECT {colunas} FROM tokens")
            con.execute("DROP TABLE tokens")
            con.execute("ALTER TABLE tokens_novo RENAME TO tokens")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
