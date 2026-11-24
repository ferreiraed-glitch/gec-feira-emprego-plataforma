#!/usr/bin/env python3
"""E-mails da Feira de Emprego 2026, enviados de feira@exemplo.pt (7 Out 2026).

Todos os envios usam o mesmo template (templates/email/base.html): no topo, o
cabeçalho dos dashboards (símbolo do GEC, «XI Feira de Emprego · Carnide 2026
· 13 Out · Lispolis», azul-escuro e amarelo); na assinatura, os logótipos do
rodapé (GEC, Lisboa REDEmprega, Lisboa BIP/ZIP) e o crédito. As imagens vão
dentro da mensagem (CID), porque muitos clientes de e-mail bloqueiam imagens
externas. Cada tipo de e-mail tem um .html e um .txt que estendem a base.

    correio.py estado [--simular] [--para EMAIL] [--limite N] [--forcar]
        ponto de situação a cada técnico que inscreveu candidatos (cron às 18:00,
        até FE2026_ESTADO_ATE; até 7 Out também às 13:00)
    correio.py candidatos [--simular] [--para EMAIL] [--limite N] [--forcar]
        a cada candidato com candidaturas validadas ou anuladas: o resultado e,
        se tiver, a hora da(s) entrevista(s), com dicas para a entrevista
        (cron às 18:00, desde 8 Out 2026)
    correio.py links-novos [--simular]
        cria e envia o link aos técnicos das entidades que ainda não o têm
        (cron de meia em meia hora, desde 8 Out 2026)
    correio.py fecho [--simular] [--para EMAIL]
        o ponto de situação final a cada técnico de entidade, preparado no fecho
        do dia na receção (13 Out 2026); envia os que estão por enviar (cron)
    correio.py acompanhamento [--simular] [--para EMAIL]
        a cada técnico de entidade, as alterações ao acompanhamento dos seus
        candidatos feitas pelas empresas ou pela coordenação desde o último
        aviso, com CC a FE2026_ACOMPANHAMENTO_CC; as feitas pelos próprios técnicos
        vão só para esse endereço (cron de hora a hora, 14 Out 2026)
    correio.py coordenacao --nome N --email E [--anexo PDF] [--simular] [--para EMAIL] < link
        o link de coordenação a uma pessoa, com o manual em anexo (15 Out 2026;
        o link vem pela entrada padrão)
    correio.py avaliacao [--simular] [--para EMAIL] [--forcar] [--partilhados] [--lembrete] < links (triagem + entidades)
        a cada técnico (triagem e encaminhamento), o aviso do questionário de
        avaliação da organização no seu perfil, com os seus links (19 Out 2026)
    correio.py links [--simular] [--para EMAIL] [--forcar] [--partilhados] < fe2026-links-entidades.txt
        o link pessoal do dashboard a cada técnico de entidade (um e-mail por
        pessoa, só com o seu link; o ficheiro vem pela entrada padrão e não
        fica no CT129). Um e-mail partilhado por vários técnicos fica retido,
        salvo com --partilhados (ex. Associação João 13, autorizado a 7 Out
        2026: os dois técnicos acedem aos mesmos candidatos)

Credenciais em FE2026_MAIL_ENV (/etc/gec-fe2026/mail.env): MAIL_USER,
MAIL_PASS, MAIL_SMTP_HOST, MAIL_SMTP_PORT (SSL) e, para guardar uma cópia em
Enviados, MAIL_IMAP_HOST e MAIL_IMAP_PORT. Cada envio fica na tabela envios
(sem o link nem o conteúdo).

Sem repetições (8 Out 2026): cada e-mail de estado ou de candidato leva uma
assinatura do que comunica (empresas, estados, horas). Se a última mensagem
enviada a essa pessoa tinha a mesma assinatura, não se envia nada; e cada
pessoa recebe no máximo uma por dia. Para não chegar ao limite da Hostinger
(1.000 mensagens por dia desde 8 Out), um envio pára ao fim de
FE2026_MAIL_LIMITE_DIA mensagens nas últimas 24 h, ou numa recusa temporária
(4xx) do servidor, e o resto sai na execução seguinte.
"""
import argparse
import hashlib
import imaplib
import json
import os
import re
import smtplib
import ssl
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import jinja2

import agenda
import bd

AQUI = os.path.dirname(os.path.abspath(__file__))
MAIL_ENV = os.environ.get("FE2026_MAIL_ENV", "/etc/gec-fe2026/mail.env")
BASE_URL = os.environ.get("FE2026_BASE_URL", "https://dashboard.exemplo.org")
REMETENTE_NOME = "Feira de Emprego Carnide 2026"


def _hora(texto):
    return datetime.strptime(texto, "%Y-%m-%d %H:%M").replace(tzinfo=bd.TZ)


# Os envios das 18:00 terminam na véspera da Feira (decisão do utilizador, 7 Out
# 2026); até às 23:59 para as continuações depois de um corte por limite.
ESTADO_ATE = _hora(os.environ.get("FE2026_ESTADO_ATE", "2026-10-12 23:59"))
LINKS_NOVOS_ATE = _hora(os.environ.get("FE2026_LINKS_NOVOS_ATE", "2026-10-13 12:00"))
# Plano da Hostinger desde 8 Out 2026: 1.000 mensagens por dia (antes cortava ao fim de ~100).
LIMITE_DIA = int(os.environ.get("FE2026_MAIL_LIMITE_DIA", "900"))
ARTIGO_ENTREVISTAS = "https://gec-carnide.pt/entrevistas-comunicacao-e-impacto/"
# Logótipos do rodapé dos dashboards: (cid, ficheiro em static/)
IMAGENS = (("gec", "gec-simbolo.png"), ("redemprega", "redemprega.png"), ("bipzip", "lisboa-bipzip.png"))
PAUSA = float(os.environ.get("FE2026_MAIL_PAUSA", "1.5"))   # segundos entre mensagens
EMAIL_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

_env = jinja2.Environment(loader=jinja2.FileSystemLoader(os.path.join(AQUI, "templates", "email")),
                          autoescape=jinja2.select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)


def ler_config(path=MAIL_ENV):
    cfg = {}
    with open(path) as f:
        for linha in f:
            if "=" in linha and not linha.lstrip().startswith("#"):
                k, v = linha.strip().split("=", 1)
                cfg[k] = v
    for k in ("MAIL_USER", "MAIL_PASS", "MAIL_SMTP_HOST", "MAIL_SMTP_PORT"):
        if not cfg.get(k):
            raise SystemExit(f"{path}: falta {k}")
    return cfg


def _chave(s):
    return unicodedata.normalize("NFKD", (s or "").strip()).encode("ascii", "ignore").decode().casefold()


def primeiro_nome(nome):
    return (nome or "").split()[0].capitalize() if (nome or "").split() else ""


# ── Composição ───────────────────────────────────────────────────────────

def compor(remetente, para, assunto, template, nome_para=None, **ctx):
    """EmailMessage com texto simples e HTML (template.txt e template.html),
    com os logótipos embebidos."""
    texto = _env.get_template(f"{template}.txt").render(**ctx)
    html = _env.get_template(f"{template}.html").render(assunto=assunto, **ctx)
    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = Address(REMETENTE_NOME, addr_spec=remetente)
    msg["To"] = Address(nome_para or "", addr_spec=para)
    msg["Reply-To"] = remetente
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=remetente.split("@")[1])
    msg.set_content(texto)
    msg.add_alternative(html, subtype="html")
    parte_html = msg.get_payload()[1]
    for cid, ficheiro in IMAGENS:
        with open(os.path.join(AQUI, "static", ficheiro), "rb") as f:
            parte_html.add_related(f.read(), "image", "png", cid=f"<{cid}@fe2026>", filename=ficheiro)
    return msg


# ── Envio ────────────────────────────────────────────────────────────────

class Carteiro:
    """Uma ligação SMTP para vários envios, com uma nova tentativa se cair;
    guarda uma cópia de cada mensagem na pasta Enviados, se houver IMAP."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.smtp = None
        self.enviados = None

    def _ligar(self):
        self.smtp = smtplib.SMTP_SSL(self.cfg["MAIL_SMTP_HOST"], int(self.cfg["MAIL_SMTP_PORT"]),
                                     context=ssl.create_default_context(), timeout=30)
        self.smtp.login(self.cfg["MAIL_USER"], self.cfg["MAIL_PASS"])

    def enviar(self, msg):
        for tentativa in (1, 2):
            try:
                if self.smtp is None:
                    self._ligar()
                self.smtp.send_message(msg)
                break
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, OSError):
                self.smtp = None
                if tentativa == 2:
                    raise
                time.sleep(3)
        self._copia(msg)

    def _copia(self, msg):
        if not self.cfg.get("MAIL_IMAP_HOST"):
            return
        try:
            with imaplib.IMAP4_SSL(self.cfg["MAIL_IMAP_HOST"], int(self.cfg.get("MAIL_IMAP_PORT", 993)),
                                   ssl_context=ssl.create_default_context()) as imap:
                imap.login(self.cfg["MAIL_USER"], self.cfg["MAIL_PASS"])
                if self.enviados is None:
                    self.enviados = "Sent"
                    for linha in imap.list()[1]:
                        txt = linha.decode()
                        if "\\Sent" in txt:
                            self.enviados = txt.rsplit(" ", 1)[1].strip('"')
                            break
                imap.append(self.enviados, "\\Seen", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        except Exception as e:  # a cópia é conveniência: o envio já foi feito
            print(f"AVISO: cópia em Enviados falhou: {e}", file=sys.stderr)

    def fechar(self):
        if self.smtp is not None:
            try:
                self.smtp.quit()
            except Exception:
                pass


def temporario(e):
    """Recusa temporária do servidor (4xx), como o limite de envio da
    Hostinger (a 7 Out cortou ao fim de ~100 mensagens seguidas)."""
    return isinstance(e, smtplib.SMTPResponseException) and 400 <= e.smtp_code < 500


def registar_envio(con, tipo, destinatario, ref, assunto, linhas, resultado, assinatura=None):
    con.execute("INSERT INTO envios (em, tipo, destinatario, ref, assunto, linhas, resultado, assinatura) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (bd.agora_utc(), tipo, destinatario, ref, assunto, linhas, resultado, assinatura))


def ja_enviado(con, tipo, ref, desde):
    return con.execute("SELECT 1 FROM envios WHERE tipo = ? AND ref = ? AND resultado = 'enviado' AND em >= ?",
                       (tipo, ref, desde)).fetchone() is not None


def ultima_assinatura(con, tipo, ref):
    row = con.execute("SELECT assinatura FROM envios WHERE tipo = ? AND ref = ? AND resultado = 'enviado' "
                      "ORDER BY id DESC LIMIT 1", (tipo, ref)).fetchone()
    return row[0] if row else None


def assinatura_de(dados):
    return hashlib.sha256(json.dumps(dados, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]


def inicio_do_dia():
    """Meia-noite de hoje (Lisboa) em UTC ISO, para «no máximo um por dia»."""
    hoje = datetime.now(bd.TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return hoje.astimezone(timezone.utc).isoformat(timespec="seconds")


def enviados_ultimas_24h(con):
    desde = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    return con.execute("SELECT count(*) FROM envios WHERE resultado = 'enviado' AND em >= ?", (desde,)).fetchone()[0]


def motivo_para_nao_enviar(con, a, tipo, ref, assinatura):
    """Texto a mostrar se esta pessoa não deve receber agora; None se deve."""
    if a.para or a.forcar:
        return None
    if ultima_assinatura(con, tipo, ref) == assinatura:
        return "sem alterações desde o último e-mail"
    if ja_enviado(con, tipo, ref, inicio_do_dia()):
        return "já recebeu hoje"
    return None


class Envio:
    """Um lote de mensagens: regista cada uma, respeita o limite por hora e
    pára numa recusa temporária do servidor."""

    def __init__(self, con, a):
        self.con, self.a = con, a
        self.cfg = None if a.simular else ler_config()
        self.carteiro = None if a.simular else Carteiro(self.cfg)
        self.enviados = self.falhados = self.saltados = 0
        self.parado = None

    @property
    def remetente(self):
        return self.cfg["MAIL_USER"]

    def pode_continuar(self):
        if self.parado:
            return False
        if not self.a.simular and enviados_ultimas_24h(self.con) >= LIMITE_DIA:
            self.parado = f"limite de {LIMITE_DIA} mensagens em 24 h: o resto sai na execução seguinte"
        return not self.parado

    def enviar(self, msg, tipo, ref, assunto, linhas=None, assinatura=None, quem=""):
        para = str(msg["To"].addresses[0].addr_spec)
        try:
            self.carteiro.enviar(msg)
            registar_envio(self.con, tipo, para, ref, assunto, linhas, "enviado", assinatura)
            self.enviados += 1
            return True
        except Exception as e:
            registar_envio(self.con, tipo, para, ref, assunto, linhas, f"erro: {e}"[:300], assinatura)
            print(f"ERRO a enviar a {quem}: {e}", file=sys.stderr)
            self.falhados += 1
            if temporario(e):
                self.parado = "recusa temporária do servidor: o resto sai na execução seguinte"
            return False
        finally:
            time.sleep(PAUSA)

    def fechar(self, rotulo, total):
        if self.carteiro:
            self.carteiro.fechar()
        if self.parado:
            print(f"PARADO: {self.parado}", file=sys.stderr)
        print(f"{datetime.now(bd.TZ):%d/%m %H:%M} {rotulo}: {total}, {self.enviados} enviados, {self.falhados} com erro, "
              f"{self.saltados} saltados{' (simulação)' if self.a.simular else ''}")
        return 1 if self.falhados else 0


# ── Ponto de situação aos técnicos das entidades ─────────────────────────

ESTADO_SQL = f"""
SELECT c.id_submissao, c.empresa, c.validacao, c.horario_entrevista, c.observacoes, e.duracao_entrevista_min,
       s.cand_nome, s.tecnico_id,
       COALESCE(s.tecnico_canon, s.entidade_tecnico) AS tecnico,
       COALESCE(s.tecnico_email_canon, lower(trim(s.entidade_email))) AS email,
       COALESCE(s.entidade_canon, s.entidade_nome) AS entidade
FROM candidaturas c
JOIN submissoes s ON s.id_submissao = c.id_submissao
JOIN empresas e ON e.empresa = c.empresa
WHERE {agenda.ACTIVAS}
"""


def situacao(r, espera):
    """(classe, estado, entrevista) de uma candidatura, como no dashboard."""
    if r["validacao"] == "Anulado":
        return "anulado", "Anulada", "—"
    if r["validacao"] == "Por validar":
        return "pendente", "Por validar", "—"
    if r["horario_entrevista"]:
        fim = agenda.para_hhmm(agenda.para_minutos(r["horario_entrevista"]) + (r["duracao_entrevista_min"] or 0))
        return "marcado", "Validada", f"13 Out, {r['horario_entrevista']}–{fim}"
    pos = espera.get((r["id_submissao"], r["empresa"]))
    return "espera", "Validada", f"Lista de espera{f' (n.º {pos})' if pos else ''}"


def pontos_de_situacao(con):
    """Um grupo por técnico de entidade (tecnico_id unificado), com as
    candidaturas ordenadas por candidato e depois por empresa."""
    espera = agenda.posicoes_em_espera(con)
    grupos = {}
    for r in con.execute(ESTADO_SQL).fetchall():
        ref = r["tecnico_id"] or r["email"]
        if not ref:
            continue
        g = grupos.setdefault(ref, {"ref": ref, "tecnico": r["tecnico"], "email": r["email"],
                                    "entidade": r["entidade"], "linhas": []})
        classe, estado, entrevista = situacao(r, espera)
        g["linhas"].append({"candidato": r["cand_nome"], "empresa": r["empresa"], "classe": classe,
                            "estado": estado, "entrevista": entrevista, "observacoes": r["observacoes"] or ""})
    for g in grupos.values():
        g["linhas"].sort(key=lambda l: (_chave(l["candidato"]), _chave(l["empresa"])))
        conta = lambda c: sum(1 for l in g["linhas"] if l["classe"] == c)  # noqa: E731
        g["resumo"] = {"pendente": conta("pendente"), "marcado": conta("marcado"), "espera": conta("espera"),
                       "anulado": conta("anulado")}
        g["assinatura"] = assinatura_de(g["linhas"])
    return sorted(grupos.values(), key=lambda g: (_chave(g["entidade"]), _chave(g["tecnico"])))


def cmd_estado(a):
    agora = datetime.now(timezone.utc).astimezone(bd.TZ)
    if agora > ESTADO_ATE and not a.para:
        print(f"{agora:%d/%m %H:%M}: fora do período dos pontos de situação (até {ESTADO_ATE:%d/%m %H:%M}).")
        return 0
    con = bd.ligar()
    bd.garantir_schema(con)
    grupos = pontos_de_situacao(con)
    assunto = f"XI Feira de Emprego · ponto de situação dos seus candidatos · {agora:%d/%m} às {agora:%H:%M}"
    tipo = "estado-teste" if a.para else "estado"
    lote = Envio(con, a)
    try:
        for g in grupos[:a.limite] if a.limite else grupos:
            n = len(g["linhas"])
            if not g["email"] or not EMAIL_OK.match(g["email"]):
                print(f"SEM E-MAIL VÁLIDO: {g['tecnico']} ({g['entidade']}), {n} candidaturas")
                lote.saltados += 1
                continue
            motivo = motivo_para_nao_enviar(con, a, tipo, g["ref"], g["assinatura"])
            if motivo:
                lote.saltados += 1
                continue
            if not lote.pode_continuar():
                break
            if a.simular:
                print(f"{g['tecnico']} <{g['email']}> · {g['entidade']} · {n} candidaturas · {g['resumo']}")
                continue
            msg = compor(lote.remetente, a.para or g["email"], ("[TESTE] " if a.para else "") + assunto, "estado",
                         nome_para=g["tecnico"], g=g, nome=primeiro_nome(g["tecnico"]), data=agora,
                         ate=ESTADO_ATE, base_url=BASE_URL)
            lote.enviar(msg, tipo, g["ref"], assunto, n, g["assinatura"], g["tecnico"])
    finally:
        rc = lote.fechar("estado", f"{len(grupos)} técnicos")
    return rc


# ── E-mail a cada candidato (8 Out 2026) ─────────────────────────────────

CANDIDATO_SQL = f"""
SELECT c.id_submissao, c.empresa, c.validacao, c.horario_entrevista, c.ofertas_seleccionadas,
       e.duracao_entrevista_min, s.cand_nome, s.cand_email, s.criada_em, s.duplicado_de,
       COALESCE(s.tecnico_canon, s.entidade_tecnico) AS tecnico,
       COALESCE(s.entidade_canon, s.entidade_nome) AS entidade,
       COALESCE(s.tecnico_email_canon, s.entidade_email) AS tecnico_email
FROM candidaturas c
JOIN submissoes s ON s.id_submissao = c.id_submissao
JOIN empresas e ON e.empresa = c.empresa
WHERE {agenda.ACTIVAS}
"""


ALIASES = os.path.join(os.path.dirname(AQUI), "entidades-aliases.json")


def correcoes_email(path=ALIASES):
    """{e-mail errado: e-mail certo} de `entidades-aliases.json`: valem para técnicos e candidatos (8 Out 2026)."""
    try:
        with open(path, encoding="utf-8") as f:
            dados = json.load(f).get("emails", {})
    except FileNotFoundError:
        return {}
    return {k.strip().lower(): v.strip().lower() for k, v in dados.items() if v.strip()}


def candidatos_a_contactar(con):
    """Uma entrada por pessoa (as inscrições com o mesmo NIF ou documento
    juntam-se, como na receção), só com as candidaturas validadas ou anuladas.
    Os dados de contacto são os da inscrição mais recente."""
    rows = con.execute(CANDIDATO_SQL).fetchall()
    pai = {}

    def raiz(x):
        pai.setdefault(x, x)
        while pai[x] != x:
            x = pai[x]
        return x
    for r in rows:
        raiz(r["id_submissao"])
        for outra in (r["duplicado_de"] or "").split(","):
            if outra.strip():
                ra, rb = raiz(r["id_submissao"]), raiz(outra.strip())
                if ra != rb:
                    pai[max(ra, rb)] = min(ra, rb)
    correcoes = correcoes_email()
    pessoas = {}
    for r in sorted(rows, key=lambda r: (r["criada_em"], r["id_submissao"])):
        pessoas.setdefault(raiz(r["id_submissao"]), []).append(r)
    resultado = []
    for linhas in pessoas.values():
        decididas = [r for r in linhas if r["validacao"] in ("Validado", "Anulado")]
        if not decididas:
            continue
        recente = linhas[-1]
        itens = []
        for r in decididas:
            if r["validacao"] == "Anulado":
                classe = "anulado"
            else:
                classe = "marcado" if r["horario_entrevista"] else "espera"
            fim = (agenda.para_hhmm(agenda.para_minutos(r["horario_entrevista"]) + (r["duracao_entrevista_min"] or 0))
                   if r["horario_entrevista"] else None)
            itens.append({"empresa": r["empresa"], "classe": classe, "hora": r["horario_entrevista"], "fim": fim,
                          "ofertas": (r["ofertas_seleccionadas"] or "").replace(" | ", ", ")})
        ordem = {"marcado": 0, "espera": 1, "anulado": 2}
        itens.sort(key=lambda i: (ordem[i["classe"]], i["hora"] or "", _chave(i["empresa"])))
        # A assinatura é o que se comunica: empresa, resultado e hora (não a posição na lista de espera).
        assinatura = assinatura_de(sorted([i["empresa"], i["classe"], i["hora"] or ""] for i in itens))
        original = next((e for e in (str(r["cand_email"] or "").strip().lower() for r in reversed(linhas))
                         if EMAIL_OK.match(e)), "")
        email = correcoes.get(original, original)
        resultado.append({"ref": linhas[0]["id_submissao"], "nome": recente["cand_nome"], "email": email,
                          "email_original": original, "tecnico_email": (recente["tecnico_email"] or "").strip().lower(),
                          "tecnico": recente["tecnico"], "entidade": recente["entidade"], "itens": itens,
                          "assinatura": assinatura,
                          "entrevistas": [i for i in itens if i["classe"] == "marcado"],
                          "espera": [i for i in itens if i["classe"] == "espera"],
                          "anuladas": [i for i in itens if i["classe"] == "anulado"]})
    return sorted(resultado, key=lambda p: _chave(p["nome"]))


def assunto_candidato(p):
    if p["entrevistas"]:
        return "XI Feira de Emprego de Carnide · a sua entrevista a 13 de Outubro"
    if p["espera"]:
        return "XI Feira de Emprego de Carnide · a sua candidatura está em lista de espera"
    return "XI Feira de Emprego de Carnide · resultado da sua candidatura"


def cmd_candidatos(a):
    agora = datetime.now(bd.TZ)
    if agora > ESTADO_ATE and not a.para:
        print(f"{agora:%d/%m %H:%M}: fora do período dos e-mails aos candidatos (até {ESTADO_ATE:%d/%m %H:%M}).")
        return 0
    con = bd.ligar()
    bd.garantir_schema(con)
    pessoas = candidatos_a_contactar(con)
    if a.so:
        so = {e.strip().lower() for e in a.so}
        pessoas = [p for p in pessoas if p["email"] in so or p["email_original"] in so]
    tipo = "candidato-teste" if a.para else "candidato"
    lote = Envio(con, a)
    try:
        for p in pessoas[:a.limite] if a.limite else pessoas:
            if not p["email"]:
                print(f"SEM E-MAIL VÁLIDO: {p['nome']} ({p['entidade']})")
                lote.saltados += 1
                continue
            if motivo_para_nao_enviar(con, a, tipo, p["ref"], p["assinatura"]):
                lote.saltados += 1
                continue
            if not lote.pode_continuar():
                break
            assunto = assunto_candidato(p)
            if a.simular:
                print(f"{p['nome']} <{p['email']}> · {len(p['entrevistas'])} entrevista(s), {len(p['espera'])} em espera, "
                      f"{len(p['anuladas'])} anulada(s)")
                continue
            msg = compor(lote.remetente, a.para or p["email"], ("[TESTE] " if a.para else "") + assunto, "candidato",
                         nome_para=p["nome"], p=p, nome=primeiro_nome(p["nome"]), artigo=ARTIGO_ENTREVISTAS)
            lote.enviar(msg, tipo, p["ref"], assunto, len(p["itens"]), p["assinatura"], p["nome"])
    finally:
        rc = lote.fechar("candidatos", f"{len(pessoas)} candidatos")
    return rc


# ── Links pessoais dos técnicos das entidades ─────────────────────────────

def ler_links(fluxo):
    """Linhas do ficheiro de criar-entidades: nome, entidade, e-mail, telefone,
    inscrições, link (separados por tabulações)."""
    linhas = []
    for n, linha in enumerate(fluxo, 1):
        campos = linha.rstrip("\n").split("\t")
        if len(campos) != 6 or linha.startswith("#"):
            continue
        nome, entidade, email, _tel, inscricoes, link = (c.strip() for c in campos)
        linhas.append({"n": n, "nome": nome, "entidade": entidade, "email": email.lower(),
                       "inscricoes": inscricoes, "link": link})
    return linhas


VALIDADE_LINKS = "11 de Janeiro de 2027"
ASSUNTO_LINK = "XI Feira de Emprego · o seu acesso ao estado dos candidatos que inscreveu"


def cmd_links(a):
    linhas = ler_links(sys.stdin)
    por_email = {}
    for l in linhas:
        por_email.setdefault(l["email"], []).append(l)
    con = bd.ligar()
    bd.garantir_schema(con)
    tipo = "link-teste" if a.para else "link"
    lote = Envio(con, a)
    try:
        for l in linhas[:a.limite] if a.limite else linhas:
            quem = f"linha {l['n']}: {l['nome']} ({l['entidade']})"
            if not EMAIL_OK.match(l["email"]):
                print(f"RETIDO {quem}: e-mail inválido")
                lote.saltados += 1
                continue
            if not l["link"].startswith(f"{BASE_URL}/?t=") or len(l["link"]) < len(BASE_URL) + 20:
                print(f"RETIDO {quem}: link com formato inesperado")
                lote.saltados += 1
                continue
            partilhado = len(por_email[l["email"]]) > 1
            if partilhado and not a.partilhados:
                # Cada pessoa só pode receber o seu link: numa caixa partilhada,
                # o colega veria o link do outro.
                print(f"RETIDO {quem}: e-mail partilhado com "
                      + ", ".join(o["nome"] for o in por_email[l["email"]] if o is not l))
                lote.saltados += 1
                continue
            # A referência é e-mail e nome: numa caixa partilhada, o envio a um técnico
            # não conta como envio ao outro. As linhas de 7 Out antes desta correcção
            # têm só o e-mail, e valem para quem não partilha a caixa.
            ref = f"{l['email']}|{l['nome']}"
            if not a.para and not a.forcar and (ja_enviado(con, tipo, ref, "0")
                                                or (not partilhado and ja_enviado(con, tipo, l["email"], "0"))):
                print(f"já enviado: {quem}")
                lote.saltados += 1
                continue
            if not lote.pode_continuar():
                break
            if a.simular:
                print(f"enviaria a {l['nome']} <{l['email']}> · {l['entidade']} · {l['inscricoes']} inscrições")
                continue
            msg = compor(lote.remetente, a.para or l["email"], ("[TESTE] " if a.para else "") + ASSUNTO_LINK, "link",
                         nome_para=l["nome"], nome=primeiro_nome(l["nome"]), l=l, validade=VALIDADE_LINKS,
                         ate=ESTADO_ATE if datetime.now(bd.TZ) <= ESTADO_ATE else None)
            lote.enviar(msg, tipo, ref, ASSUNTO_LINK, quem=quem)
    finally:
        rc = lote.fechar("links", f"{len(linhas)} linhas")
    return rc


def cmd_links_novos(a):
    """Técnicos das entidades sem link activo (inscreveram-se depois de criados
    os links, ou o link foi revogado): cria o link e envia-o (8 Out 2026). O
    link só existe no e-mail e na cópia em Enviados. Se o envio falhar, o link
    é revogado e volta a ser tentado na execução seguinte."""
    import tokens
    agora = datetime.now(bd.TZ)
    if agora > LINKS_NOVOS_ATE:
        print(f"{agora:%d/%m %H:%M}: fora do período dos links novos (até {LINKS_NOVOS_ATE:%d/%m %H:%M}).")
        return 0
    con = bd.ligar()
    bd.garantir_schema(con)
    novos = [t for t in tokens.tecnicos_de_entidades(con) if not t["tem_link"]]
    lote = Envio(con, a)
    try:
        for t in novos:
            nome, email = t["tecnico_canon"] or "", (t["tecnico_email_canon"] or "").lower()
            quem = f"{nome} ({t['entidade_canon']})"
            if not EMAIL_OK.match(email):
                print(f"SEM E-MAIL VÁLIDO: {quem}")
                lote.saltados += 1
                continue
            if not lote.pode_continuar():
                break
            if a.simular:
                print(f"criaria e enviaria o link a {nome} <{email}> · {t['entidade_canon']} · {t['n']} inscrições")
                continue
            token = tokens.criar(con, "entidade", nome or "?", ref_submissao=t["tecnico_id"], email=email)
            l = {"nome": nome, "entidade": t["entidade_canon"], "email": email, "link": f"{BASE_URL}/?t={token}"}
            msg = compor(lote.remetente, email, ASSUNTO_LINK, "link", nome_para=nome, nome=primeiro_nome(nome), l=l,
                         validade=VALIDADE_LINKS, ate=ESTADO_ATE)
            if not lote.enviar(msg, "link", f"{email}|{nome}", ASSUNTO_LINK, quem=quem):
                con.execute("UPDATE tokens SET revogado = 1 WHERE token_hash = ?", (bd.hash_token(token),))
            else:
                print(f"link criado e enviado: {quem}")
    finally:
        rc = lote.fechar("links novos", f"{len(novos)} técnicos sem link")
    return rc


# ── Ponto de situação final, depois da Feira (13 Out 2026) ───────────────

ASSUNTO_FECHO = "XI Feira de Emprego · ponto de situação final dos seus candidatos"
# Os links das entidades passam a valer até ao fim do seguimento (fe2026-tokens prolongar)
VALIDADE_SEGUIMENTO = "11 de Janeiro de 2027"
TENTATIVAS_FECHO = 3     # depois disto, um e-mail que falha fica de lado (aparece na página do fecho)


def cmd_fecho(a):
    """Envia os e-mails preparados no fecho do dia da receção (tabela fecho_emails).
    Corre pelo cron: sem nada por enviar, não faz nada. Com --para, envia tudo
    para esse endereço com [TESTE] e não marca nada como enviado."""
    con = bd.ligar()
    bd.garantir_schema(con)
    pendentes = con.execute("SELECT * FROM fecho_emails WHERE enviado_em IS NULL AND tentativas < ? ORDER BY id",
                            (TENTATIVAS_FECHO,)).fetchall()
    if a.para:
        pendentes = con.execute("SELECT * FROM fecho_emails ORDER BY id").fetchall()
    if not pendentes:
        return 0
    tipo = "fecho-teste" if a.para else "fecho"
    lote = Envio(con, a)
    try:
        for row in pendentes[:a.limite] if a.limite else pendentes:
            g = json.loads(row["dados"])
            quem = f"{row['nome']} ({row['entidade']})"
            if not lote.pode_continuar():
                break
            if a.simular:
                print(f"enviaria a {quem} <{row['email']}> · {g['resumo']}")
                continue
            msg = compor(lote.remetente, a.para or row["email"], ("[TESTE] " if a.para else "") + ASSUNTO_FECHO, "fecho",
                         nome_para=row["nome"], g=g, nome=primeiro_nome(row["nome"]), validade=VALIDADE_SEGUIMENTO,
                         base_url=BASE_URL)
            n = len(g["presentes"]) + len(g["faltaram"])
            ok = lote.enviar(msg, tipo, row["ref"], ASSUNTO_FECHO, n, quem=quem)
            if a.para:
                continue
            ultimo = con.execute("SELECT resultado FROM envios ORDER BY id DESC LIMIT 1").fetchone()[0]
            con.execute("UPDATE fecho_emails SET enviado_em = ?, tentativas = tentativas + 1, resultado = ? WHERE id = ?",
                        (bd.agora_utc() if ok else None, ultimo, row["id"]))
    finally:
        rc = lote.fechar("fecho", f"{len(pendentes)} por enviar")
    return rc


# ── Avisos de acompanhamento (14 Out 2026) ───────────────────────────────
# De hora a hora (cron): cada técnico de entidade recebe as linhas do acompanhamento
# dos seus candidatos que as empresas ou a coordenação alteraram desde o último
# aviso, com a técnica que acompanha a fase pós-Feira (Ana Coordenadora) em CC; o
# que os técnicos alteram vai, num só e-mail, só para ela. Cada versão de uma
# linha é avisada uma vez: um candidato só volta a um e-mail se a sua linha mudou
# desde o último envio. Só depois do fecho do dia, que apresenta o Acompanhamento.

ASSUNTO_ACOMPANHAMENTO = "XI Feira de Emprego · actualização do acompanhamento dos seus candidatos"
ASSUNTO_ACOMPANHAMENTO_TECNICOS = "XI Feira de Emprego · acompanhamento actualizado pelos técnicos das entidades"
ACOMPANHAMENTO_CC = os.environ.get("FE2026_ACOMPANHAMENTO_CC", "coordenacao@exemplo.pt")
ACOMPANHAMENTO_CC_NOME = os.environ.get("FE2026_ACOMPANHAMENTO_CC_NOME", "Ana Coordenadora")
ACOMPANHAMENTO_ATE = _hora(os.environ.get("FE2026_ACOMPANHAMENTO_ATE", "2027-01-11 23:59"))
CAMPOS_ACOMPANHAMENTO = (("nova_entrevista", "Fez nova entrevista?"), ("selecionado", "Foi selecionado?"),
                         ("contratado", "Ficou contratado?"), ("tipo_contrato", "Tipo de contrato"),
                         ("duracao_contrato", "Duração do contrato"), ("observacoes", "Observações"))
QUEM_ACOMPANHAMENTO = {"empresa": "pela empresa", "coordenador": "pela coordenação"}

ACOMPANHAMENTO_SQL = """
SELECT g.*, s.cand_nome, s.tecnico_id,
       COALESCE(s.tecnico_canon, s.entidade_tecnico) AS tecnico,
       COALESCE(s.tecnico_email_canon, lower(trim(s.entidade_email))) AS email,
       COALESCE(s.entidade_canon, s.entidade_nome) AS entidade,
       v.valores AS valores_avisados, v.alterado_em AS avisado_alterado_em
FROM seguimento g
JOIN submissoes s ON s.id_submissao = g.id_submissao
LEFT JOIN acompanhamento_avisos v ON v.id_submissao = g.id_submissao AND v.empresa = g.empresa
WHERE s.removida = 0
"""
# Compara-se a hora e os valores: alterado_em vai ao segundo, e duas gravações no
# mesmo segundo (a empresa e logo o técnico) ficariam com a mesma hora.


def acompanhamento_por_avisar(con):
    """(grupos, dos_tecnicos, iguais).
    - grupos: um por técnico de entidade, com as linhas que a empresa ou a
      coordenação alteraram;
    - dos_tecnicos: o grupo para a Ana Coordenadora com as linhas que os técnicos
      alteraram (None se não houver);
    - iguais: linhas gravadas de novo com os valores já comunicados (alterado e
      reposto), para marcar sem enviar.
    Em cada campo, se mudou desde o último aviso (ou, no 1.º, se está preenchido)."""
    correcoes = correcoes_email()
    grupos, iguais = {}, []
    tecnicos = {"ref": f"coordenacao|{ACOMPANHAMENTO_CC}", "tecnico": ACOMPANHAMENTO_CC_NOME,
                "email": ACOMPANHAMENTO_CC.lower(), "entidade": "", "dos_tecnicos": True, "linhas": []}
    for r in con.execute(ACOMPANHAMENTO_SQL).fetchall():
        ref = r["tecnico_id"] or r["email"]
        if not ref:
            continue
        antes = json.loads(r["valores_avisados"]) if r["valores_avisados"] else {}
        valores = {k: r[k] for k, _ in CAMPOS_ACOMPANHAMENTO}
        mudou = {k: (valores[k] or None) != (antes.get(k) or None) for k, _ in CAMPOS_ACOMPANHAMENTO}
        linha = {"sid": r["id_submissao"], "empresa": r["empresa"], "alterado_em": r["alterado_em"], "valores": valores}
        if antes and not any(mudou.values()):
            if r["avisado_alterado_em"] != r["alterado_em"]:
                iguais.append(linha)
            continue
        quando = datetime.fromisoformat(r["alterado_em"]).astimezone(bd.TZ)
        linha.update(candidato=r["cand_nome"], tecnico=r["tecnico"] or "", entidade=r["entidade"] or "",
                     quando=f"{quando:%d/%m/%Y, %H:%M}",
                     campos=[{"rotulo": rot, "valor": valores[k] or "—", "mudou": mudou[k]}
                             for k, rot in CAMPOS_ACOMPANHAMENTO])
        if r["alterado_perfil"] == "entidade":
            linha["quem"] = f"pelo/a técnico/a {linha['tecnico']}" + (f" ({linha['entidade']})" if linha["entidade"] else "")
            tecnicos["linhas"].append(linha)
            continue
        linha["quem"] = QUEM_ACOMPANHAMENTO.get(r["alterado_perfil"] or "", "")
        email = (r["email"] or "").strip().lower()
        gr = grupos.setdefault(ref, {"ref": ref, "tecnico": r["tecnico"] or "", "email": correcoes.get(email, email),
                                     "entidade": r["entidade"] or "", "linhas": []})
        gr["linhas"].append(linha)
    for gr in grupos.values():
        gr["linhas"].sort(key=lambda l: (_chave(l["candidato"]), l["sid"], _chave(l["empresa"])))
    tecnicos["linhas"].sort(key=lambda l: (_chave(l["entidade"]), _chave(l["tecnico"]), _chave(l["candidato"]),
                                           l["sid"], _chave(l["empresa"])))
    return (sorted(grupos.values(), key=lambda gr: (_chave(gr["entidade"]), _chave(gr["tecnico"]))),
            tecnicos if tecnicos["linhas"] else None, iguais)


def marcar_avisadas(con, linhas):
    for l in linhas:
        con.execute("INSERT INTO acompanhamento_avisos (id_submissao, empresa, alterado_em, valores, avisado_em) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(id_submissao, empresa) DO UPDATE SET "
                    "alterado_em = excluded.alterado_em, valores = excluded.valores, avisado_em = excluded.avisado_em",
                    (l["sid"], l["empresa"], l["alterado_em"], json.dumps(l["valores"], ensure_ascii=False),
                     bd.agora_utc()))


def cmd_acompanhamento(a):
    """Corre pelo cron de hora a hora: sem alterações por avisar, não faz nada. Com
    --para, envia tudo para esse endereço com [TESTE], sem CC, e não marca nada."""
    agora = datetime.now(timezone.utc).astimezone(bd.TZ)
    if agora > ACOMPANHAMENTO_ATE and not a.para:
        return 0
    con = bd.ligar()
    bd.garantir_schema(con)
    if con.execute("SELECT 1 FROM rececao_fecho").fetchone() is None and not a.para:
        return 0
    grupos, dos_tecnicos, iguais = acompanhamento_por_avisar(con)
    if iguais and not a.para and not a.simular:
        marcar_avisadas(con, iguais)
    # (grupo, assunto, cc): aos técnicos com CC à Ana; o dos técnicos só a ela
    envios = [(gr, ASSUNTO_ACOMPANHAMENTO, ACOMPANHAMENTO_CC) for gr in grupos]
    if dos_tecnicos and ACOMPANHAMENTO_CC:
        envios.append((dos_tecnicos, ASSUNTO_ACOMPANHAMENTO_TECNICOS, None))
    if not envios:
        return 0
    tipo = "acompanhamento-teste" if a.para else "acompanhamento"
    lote = Envio(con, a)
    try:
        for gr, assunto, cc in envios[:a.limite] if a.limite else envios:
            n = len(gr["linhas"])
            quem = f"{gr['tecnico']} ({gr['entidade']})" if gr["entidade"] else gr["tecnico"]
            if not EMAIL_OK.match(gr["email"]):
                print(f"SEM E-MAIL VÁLIDO: {quem}, {n} alterações")
                lote.saltados += 1
                continue
            if not lote.pode_continuar():
                break
            if a.simular:
                print(f"avisaria {quem} <{gr['email']}>{f' (CC {cc})' if cc else ''} · "
                      + "; ".join(f"{l['candidato']} / {l['empresa']}" for l in gr["linhas"]))
                continue
            msg = compor(lote.remetente, a.para or gr["email"], ("[TESTE] " if a.para else "") + assunto,
                         "acompanhamento", nome_para=gr["tecnico"], g=gr, nome=primeiro_nome(gr["tecnico"]),
                         validade=VALIDADE_SEGUIMENTO, contacto=ACOMPANHAMENTO_CC_NOME)
            if cc and not a.para and cc.lower() != gr["email"]:
                msg["Cc"] = cc
            ok = lote.enviar(msg, tipo, gr["ref"], assunto, n, quem=quem)
            if ok and not a.para:
                marcar_avisadas(con, gr["linhas"])
    finally:
        rc = lote.fechar("acompanhamento", f"{len(envios)} e-mails")
    return rc


FECHO_INSCRICOES = "9 de Outubro, às 13:00"
ASSUNTO_TRIAGEM = "XI Feira de Emprego · nota sobre o fecho das inscrições"


def triagem_por_fazer(con, id_tecnico, nome):
    """(n.º por validar, empresas) do técnico validador; None se já tratou alguma candidatura."""
    tratadas = con.execute("SELECT count(*) FROM candidaturas WHERE validacao_alterada_por = ?", (nome,)).fetchone()[0]
    if tratadas:
        return None
    rows = con.execute("SELECT empresa, count(*) AS n FROM candidaturas WHERE id_tecnico = ? "
                       "AND validacao = 'Por validar' GROUP BY empresa ORDER BY empresa", (id_tecnico,)).fetchall()
    return sum(r["n"] for r in rows), [r["empresa"] for r in rows]


def cmd_triagem(a):
    """Lembrete aos técnicos validadores que ainda não trataram nenhuma candidatura (8 Out 2026):
    as inscrições fecham amanhã, quantas têm por triar e o seu link. Lê do stdin linhas
    «nome<TAB>e-mail<TAB>link»; o link tem de ser o do token de técnico com esse nome."""
    con = bd.ligar()
    bd.garantir_schema(con)
    linhas = [l.rstrip("\n").split("\t") for l in sys.stdin if l.strip() and not l.startswith("#")]
    tipo = "triagem-teste" if a.para else "triagem"
    lote = Envio(con, a)
    try:
        for campos in linhas:
            if len(campos) != 3:
                print("RETIDA: linha sem nome, e-mail e link")
                lote.saltados += 1
                continue
            nome, email, link = (c.strip() for c in campos)
            email = email.lower()
            token = link.split("?t=", 1)[-1]
            t = con.execute("SELECT nome, id_tecnico FROM tokens WHERE perfil = 'tecnico' AND revogado = 0 "
                            "AND token_hash = ?", (bd.hash_token(token),)).fetchone()
            if not t or t["nome"] != nome or not link.startswith(f"{BASE_URL}/?t="):
                print(f"RETIDO {nome}: o link não é o do técnico validador com este nome")
                lote.saltados += 1
                continue
            if not EMAIL_OK.match(email):
                print(f"RETIDO {nome}: e-mail inválido")
                lote.saltados += 1
                continue
            por_fazer = triagem_por_fazer(con, t["id_tecnico"], nome)
            if por_fazer is None or not por_fazer[0]:
                print(f"não precisa: {nome} ({'já tratou candidaturas' if por_fazer is None else 'nada por validar'})")
                lote.saltados += 1
                continue
            n, empresas = por_fazer
            ref = f"{email}|{nome}"
            if not a.para and not a.forcar and ja_enviado(con, tipo, ref, "0"):
                print(f"já enviado: {nome}")
                lote.saltados += 1
                continue
            if not lote.pode_continuar():
                break
            empresa = ", ".join(empresas)
            if a.simular:
                print(f"enviaria a {nome} <{email}> · {n} por validar · {empresa}")
                continue
            msg = compor(lote.remetente, a.para or email, ("[TESTE] " if a.para else "") + ASSUNTO_TRIAGEM, "triagem",
                         nome_para=nome, nome=primeiro_nome(nome), n=n, empresa=empresa, link=link,
                         fecho=FECHO_INSCRICOES)
            lote.enviar(msg, tipo, ref, ASSUNTO_TRIAGEM, linhas=n, quem=nome)
    finally:
        rc = lote.fechar("triagem", f"{len(linhas)} técnicos")
    return rc


ASSUNTO_DEVOLVIDOS = "XI Feira de Emprego · e-mail de candidato devolvido"


def cmd_devolvidos(a):
    """E-mails a candidatos que voltaram devolvidos (8 Out 2026): avisa o técnico que os inscreveu, com o
    endereço que falhou e o resultado, para lhes dar a informação e confirmar o e-mail. Lê do stdin os
    endereços devolvidos (o original ou o corrigido). Um e-mail por técnico; não repete o mesmo aviso."""
    devolvidos = {l.strip().lower() for l in sys.stdin if l.strip() and not l.startswith("#")}
    con = bd.ligar()
    bd.garantir_schema(con)
    por_tecnico = {}
    for p in candidatos_a_contactar(con):
        falhados = sorted({e for e in (p["email"], p["email_original"]) if e in devolvidos})
        if falhados:
            p["falhados"] = falhados
            por_tecnico.setdefault((p["tecnico_email"], p["tecnico"] or "", p["entidade"] or ""), []).append(p)
    tipo = "devolvido-teste" if a.para else "devolvido"
    lote = Envio(con, a)
    try:
        for (email, nome, entidade), pessoas in sorted(por_tecnico.items(), key=lambda x: _chave(x[0][1])):
            quem = f"{nome} ({entidade}): {', '.join(p['nome'] for p in pessoas)}"
            if not EMAIL_OK.match(email):
                print(f"SEM E-MAIL VÁLIDO DO TÉCNICO: {quem}")
                lote.saltados += 1
                continue
            ref = f"{email}|" + ",".join(sorted(p["ref"] for p in pessoas))
            if not a.para and not a.forcar and ja_enviado(con, tipo, ref, "0"):
                print(f"já avisado: {quem}")
                lote.saltados += 1
                continue
            if not lote.pode_continuar():
                break
            if a.simular:
                print(f"avisaria {nome} <{email}> · {', '.join(p['nome'] + ' ' + '/'.join(p['falhados']) for p in pessoas)}")
                continue
            msg = compor(lote.remetente, a.para or email, ("[TESTE] " if a.para else "") + ASSUNTO_DEVOLVIDOS,
                         "devolvido", nome_para=nome, nome=primeiro_nome(nome), pessoas=pessoas)
            lote.enviar(msg, tipo, ref, ASSUNTO_DEVOLVIDOS, len(pessoas), quem=quem)
    finally:
        rc = lote.fechar("devolvidos", f"{len(por_tecnico)} técnicos")
    return rc


ASSUNTO_COORDENACAO = "XI Feira de Emprego · o seu acesso de coordenação ao dashboard"


def cmd_coordenacao(a):
    """Envia o link de coordenação a uma pessoa, com o manual em anexo. O link vem
    pela entrada padrão (uma linha) e não fica guardado."""
    link = sys.stdin.readline().strip()
    if not link.startswith(f"{BASE_URL}/?t=") or len(link) < len(BASE_URL) + 20:
        print("link com formato inesperado")
        return 1
    if not a.email or not EMAIL_OK.match(a.email):
        print("e-mail inválido ou em falta (--email)")
        return 1
    anexo = None
    if a.anexo:
        with open(a.anexo, "rb") as f:
            anexo = f.read()
    con = bd.ligar()
    bd.garantir_schema(con)
    tipo = "coordenacao-teste" if a.para else "coordenacao"
    ref = a.email.lower()
    lote = Envio(con, a)
    try:
        if not a.para and not a.forcar and ja_enviado(con, tipo, ref, "0"):
            print(f"já enviado: {a.email}")
            lote.saltados += 1
        elif a.simular:
            print(f"enviaria a {a.nome or ''} <{a.email}>{' com anexo ' + os.path.basename(a.anexo) if a.anexo else ''}")
        else:
            msg = compor(lote.remetente, a.para or a.email, ("[TESTE] " if a.para else "") + ASSUNTO_COORDENACAO,
                         "coordenacao", nome_para=a.nome, nome=primeiro_nome(a.nome), link=link,
                         validade=VALIDADE_SEGUIMENTO)
            if anexo:
                msg.add_attachment(anexo, maintype="application", subtype="pdf", filename=os.path.basename(a.anexo))
            lote.enviar(msg, tipo, ref, ASSUNTO_COORDENACAO, quem=a.email)
    finally:
        rc = lote.fechar("coordenacao", a.email)
    return rc


# ── Avaliação da organização pelos técnicos (19 Out 2026) ────────────────

ASSUNTO_AVALIACAO = "XI Feira de Emprego · avaliação da organização da Feira"
ASSUNTO_AVALIACAO_LEMBRETE = "XI Feira de Emprego · lembrete: a sua avaliação da Feira"
LINK_RE = re.compile(re.escape(BASE_URL) + r"/\?t=([A-Za-z0-9_-]{20,})")


def ler_entrada_avaliacao(fluxo):
    """Da entrada padrão: os links (de qualquer formato de linha, como os dos
    ficheiros dos técnicos de triagem e das entidades), por hash do token, e
    e-mails acrescentados à mão em linhas «nome<TAB>e-mail», por pessoa."""
    links, emails = {}, {}
    for linha in fluxo:
        if linha.startswith("#"):
            continue
        for m in LINK_RE.finditer(linha):
            links[bd.hash_token(m.group(1))] = m.group(0)
        campos = [c.strip() for c in linha.rstrip("\n").split("\t")]
        if len(campos) == 2 and EMAIL_OK.match(campos[1]):
            emails[bd.chave_pessoa(campos[0])] = campos[1].lower()
    return links, emails


def links_em_enviados(cfg, pedidos):
    """Recupera da pasta Enviados os links que só existem no e-mail (os dos
    técnicos que chegaram depois de 6 Out, criados por links-novos). pedidos:
    {e-mail: hashes dos tokens dessa pessoa}. Devolve {hash: link}; só aceita
    um link cujo hash seja de um dos tokens pedidos."""
    import email as email_mod
    from email import policy
    achados = {}
    if not pedidos or not cfg.get("MAIL_IMAP_HOST"):
        return achados
    with imaplib.IMAP4_SSL(cfg["MAIL_IMAP_HOST"], int(cfg.get("MAIL_IMAP_PORT", 993)),
                           ssl_context=ssl.create_default_context()) as imap:
        imap.login(cfg["MAIL_USER"], cfg["MAIL_PASS"])
        pasta = "Sent"
        for linha in imap.list()[1]:
            if "\\Sent" in linha.decode():
                pasta = linha.decode().rsplit(" ", 1)[1].strip('"')
                break
        imap.select(pasta, readonly=True)
        for endereco, hashes in pedidos.items():
            _, dados = imap.search(None, "TO", f'"{endereco}"')
            for num in dados[0].split():
                _, partes = imap.fetch(num, "(RFC822)")
                msg = email_mod.message_from_bytes(partes[0][1], policy=policy.default)
                corpo = msg.get_body(preferencelist=("plain",))
                for m in LINK_RE.finditer(corpo.get_content() if corpo else ""):
                    h = bd.hash_token(m.group(1))
                    if h in hashes:
                        achados[h] = m.group(0)
    return achados


def destinatarios_avaliacao(con, links, emails_extra, coordenacao=False):
    """Uma entrada por pessoa (bd.chave_pessoa) com link activo de triagem ou de
    entidade: os seus links e o e-mail. O e-mail vem do link da entidade (o do
    formulário), dos e-mails acrescentados à mão ou do lembrete de triagem."""
    de_triagem = {}
    for (ref,) in con.execute("SELECT ref FROM envios WHERE tipo = 'triagem' AND resultado = 'enviado'"):
        endereco, _, nome = (ref or "").partition("|")
        de_triagem[bd.chave_pessoa(nome)] = endereco
    pessoas = {}
    for t in con.execute("SELECT * FROM tokens WHERE revogado = 0 AND expira_em > ? AND perfil IN ('tecnico', 'entidade'"
                         + (", 'coordenador'" if coordenacao else "") + ") ORDER BY criado_em", (bd.agora_utc(),)):
        chave = bd.chave_pessoa(t["nome"])
        p = pessoas.setdefault(chave, {"pessoa": chave, "nome": t["nome"], "email": None, "triagem": None,
                                       "entidade": None, "hashes": set(), "ate_triagem": None, "tem_entidade": False,
                                       "coordenacao": False})
        if t["perfil"] == "coordenador":
            p["coordenacao"] = True
        elif t["perfil"] == "entidade":
            p["nome"], p["tem_entidade"] = t["nome"], True     # o nome completo, como no formulário
            p["email"] = (t["email"] or "").lower() or p["email"]
            p["hashes"].add(t["token_hash"])
            p["entidade"] = links.get(t["token_hash"]) or p["entidade"]
        else:
            p["triagem"] = links.get(t["token_hash"]) or p["triagem"]
            p["ate_triagem"] = t["expira_em"]
    for chave, p in pessoas.items():
        p["email"] = emails_extra.get(chave) or p["email"] or de_triagem.get(chave)
    return sorted(pessoas.values(), key=lambda p: _chave(p["nome"]))


def cmd_avaliacao(a):
    """Avisa os técnicos (triagem e encaminhamento) de que têm o questionário de
    avaliação da organização no seu perfil, e reenvia na mesma mensagem os seus
    links. Os links vêm pela entrada padrão (os ficheiros fe2026-links-tecnicos.txt
    e fe2026-links-entidades.txt juntos) e não ficam no CT129; os que faltarem
    procuram-se na pasta Enviados. Não envia a quem já respondeu nem repete."""
    links, emails_extra = ler_entrada_avaliacao(sys.stdin)
    con = bd.ligar()
    bd.garantir_schema(con)
    pessoas = destinatarios_avaliacao(con, links, emails_extra)
    try:
        cfg = ler_config()
    except (OSError, SystemExit):
        cfg = None
    if cfg:
        pedidos = {}
        for p in pessoas:
            if p["tem_entidade"] and not p["entidade"] and p["email"]:
                pedidos.setdefault(p["email"], set()).update(p["hashes"])
        try:
            recuperados = links_em_enviados(cfg, pedidos)
        except Exception as e:   # sem a pasta Enviados, segue com os links da entrada
            print(f"AVISO: não foi possível procurar links em Enviados: {e}", file=sys.stderr)
            recuperados = {}
        for p in pessoas:
            if not p["entidade"]:
                p["entidade"] = next((recuperados[h] for h in p["hashes"] if h in recuperados), None)
    por_email = {}
    for p in pessoas:
        por_email.setdefault(p["email"], []).append(p)
    respondeu = {r[0] for r in con.execute("SELECT pessoa FROM avaliacao_feira")}
    tipo = "avaliacao-feira-teste" if a.para else ("avaliacao-feira-lembrete" if a.lembrete else "avaliacao-feira")
    assunto = ASSUNTO_AVALIACAO_LEMBRETE if a.lembrete else ASSUNTO_AVALIACAO
    lote = Envio(con, a)
    try:
        for p in pessoas[:a.limite] if a.limite else pessoas:
            quem = f"{p['nome']} ({'triagem' if p['ate_triagem'] else ''}{' + ' if p['ate_triagem'] and p['tem_entidade'] else ''}{'encaminhamento' if p['tem_entidade'] else ''})"
            if not p["email"] or not EMAIL_OK.match(p["email"]):
                print(f"SEM E-MAIL: {quem}")
                lote.saltados += 1
                continue
            if not p["triagem"] and not p["entidade"]:
                print(f"RETIDO {quem}: nenhum link na entrada nem em Enviados")
                lote.saltados += 1
                continue
            outros = [o["nome"] for o in por_email[p["email"]] if o is not p]
            if outros and not a.partilhados:
                print(f"RETIDO {quem}: e-mail partilhado com {', '.join(outros)}")
                lote.saltados += 1
                continue
            if p["pessoa"] in respondeu and not a.para and not a.forcar:
                print(f"já respondeu: {quem}")
                lote.saltados += 1
                continue
            ref = f"{p['email']}|{p['pessoa']}"
            if not a.para and not a.forcar and ja_enviado(con, tipo, ref, "0"):
                print(f"já enviado: {quem}")
                lote.saltados += 1
                continue
            if not lote.pode_continuar():
                break
            falta = [n for n, ok in (("triagem", p["ate_triagem"] and not p["triagem"]),
                                     ("entidade", p["tem_entidade"] and not p["entidade"])) if ok]
            if a.simular:
                print(f"enviaria a {quem} <{p['email']}> · links: "
                      + ", ".join(n for n in ("triagem", "entidade") if p[n])
                      + (f" · SEM o link de {', '.join(falta)}" if falta else ""))
                continue
            msg = compor(lote.remetente, a.para or p["email"], ("[TESTE] " if a.para else "") + assunto,
                         "avaliacao_feira", nome_para=p["nome"], lembrete=a.lembrete, nome=primeiro_nome(p["nome"]), p=p,
                         ate_triagem=bd.para_local(p["ate_triagem"], "%d/%m/%Y") if p["ate_triagem"] else None,
                         validade=VALIDADE_SEGUIMENTO)
            lote.enviar(msg, tipo, ref, assunto, quem=quem)
    finally:
        rc = lote.fechar("avaliacao", f"{len(pessoas)} técnicos")
    return rc


# ── Página «Estatísticas» (4 Nov 2026) ───────────────────────────────────

ASSUNTO_ESTATISTICAS = "XI Feira de Emprego · nova página «Estatísticas» no dashboard"


def cmd_estatisticas(a):
    """Avisa a coordenação e os técnicos (triagem e encaminhamento) da nova página
    «Estatísticas» do dashboard. Não leva links: usam o botão no seu perfil. Os
    e-mails em falta (coordenação) vêm pela entrada padrão em linhas «nome<TAB>e-mail»."""
    _links, emails_extra = ler_entrada_avaliacao(sys.stdin)
    con = bd.ligar()
    bd.garantir_schema(con)
    pessoas = destinatarios_avaliacao(con, {}, emails_extra, coordenacao=True)
    por_email = {}
    for p in pessoas:
        por_email.setdefault(p["email"], []).append(p)
    tipo = "estatisticas-teste" if a.para else "estatisticas"
    lote = Envio(con, a)
    try:
        for p in pessoas[:a.limite] if a.limite else pessoas:
            quem = p["nome"]
            if not p["email"] or not EMAIL_OK.match(p["email"]):
                print(f"SEM E-MAIL: {quem}")
                lote.saltados += 1
                continue
            outros = [o["nome"] for o in por_email[p["email"]] if o is not p]
            if outros and not a.partilhados:
                print(f"RETIDO {quem}: e-mail partilhado com {', '.join(outros)}")
                lote.saltados += 1
                continue
            ref = f"{p['email']}|{p['pessoa']}"
            if not a.para and not a.forcar and ja_enviado(con, tipo, ref, "0"):
                print(f"já enviado: {quem}")
                lote.saltados += 1
                continue
            if not lote.pode_continuar():
                break
            if a.simular:
                print(f"enviaria a {quem} <{p['email']}> · {'com' if p['coordenacao'] or p['ate_triagem'] else 'sem'} resumo")
                continue
            msg = compor(lote.remetente, a.para or p["email"], ("[TESTE] " if a.para else "") + ASSUNTO_ESTATISTICAS,
                         "estatisticas", nome_para=p["nome"], nome=primeiro_nome(p["nome"]),
                         com_resumo=bool(p["coordenacao"] or p["ate_triagem"]))
            lote.enviar(msg, tipo, ref, ASSUNTO_ESTATISTICAS, quem=quem)
    finally:
        rc = lote.fechar("estatisticas", f"{len(pessoas)} pessoas")
    return rc


def main():
    p = argparse.ArgumentParser(description="E-mails da Feira de Emprego 2026")
    sub = p.add_subparsers(dest="cmd", required=True)
    for nome in ("estado", "candidatos", "links", "links-novos", "triagem", "devolvidos", "fecho", "acompanhamento",
                 "coordenacao", "avaliacao", "estatisticas"):
        s = sub.add_parser(nome)
        s.add_argument("--simular", action="store_true", help="só mostra a quem enviaria")
        s.add_argument("--para", help="envia tudo para este endereço, com [TESTE] no assunto")
        s.add_argument("--limite", type=int, help="só os primeiros N")
        s.add_argument("--forcar", action="store_true", help="envia mesmo sem alterações ou já enviado hoje")
        s.add_argument("--lembrete", action="store_true",
                       help="avaliacao: lembrete a quem ainda não respondeu (outro assunto e texto; uma vez por pessoa)")
        s.add_argument("--partilhados", action="store_true",
                       help="links: envia também quando o e-mail é partilhado por vários técnicos")
        s.add_argument("--nome", help="coordenacao: nome do destinatário")
        s.add_argument("--email", help="coordenacao: e-mail do destinatário")
        s.add_argument("--anexo", help="coordenacao: PDF a anexar (caminho no CT129)")
        s.add_argument("--so", action="append", help="candidatos: só a este e-mail (original ou corrigido); repetível")
    a = p.parse_args()
    return {"estado": cmd_estado, "candidatos": cmd_candidatos, "links": cmd_links,
            "links-novos": cmd_links_novos, "triagem": cmd_triagem, "devolvidos": cmd_devolvidos,
            "fecho": cmd_fecho, "acompanhamento": cmd_acompanhamento, "coordenacao": cmd_coordenacao,
            "avaliacao": cmd_avaliacao, "estatisticas": cmd_estatisticas}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
