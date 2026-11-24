"""Dashboard das candidaturas FE2026 (§5 da especificação).

Perfis (token aleatório na URL, trocado por um cookie no 1.º acesso):
  coordenador  vê tudo, atribui técnicos, valida e marca horários, exporta CSV
  tecnico      vê e trata só as candidaturas com o seu id_tecnico; sem NIF nem
               nº de documento, que não são precisos para a triagem (30 Set 2026)
  empresa      só leitura: os seus candidatos validados, horário e CV com marca
               de água; sem NIF nem nº de documento (ponto 3, recomendação).
               Desde 6 Out 2026 preenche o seguimento dos candidatos (nova
               entrevista, selecção, contrato) até 90 dias depois da Feira; os
               CV só até uma semana depois
  entidade     técnico da entidade que inscreveu os candidatos (6 Out 2026):
               só leitura da lista e do detalhe dos candidatos que inscreveu,
               sem validar, marcar nem atribuir
  rececao      check-in no dia da Feira (7 Out 2026): só a página /rececao,
               com nome, telefone, empresas e horas dos candidatos validados

Tudo o que mostra dados de um candidato fica registado na tabela acessos.
"""
import csv
import hmac
import io
import json
import os
import re
import sqlite3
import unicodedata
import urllib.parse
from datetime import datetime, timezone
from functools import wraps

from flask import (Flask, Response, abort, g, make_response, redirect, render_template, request, url_for)

import agenda
import bd
import correio
import cv
import notas

COOKIE = "fe2026"
# "auto": cookie Secure quando o pedido chegou por HTTPS (túnel Cloudflare,
# X-Forwarded-Proto) e não no acesso directo pela LAN, que é HTTP simples.
COOKIE_SECURE = os.environ.get("FE2026_COOKIE_SECURE", "auto")
RH_VE_DOCUMENTOS = os.environ.get("FE2026_RH_VE_DOCUMENTOS", "0") == "1"
ENV_FORMBRICKS = os.environ.get("FE2026_ENV_FILE", "/etc/gec-fe2026/formbricks.env")
# Instância de demonstração (formação dos técnicos, 30 Set 2026): BD só com
# dados fictícios, faixa "Demonstração" em todas as páginas e um CV de exemplo
# local em vez do Formbricks.
DEMO = os.environ.get("FE2026_DEMO") == "1"
CV_DEMO = os.environ.get("FE2026_CV_DEMO", "")
ESTADOS = ("Por validar", "Validado", "Anulado")
NOMES_PERFIS = {"coordenador": "Coordenação", "tecnico": "Técnico/a", "empresa": "Empresa", "entidade": "Entidade",
                "rececao": "Receção"}


def _hora_local(texto):
    return datetime.strptime(texto, "%Y-%m-%d %H:%M").replace(tzinfo=bd.TZ)


# Depois da Feira (6 Out 2026): a empresa passa a entrar no seguimento dos
# candidatos, e deixa de receber os CV uma semana depois do evento.
FIM_FEIRA = _hora_local(os.environ.get("FE2026_FIM_FEIRA", "2026-10-13 23:59"))
CV_EMPRESAS_ATE = _hora_local(os.environ.get("FE2026_CV_EMPRESAS_ATE", "2026-10-20 23:59"))
SIM_NAO = ("Sim", "Não")
TIPOS_CONTRATO = ("Sem termo", "Termo certo", "Termo incerto", "Trabalho temporário", "Estágio",
                  "Prestação de serviços", "Outro")
# validacao_alterada_por gravado pelo exportador (R3) quando anula uma inscrição repetida
ANULADO_DUPLICADO = "Automático (duplicado)"
# Catálogo de ofertas (7 Out 2026): no detalhe, as páginas da empresa abrem-se
# à parte, para quem valida ver os requisitos das ofertas. É o PDF público da Feira.
AQUI = os.path.dirname(os.path.abspath(__file__))
CATALOGO = os.environ.get("FE2026_CATALOGO", os.path.join(os.path.dirname(AQUI), "catalogo-ofertas.pdf"))
with open(os.path.join(AQUI, "catalogo-ofertas.json"), encoding="utf-8") as _f:
    PAGINAS_CATALOGO = json.load(_f)["empresas"]

app = Flask(__name__)


def formbricks_env():
    env = {}
    with open(ENV_FORMBRICKS) as f:
        for line in f:
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.strip().split("=", 1)
                env[k] = v
    return env


# ── Ligação, sessão e registo ─────────────────────────────────────────────

def sem_acentos(texto):
    """Minúsculas e sem acentos, para a pesquisa: «Inácio» encontra «Inacio» e vice-versa (8 Out 2026)."""
    if texto is None:
        return None
    return "".join(ch for ch in unicodedata.normalize("NFKD", str(texto))
                   if not unicodedata.combining(ch)).casefold()


def con():
    if "con" not in g:
        g.con = bd.ligar(app.config.get("DB_PATH"))
        g.con.create_function("sem_acentos", 1, sem_acentos, deterministic=True)
    return g.con


@app.teardown_appcontext
def fechar(_exc):
    c = g.pop("con", None)
    if c is not None:
        c.close()


def ip_cliente():
    return request.headers.get("CF-Connecting-IP") or request.remote_addr


def registar(accao, id_submissao=None, empresa=None, detalhe=None):
    t = g.get("token")
    con().execute(
        "INSERT INTO acessos (em, prefixo, perfil, nome, accao, id_submissao, empresa, detalhe, ip) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (bd.agora_utc(), t["prefixo"] if t else None, t["perfil"] if t else None, t["nome"] if t else None,
         accao, id_submissao, empresa, detalhe, ip_cliente()),
    )


def token_valido(token):
    if not token or len(token) > 200:
        return None
    row = con().execute("SELECT * FROM tokens WHERE token_hash = ?", (bd.hash_token(token),)).fetchone()
    if row is None or row["revogado"] or row["expira_em"] <= bd.agora_utc():
        return None
    return row


def cookie_seguro():
    if COOKIE_SECURE in ("0", "1"):
        return COOKIE_SECURE == "1"
    return request.is_secure or request.headers.get("X-Forwarded-Proto") == "https"


def csrf_de(token):
    return hmac.new(token.encode(), b"fe2026-csrf", "sha256").hexdigest()[:32]


@app.before_request
def autenticar():
    if request.endpoint in ("static", "saida"):
        return None
    novo = request.args.get("t")
    token = novo or request.cookies.get(COOKIE)
    row = token_valido(token)
    if row is None:
        if token:
            registar("recusado", detalhe="token inválido, expirado ou revogado")
        return make_response(render_template("erro.html", titulo="Acesso não autorizado",
                             mensagem="O link de acesso não é válido ou já expirou. "
                                      "Peça um novo à coordenação da Feira."), 403)
    g.token = row
    g.token_raw = token
    con().execute("UPDATE tokens SET ultimo_acesso = ? WHERE token_hash = ?", (bd.agora_utc(), row["token_hash"]))
    if novo:
        # Tira o token do endereço (histórico, partilhas, cabeçalho Referer).
        registar("entrar")
        # "//outro.site" seria um redireccionamento para fora: fica sempre um só "/".
        destino = "/" + request.path.lstrip("/")
        resp = redirect(destino)
        resp.set_cookie(COOKIE, novo, httponly=True, secure=cookie_seguro(), samesite="Lax",
                        expires=datetime.fromisoformat(row["expira_em"]))
        return resp
    if request.method == "POST":
        origem = request.headers.get("Origin")
        if origem and urllib.parse.urlsplit(origem).netloc != request.host:
            abort(403)
        if not hmac.compare_digest(request.form.get("csrf", ""), csrf_de(token)):
            abort(403)
    return None


@app.after_request
def cabecalhos(resp):
    # O cookie leva a validade do link no 1.º acesso. Renova-se a cada pedido
    # para acompanhar um link prolongado depois (coordenação, 6 Out 2026).
    t = g.get("token")
    if t and g.get("token_raw") and request.endpoint != "saida" and "Set-Cookie" not in resp.headers:
        resp.set_cookie(COOKIE, g.token_raw, httponly=True, secure=cookie_seguro(), samesite="Lax",
                        expires=datetime.fromisoformat(t["expira_em"]))
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    # same-origin e não no-referrer: com no-referrer o browser envia "Origin: null"
    # nos POST e a verificação de origem em autenticar() recusava todas as gravações
    # (25 Set 2026). O Referer continua a não sair para outros sites.
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = g.get("csp") or (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
    return resp


@app.context_processor
def contexto():
    t = g.get("token")
    return {"t": t, "csrf": csrf_de(g.token_raw) if g.get("token_raw") else "",
            "para_local": bd.para_local, "fim": fim_entrevista, "tolerancia": agenda.e_tolerancia, "demo": DEMO,
            "ve_docs": bool(t) and ve_documentos(), "nomes_perfis": NOMES_PERFIS,
            "cv_disponivel": bool(t) and cv_disponivel(), "cv_ate": CV_EMPRESAS_ATE.strftime("%d/%m/%Y"),
            "empresa_choice": empresa_do_token()["choice_id"] if t and t["perfil"] == "empresa" else None,
            # Aviso na lista (19 Out 2026): o questionário da organização ainda por responder
            "av_feira_aviso": aviso_avaliacao_feira() if t and request.endpoint == "lista" else None}


def aviso_avaliacao_feira():
    """Aviso na lista: questionário da organização por responder ("novo"), ou com
    um bloco do papel da pessoa acrescentado depois de ter respondido ("completar")."""
    if g.token["perfil"] not in ("coordenador", "tecnico", "entidade"):
        return None
    pessoa = bd.chave_pessoa(g.token["nome"])
    a = avaliacao_feira_de(pessoa)
    if a is None:
        return {"tipo": "novo"}
    falta = blocos_por_responder(a, papeis_da_pessoa(pessoa, g.token["perfil"]))
    return {"tipo": "completar", "blocos": [b["titulo"] for b in falta]} if falta else None


def agora():
    return datetime.now(timezone.utc)


def cv_disponivel():
    """Os CV deixam de ser servidos às empresas uma semana depois da Feira."""
    return g.token["perfil"] != "empresa" or agora() <= CV_EMPRESAS_ATE


def ve_documentos():
    """NIF e nº de documento: só a coordenação, que precisa deles para tratar
    as inscrições repetidas. As empresas têm uma opção própria (RH_VE_DOCUMENTOS)."""
    return g.token["perfil"] == "coordenador"


def fim_entrevista(horario, dur):
    return agenda.para_hhmm(agenda.para_minutos(horario) + (dur or 0))


def perfis(*permitidos):
    def deco(f):
        @wraps(f)
        def inner(*a, **kw):
            if g.token["perfil"] not in permitidos:
                registar("recusado", detalhe=request.path)
                abort(403)
            return f(*a, **kw)
        return inner
    return deco


@app.errorhandler(403)
def proibido(_e):
    return render_template("erro.html", titulo="Sem permissão",
                           mensagem="Este perfil não tem acesso a esta página."), 403


@app.errorhandler(404)
def nao_existe(_e):
    return render_template("erro.html", titulo="Não encontrado",
                           mensagem="O registo pedido não existe ou já não está activo."), 404


# ── Consultas ─────────────────────────────────────────────────────────────

BASE = f"""
SELECT c.*, e.choice_id, e.duracao_entrevista_min, e.hora_fim, s.criada_em, s.criada_em_local,
       s.entidade_nome, s.entidade_tecnico, s.entidade_email, s.entidade_telefone,
       s.cand_nome, s.cand_telefone, s.cand_email, s.cand_localidade, s.cand_habilitacoes,
       s.cand_nif, s.cand_tipo_doc, s.cand_num_doc, s.cv_url, s.flags AS flags_sub, s.duplicado_de,
       COALESCE(s.entidade_canon, s.entidade_nome) AS entidade, s.tecnico_id,
       COALESCE(s.tecnico_canon, s.entidade_tecnico) AS tecnico_ent,
       COALESCE(s.tecnico_email_canon, s.entidade_email) AS tecnico_ent_email,
       COALESCE(s.tecnico_tel_canon, s.entidade_telefone) AS tecnico_ent_tel
FROM candidaturas c
JOIN submissoes s ON s.id_submissao = c.id_submissao
JOIN empresas e ON e.empresa = c.empresa
WHERE {agenda.ACTIVAS}
"""


# O técnico não vê o que já lhe chega anulado pela coordenação ou pelo sistema
# (inscrições repetidas): só as anulações feitas por técnicos (30 Set 2026).
# Se a coordenação repuser «Por validar», a candidatura volta a aparecer-lhe.
SEM_ANULADAS_PELA_COORDENACAO = (" AND (c.validacao <> 'Anulado' OR c.validacao_alterada_por IN "
                                 "(SELECT nome FROM tokens WHERE perfil = 'tecnico'))")


def ambito():
    """Restrição SQL do perfil actual."""
    t = g.token
    if t["perfil"] == "tecnico":
        return " AND c.id_tecnico = ?" + SEM_ANULADAS_PELA_COORDENACAO, [t["id_tecnico"]]
    if t["perfil"] == "empresa":
        return " AND c.empresa = ? AND c.validacao = 'Validado'", [t["empresa"]]
    if t["perfil"] == "entidade":
        # Todas as inscrições do mesmo técnico, com as várias grafias unificadas.
        return " AND s.tecnico_id = (SELECT tecnico_id FROM submissoes WHERE id_submissao = ?)", [t["ref_submissao"]]
    if t["perfil"] == "rececao":
        return " AND c.validacao = 'Validado'", []
    return "", []


def flags(r):
    fl = [f for f in (r["flags_sub"] or "").split("; ") if f]
    if r["sem_oferta"]:
        fl.append("Sem oferta")
    if r["excede_capacidade"]:
        fl.append("Excede capacidade")
    return fl


def candidatura(id_submissao, choice_id):
    extra, params = ambito()
    r = con().execute(BASE + " AND c.id_submissao = ? AND e.choice_id = ?" + extra,
                      [id_submissao, choice_id] + params).fetchone()
    if r is None:
        registar("recusado", id_submissao=id_submissao, detalhe=f"candidatura {choice_id} fora do âmbito")
        abort(404)
    return r


def empresas():
    return con().execute("SELECT * FROM empresas WHERE removida = 0 ORDER BY empresa").fetchall()


def tecnicos():
    """Quem pode receber candidaturas: os técnicos e os coordenadores a quem
    se deu um id_tecnico (validam com o link de coordenação)."""
    return con().execute("SELECT DISTINCT id_tecnico, nome FROM tokens WHERE revogado = 0 AND id_tecnico IS NOT NULL "
                         "AND perfil IN ('tecnico', 'coordenador') ORDER BY nome").fetchall()


def estado(r, espera):
    if r["validacao"] == "Validado":
        if r["horario_entrevista"]:
            return "marcado", f"Validado · {r['horario_entrevista']}"
        pos = espera.get((r["id_submissao"], r["empresa"]))
        return "espera", f"Em espera{f' (n.º {pos})' if pos else ''}"
    return {"Por validar": "pendente", "Anulado": "anulado"}[r["validacao"]], r["validacao"]


# ── Páginas ───────────────────────────────────────────────────────────────

@app.get("/")
def inicio():
    if g.token["perfil"] == "empresa":
        if agora() > FIM_FEIRA:
            return redirect(url_for("seguimento_empresa", choice_id=empresa_do_token()["choice_id"]))
        return redirect(url_for("agenda_empresa", choice_id=empresa_do_token()["choice_id"]))
    if g.token["perfil"] == "rececao":
        return redirect(url_for("rececao"))
    return redirect(url_for("lista"))


def empresa_do_token():
    return agenda.empresa_por_nome(con(), g.token["empresa"])


@app.get("/candidaturas")
@perfis("coordenador", "tecnico", "entidade")
def lista():
    extra, params = ambito()
    f = {k: request.args.get(k, "") for k in ("empresa", "estado", "tecnico", "entidade", "q")}
    if f["entidade"] and g.token["perfil"] == "coordenador":
        extra += " AND COALESCE(s.entidade_canon, s.entidade_nome) = ?"
        params.append(f["entidade"])
    if f["empresa"]:
        extra += " AND e.choice_id = ?"
        params.append(f["empresa"])
    if f["estado"] in ESTADOS:
        extra += " AND c.validacao = ?"
        params.append(f["estado"])
    elif f["estado"] == "espera":
        extra += " AND c.validacao = 'Validado' AND c.horario_entrevista IS NULL"
    if f["tecnico"] == "_nenhum":
        extra += " AND c.id_tecnico IS NULL"
    elif f["tecnico"]:
        extra += " AND c.id_tecnico = ?"
        params.append(f["tecnico"])
    if f["q"] and ve_documentos():
        extra += " AND (sem_acentos(s.cand_nome) LIKE ? OR s.cand_nif LIKE ? OR s.cand_num_doc LIKE ?)"
        params += [f"%{sem_acentos(f['q'])}%", f"%{f['q']}%", f"%{f['q']}%"]
    elif f["q"]:
        # Procurar por NIF confirmaria a quem não o vê que o número existe.
        extra += " AND sem_acentos(s.cand_nome) LIKE ?"
        params.append(f"%{sem_acentos(f['q'])}%")
    rows = con().execute(BASE + extra + " ORDER BY s.criada_em, c.empresa", params).fetchall()
    espera = agenda.posicoes_em_espera(con())
    linhas = [{"r": r, "flags": flags(r), "estado": estado(r, espera)} for r in rows]
    extra_t, params_t = ambito()
    totais = con().execute(
        f"SELECT c.validacao, c.horario_entrevista IS NOT NULL AS marcado, count(*) AS n FROM candidaturas c "
        f"JOIN submissoes s ON s.id_submissao = c.id_submissao WHERE {agenda.ACTIVAS}{extra_t} "
        f"GROUP BY 1, 2", params_t).fetchall()
    resumo = {"Por validar": 0, "marcado": 0, "espera": 0, "Anulado": 0}
    for t in totais:
        if t["validacao"] == "Validado":
            resumo["marcado" if t["marcado"] else "espera"] += t["n"]
        else:
            resumo[t["validacao"]] += t["n"]
    registar("listar", detalhe=urllib.parse.urlencode({k: v for k, v in f.items() if v}))
    return render_template("lista.html", linhas=linhas, f=f, empresas=empresas(), tecnicos=tecnicos(),
                           resumo=resumo, entidades=entidades() if g.token["perfil"] == "coordenador" else [])


def entidades():
    return [r[0] for r in con().execute(
        "SELECT DISTINCT COALESCE(entidade_canon, entidade_nome) FROM submissoes "
        "WHERE removida = 0 AND COALESCE(entidade_canon, entidade_nome) IS NOT NULL")]


def _chave(s):
    return _ascii((s or "").strip()).casefold()


def triadas():
    """Candidaturas já validadas ou anuladas, no âmbito do perfil, agrupadas
    por entidade e técnico que fez a inscrição: é a quem se comunica o
    resultado da triagem (30 Set 2026)."""
    extra, params = ambito()
    rows = con().execute(BASE + extra + " AND c.validacao IN ('Validado', 'Anulado')", params).fetchall()
    # Entidade e técnico unificados (6 Out 2026): as várias grafias ficam no mesmo grupo.
    rows.sort(key=lambda r: (_chave(r["entidade"]), _chave(r["tecnico_ent"]), r["tecnico_ent_email"] or "",
                             _chave(r["cand_nome"]), r["empresa"]))
    espera = agenda.posicoes_em_espera(con())
    grupos = []
    for r in rows:
        chave = (_chave(r["entidade"]), _chave(r["tecnico_ent"]), r["tecnico_ent_email"])
        if not grupos or grupos[-1]["chave"] != chave:
            grupos.append({"chave": chave, "r": r, "linhas": []})
        grupos[-1]["linhas"].append({"r": r, "resultado": resultado(r, espera)})
    return grupos, len(rows)


def resultado(r, espera):
    """(classe, texto) do resultado da triagem, como se diz à entidade."""
    if r["validacao"] == "Anulado":
        return "anulado", "Anulada"
    if r["horario_entrevista"]:
        return "marcado", (f"Validada · entrevista às {r['horario_entrevista']}"
                           f"–{fim_entrevista(r['horario_entrevista'], r['duracao_entrevista_min'])}")
    pos = espera.get((r["id_submissao"], r["empresa"]))
    return "espera", f"Validada · em lista de espera{f' (n.º {pos})' if pos else ''}"


@app.get("/comunicacao")
@perfis("coordenador", "tecnico")
def comunicacao():
    grupos, total = triadas()
    registar("comunicacao", detalhe=f"{total} linhas")
    agora = datetime.now(timezone.utc).astimezone(bd.TZ).strftime("%d/%m/%Y às %H:%M")
    return render_template("comunicacao.html", grupos=grupos, total=total, agora=agora)


@app.get("/comunicacao.csv")
@perfis("coordenador", "tecnico")
def comunicacao_csv():
    grupos, total = triadas()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Entidade", "Técnico/a da entidade", "E-mail", "Telefone", "Candidato/a", "Empresa", "Ofertas",
                "Resultado", "Observações", "Validação por"])
    for gr in grupos:
        for l in gr["linhas"]:
            r = l["r"]
            w.writerow([r["entidade"], r["tecnico_ent"], r["tecnico_ent_email"], r["tecnico_ent_tel"],
                        r["cand_nome"], r["empresa"], r["ofertas_seleccionadas"], l["resultado"][1],
                        r["observacoes"], r["validacao_alterada_por"]])
    registar("comunicacao", detalhe=f"csv, {total} linhas")
    data = datetime.now(timezone.utc).astimezone(bd.TZ).strftime("%Y%m%d-%H%M")
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="FE2026-validadas-anuladas-{data}.csv"'})


@app.post("/atribuir")
@perfis("coordenador")
def atribuir():
    choice_id = request.form.get("empresa", "")
    id_tecnico = request.form.get("id_tecnico", "")
    so_sem = request.form.get("so_sem_tecnico") == "1"
    emp = con().execute("SELECT empresa FROM empresas WHERE choice_id = ?", (choice_id,)).fetchone()
    if emp is None or id_tecnico not in {t["id_tecnico"] for t in tecnicos()}:
        abort(400)
    con().execute("BEGIN IMMEDIATE")
    n = con().execute(
        "UPDATE candidaturas SET id_tecnico = ? WHERE empresa = ? AND removida = 0"
        + (" AND id_tecnico IS NULL" if so_sem else ""), (id_tecnico, emp["empresa"])).rowcount
    registar("atribuir", empresa=emp["empresa"], detalhe=f"{n} candidaturas → {id_tecnico}")
    con().execute("COMMIT")
    return redirect(url_for("lista", empresa=choice_id, msg=f"{n} candidaturas atribuídas a {id_tecnico}."))


@app.route("/c/<id_submissao>/<choice_id>", methods=["GET", "POST"])
@perfis("coordenador", "tecnico", "entidade")
def detalhe(id_submissao, choice_id):
    erro = None
    pode_editar = g.token["perfil"] != "entidade"
    if request.method == "POST":
        if not pode_editar:
            registar("recusado", id_submissao=id_submissao, detalhe="entidade só tem leitura")
            abort(403)
        erro = gravar(id_submissao, choice_id)
        if erro is None:
            return redirect(url_for("detalhe", id_submissao=id_submissao, choice_id=choice_id, ok=1))
    r = candidatura(id_submissao, choice_id)
    registar("ver", id_submissao=id_submissao, empresa=r["empresa"])
    disp = agenda.disponibilidade(con(), id_submissao, r["empresa"])
    sugerido = horario_sugerido(r, disp)
    sids = agenda.submissoes_da_pessoa(con(), id_submissao)
    extra_o, params_o = ambito() if g.token["perfil"] == "entidade" else ("", [])
    outras = con().execute(
        BASE + (SEM_ANULADAS_PELA_COORDENACAO if g.token["perfil"] == "tecnico" else "") + extra_o
        + f" AND c.id_submissao IN ({','.join('?' * len(sids))}) ORDER BY c.horario_entrevista IS NULL, "
        "c.horario_entrevista", params_o + sids).fetchall()
    outras = [o for o in outras if not (o["id_submissao"] == id_submissao and o["empresa"] == r["empresa"])]
    espera = agenda.posicoes_em_espera(con())
    return render_template("detalhe.html", r=r, flags=flags(r), disp=disp, sugerido=sugerido, outras=outras,
                           estado=estado(r, espera), espera=espera, erro=erro, tecnicos=tecnicos(),
                           pode_editar=pode_editar, ok=request.args.get("ok"),
                           catalogo=paginas_catalogo(r["empresa"]))


def horario_sugerido(r, disp):
    """Horário pré-seleccionado no formulário (8 Out 2026): o já marcado; para quem ainda não está
    validado, o primeiro horário livre (os de tolerância só depois dos outros); sem nenhum livre, a
    lista de espera. Uma candidatura já validada em lista de espera continua nela: guardar a ficha
    não lhe atribui horário sem o técnico o escolher."""
    if r["horario_entrevista"]:
        return r["horario_entrevista"]
    if r["validacao"] == "Validado":
        return None
    livres = [d for d in disp if d["livre"]]
    livres.sort(key=lambda d: d["tolerancia"])
    return livres[0]["horario"] if livres else None


def gravar(id_submissao, choice_id):
    """Grava validação, horário, observações (e técnico, se coordenação).
    Tudo numa transacção: as regras de marcação são verificadas sobre o
    estado actual, com a escrita trancada."""
    validacao = request.form.get("validacao")
    horario = request.form.get("horario") or None
    obs = (request.form.get("observacoes") or "").strip() or None
    if validacao not in ESTADOS:
        return "Estado de validação inválido."
    c = con()
    c.execute("BEGIN IMMEDIATE")
    try:
        r = candidatura(id_submissao, choice_id)
        if (r["validacao"] == "Anulado" and validacao != "Anulado"
                and r["validacao_alterada_por"] == ANULADO_DUPLICADO and g.token["perfil"] != "coordenador"):
            c.execute("ROLLBACK")
            return ("Esta candidatura foi anulada automaticamente por ser uma inscrição repetida. "
                    "Só a coordenação a pode reverter.")
        if validacao != "Validado":
            horario = None          # anular ou voltar a "Por validar" liberta o horário
        if validacao != "Anulado":
            obs = notas.sem_notas_automaticas(obs)   # a nota da anulação automática já não se aplica
        if horario and horario != r["horario_entrevista"]:
            motivo = agenda.motivo_recusa(c, id_submissao, r["empresa"], horario)
            if motivo:
                c.execute("ROLLBACK")
                return f"Não foi possível marcar às {horario}: {motivo}. Escolha outro horário."
        sets = {"validacao": validacao, "horario_entrevista": horario, "observacoes": obs}
        if validacao != r["validacao"]:
            sets["validacao_alterada_em"] = bd.agora_utc(preciso=True)
            sets["validacao_alterada_por"] = g.token["nome"]
        if g.token["perfil"] == "coordenador":
            tec = request.form.get("id_tecnico", "")
            if tec and tec not in {t["id_tecnico"] for t in tecnicos()}:
                c.execute("ROLLBACK")
                return "Técnico desconhecido."
            sets["id_tecnico"] = tec or None
        c.execute(f"UPDATE candidaturas SET {', '.join(f'{k} = ?' for k in sets)} "
                  "WHERE id_submissao = ? AND empresa = ?", [*sets.values(), id_submissao, r["empresa"]])
        mudou = {k: v for k, v in sets.items() if k in r.keys() and r[k] != v and not k.startswith("validacao_alterada")}
        registar("alterar", id_submissao=id_submissao, empresa=r["empresa"],
                 detalhe="; ".join(f"{k}={v}" for k, v in mudou.items() if k != "observacoes")
                 + ("; observacoes" if "observacoes" in mudou else ""))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    return None


@app.get("/agenda/<choice_id>")
def agenda_empresa(choice_id):
    emp = con().execute("SELECT * FROM empresas WHERE choice_id = ?", (choice_id,)).fetchone()
    if emp is None:
        abort(404)
    perfil = g.token["perfil"]
    if perfil in ("entidade", "rececao") or (perfil == "empresa" and emp["empresa"] != g.token["empresa"]):
        registar("recusado", empresa=emp["empresa"], detalhe="agenda de outra empresa")
        abort(403)
    rows = con().execute(BASE + " AND c.empresa = ? AND c.validacao = 'Validado' ORDER BY c.horario_entrevista",
                         (emp["empresa"],)).fetchall()
    # O técnico vê a ocupação de toda a agenda, mas só os nomes dos seus candidatos.
    def visivel(r):
        return perfil != "tecnico" or r["id_tecnico"] == g.token["id_tecnico"]
    por_horario = {}
    for r in rows:
        if r["horario_entrevista"]:
            por_horario.setdefault(r["horario_entrevista"], []).append({"r": r, "visivel": visivel(r)})
    # Um horário marcado que entretanto saiu da grelha (ex. tolerância reduzida)
    # continua a aparecer, para ninguém ficar esquecido.
    horarios = sorted(set(agenda.grelha(emp)) | set(por_horario), key=agenda.para_minutos)
    grelha = [{"horario": h, "marcados": por_horario.get(h, []), "tolerancia": agenda.e_tolerancia(emp, h)}
              for h in horarios]
    espera = [{"r": r, "visivel": visivel(r)} for r in
              sorted((r for r in rows if not r["horario_entrevista"]),
                     key=lambda r: (r["validacao_alterada_em"] is None, r["validacao_alterada_em"] or "",
                                    r["criada_em"]))]
    registar("agenda", empresa=emp["empresa"])
    return render_template("agenda.html", emp=emp, grelha=grelha, espera=espera,
                           lugares=emp["n_entrevistadores"] or 1, rh_docs=RH_VE_DOCUMENTOS,
                           empresas=empresas() if perfil != "empresa" else [])


@app.get("/cv/<id_submissao>/<choice_id>")
def ver_cv(id_submissao, choice_id):
    perfil = g.token["perfil"]
    if perfil == "rececao":
        abort(403)
    if perfil == "empresa":
        emp = empresa_do_token()
        if emp["choice_id"] != choice_id:
            abort(403)
        if not cv_disponivel():
            registar("recusado", id_submissao=id_submissao, empresa=emp["empresa"], detalhe="CV depois do prazo")
            return render_template("erro.html", titulo="CV já não disponível",
                                   mensagem=f"Os CV dos candidatos estiveram disponíveis até "
                                            f"{CV_EMPRESAS_ATE.strftime('%d/%m/%Y')}, uma semana depois da Feira."), 403
    r = candidatura(id_submissao, choice_id)   # aplica o âmbito do perfil
    try:
        if DEMO:
            with open(CV_DEMO, "rb") as f:
                pdf = f.read()
        else:
            env = formbricks_env()
            pdf = cv.descarregar(r["cv_url"], env.get("FORMBRICKS_GEC_URL", cv.PUBLICO), env["FORMBRICKS_GEC_API_KEY"])
        if perfil == "empresa":
            pdf = cv.marca_de_agua(pdf, r["empresa"])
    except cv.CVIndisponivel as e:
        registar("cv-falhou", id_submissao=id_submissao, empresa=r["empresa"], detalhe=str(e))
        # 422 e não 502: a Cloudflare troca as respostas 502 da origem pela página dela
        # e a explicação nunca chegava ao browser (29 Set 2026).
        return render_template("erro.html", titulo="CV indisponível",
                               mensagem=f"Não foi possível abrir o CV: {e}. Contacte a coordenação."), 422
    registar("cv", id_submissao=id_submissao, empresa=r["empresa"])
    nome = re.sub(r"[^A-Za-z0-9]+", "-", _ascii(r["cand_nome"] or "cv")).strip("-") or "cv"
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="CV-{nome}.pdf"'})


def paginas_catalogo(empresa):
    """(1.ª, última) página da empresa no catálogo, ou None sem catálogo."""
    pags = PAGINAS_CATALOGO.get(empresa)
    return tuple(pags) if pags and os.path.exists(CATALOGO) else None


_ofertas_cache = {}


def ofertas_da_empresa(empresa):
    """PDF só com as páginas da empresa, guardado em memória enquanto o
    catálogo não mudar."""
    ini, fim = paginas_catalogo(empresa)
    chave = (empresa, os.path.getmtime(CATALOGO))
    if chave not in _ofertas_cache:
        from pypdf import PdfReader, PdfWriter
        leitor, escritor = PdfReader(CATALOGO), PdfWriter()
        for i in range(ini - 1, min(fim, len(leitor.pages))):
            escritor.add_page(leitor.pages[i])
        buf = io.BytesIO()
        escritor.write(buf)
        _ofertas_cache[chave] = buf.getvalue()
    return _ofertas_cache[chave]


@app.get("/ofertas/<choice_id>.pdf")
def ofertas_pdf(choice_id):
    emp = con().execute("SELECT empresa FROM empresas WHERE choice_id = ?", (choice_id,)).fetchone()
    if emp is None or paginas_catalogo(emp["empresa"]) is None:
        abort(404)
    if g.token["perfil"] == "empresa" and emp["empresa"] != g.token["empresa"]:
        abort(403)
    registar("ofertas", empresa=emp["empresa"])
    nome = re.sub(r"[^A-Za-z0-9]+", "-", _ascii(emp["empresa"])).strip("-")
    return Response(ofertas_da_empresa(emp["empresa"]), mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="Ofertas-{nome}.pdf"'})


def _ascii(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


@app.get("/exportar.csv")
@perfis("coordenador")
def exportar():
    cur = con().execute("SELECT * FROM v_candidaturas")
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([d[0] for d in cur.description])
    n = 0
    for row in cur:
        w.writerow(row)
        n += 1
    registar("exportar", detalhe=f"{n} linhas")
    data = datetime.now(timezone.utc).astimezone(bd.TZ).strftime("%Y%m%d-%H%M")
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="FE2026-candidaturas-{data}.csv"'})


def ultima_sincronizacao():
    row = con().execute("SELECT max(em) AS em FROM sincronizacoes").fetchone()
    return bd.para_local(row["em"], "%d/%m/%Y às %H:%M") if row and row["em"] else None


@app.get("/vagas")
@perfis("coordenador")
def vagas():
    lista_vagas = agenda.vagas(con())
    abertas = [v for v in lista_vagas if v["aberta"]]
    agora = datetime.now(timezone.utc).astimezone(bd.TZ).strftime("%d/%m/%Y às %H:%M")
    registar("vagas")
    return render_template("vagas.html", abertas=abertas, fechadas=[v for v in lista_vagas if not v["aberta"]],
                           agora=agora, sincronizado=ultima_sincronizacao())


@app.get("/vagas.csv")
@perfis("coordenador")
def vagas_csv():
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Empresa", "Ofertas", "Situação", "Vagas", "Inscrições", "Capacidade"])
    for v in agenda.vagas(con()):
        w.writerow([v["e"]["empresa"], v["e"]["ofertas"] or "", v["situacao"],
                    v["livres"] if v["livres"] is not None else "", v["inscritas"],
                    v["capacidade"] if v["capacidade"] is not None else ""])
    registar("vagas", detalhe="csv")
    data = datetime.now(timezone.utc).astimezone(bd.TZ).strftime("%Y%m%d-%H%M")
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="FE2026-vagas-{data}.csv"'})


# ── Seguimento pelas empresas depois da Feira (6 Out 2026) ────────────────
# Desde 13 Out 2026 também o técnico da entidade que inscreveu o candidato vê e
# actualiza o seguimento dos seus candidatos nas empresas em que fizeram
# entrevista. É o mesmo registo: o que um grava, o outro vê, com quem actualizou.

CAMPOS_SEGUIMENTO = ("nova_entrevista", "selecionado", "contratado", "tipo_contrato", "duracao_contrato",
                     "observacoes")
QUEM_SEGUIMENTO = {"empresa": "pela empresa", "entidade": "pelo/a técnico/a da entidade", "coordenador": "pela coordenação"}


def validadas_da_empresa(empresa):
    """Candidaturas validadas da empresa: entrevistas pela hora e depois a lista de espera."""
    rows = con().execute(BASE + " AND c.empresa = ? AND c.validacao = 'Validado'", (empresa,)).fetchall()
    return sorted(rows, key=lambda r: (r["horario_entrevista"] is None,
                                       agenda.para_minutos(r["horario_entrevista"]) if r["horario_entrevista"] else 0,
                                       r["validacao_alterada_em"] or "", _chave(r["cand_nome"])))


def na_feira(x):
    """O que a receção registou numa entrevista: (classe, texto), ou None sem registo."""
    if x is None:
        return None
    if x["entrevistado"]:
        return "feita", "Entrevista feita"
    if x["faltou"] or not x["presente"]:
        return ("faltou", "Não compareceu") if x["faltou"] else None
    return "nao-feita", "Esteve na Feira, sem esta entrevista"


SUBMISSAO_SEG = """
SELECT s.id_submissao, s.cand_nome, s.cand_telefone, s.cand_email, s.cv_url,
       COALESCE(s.entidade_canon, s.entidade_nome) AS entidade,
       COALESCE(s.tecnico_canon, s.entidade_tecnico) AS tecnico_ent
FROM submissoes s WHERE s.removida = 0 AND s.id_submissao = ?"""


def seguimento_linhas(empresa):
    """Quem a empresa tinha para entrevistar e quem entrevistou: as candidaturas
    validadas e as entrevistas acrescentadas no dia na receção (13 Out 2026),
    cada uma com o que a receção registou. Os candidatos sem inscrição não
    entram: não têm técnico nem contactos além do telefone."""
    rec = {r["id_submissao"]: r for r in con().execute("SELECT * FROM rececao WHERE empresa = ?", (empresa,))}
    espera = agenda.posicoes_em_espera(con())
    linhas = []
    for r in validadas_da_empresa(empresa):
        hora = r["horario_entrevista"] or f"Espera {espera.get((r['id_submissao'], r['empresa']), '')}".strip()
        linhas.append({"sid": r["id_submissao"], "empresa": empresa, "nome": r["cand_nome"],
                       "telefone": r["cand_telefone"], "email": r["cand_email"], "entidade": r["entidade"],
                       "tecnico": r["tecnico_ent"], "hora": hora, "espera": not r["horario_entrevista"],
                       "cv": bool(r["cv_url"]), "choice_id": r["choice_id"], "extra": False,
                       "feira": na_feira(rec.get(r["id_submissao"]))})
    for sid, x in sorted(rec.items(), key=lambda kv: kv[1]["alterado_em"]):
        if not x["extra"] or sid.startswith(AVULSO):
            continue
        s = con().execute(SUBMISSAO_SEG, (sid,)).fetchone()
        if s is None:
            continue
        linhas.append({"sid": sid, "empresa": empresa, "nome": s["cand_nome"], "telefone": s["cand_telefone"],
                       "email": s["cand_email"], "entidade": s["entidade"], "tecnico": s["tecnico_ent"],
                       "hora": x["horario_dia"] or "Extra", "espera": False, "cv": False, "choice_id": None,
                       "extra": True, "feira": na_feira(x)})
    return linhas


def seguimento_de(empresa=None):
    """{(id_submissao, empresa): linha} do seguimento, de uma empresa ou de todas."""
    q, params = ("SELECT * FROM seguimento WHERE empresa = ?", (empresa,)) if empresa else ("SELECT * FROM seguimento", ())
    return {(r["id_submissao"], r["empresa"]): r for r in con().execute(q, params)}


def assinatura_seguimento(v):
    """Resumo dos valores de uma linha do seguimento como foram mostrados: ao gravar,
    só se escrevem as linhas que a pessoa alterou (13 Out 2026). Sem isto, uma
    página aberta há horas repunha os valores antigos numa linha que entretanto a
    empresa ou o técnico da entidade actualizou."""
    vals = [(v[k] if v is not None else None) or "" for k in CAMPOS_SEGUIMENTO]
    return hmac.new(b"fe2026-seguimento", json.dumps(vals, ensure_ascii=False).encode(), "sha256").hexdigest()[:16]


def quem_actualizou(s):
    return QUEM_SEGUIMENTO.get(s["alterado_perfil"] or "", "") if s else ""


def empresa_do_seguimento(choice_id):
    emp = con().execute("SELECT * FROM empresas WHERE choice_id = ?", (choice_id,)).fetchone()
    if emp is None:
        abort(404)
    if g.token["perfil"] == "empresa" and emp["empresa"] != g.token["empresa"]:
        registar("recusado", empresa=emp["empresa"], detalhe="seguimento de outra empresa")
        abort(403)
    return emp


@app.get("/seguimento")
@perfis("coordenador", "empresa", "entidade")
def seguimento():
    if g.token["perfil"] == "empresa":
        return redirect(url_for("seguimento_empresa", choice_id=empresa_do_token()["choice_id"]))
    if g.token["perfil"] == "entidade":
        return redirect(url_for("seguimento_entidade"))
    resumo = []
    seg = seguimento_de()
    for e in empresas():
        linhas = seguimento_linhas(e["empresa"])
        if not linhas:
            continue
        def conta(campo):
            return sum(1 for l in linhas if (s := seg.get((l["sid"], l["empresa"]))) and s[campo] == "Sim")
        resumo.append({"e": e, "validadas": sum(1 for l in linhas if not l["extra"]),
                       "extra": sum(1 for l in linhas if l["extra"]),
                       "feitas": sum(1 for l in linhas if l["feira"] and l["feira"][0] == "feita"),
                       "preenchidas": sum(1 for l in linhas if (l["sid"], l["empresa"]) in seg),
                       "nova_entrevista": conta("nova_entrevista"), "selecionado": conta("selecionado"),
                       "contratado": conta("contratado")})
    registar("seguimento", detalhe="resumo")
    return render_template("seguimento_resumo.html", resumo=resumo)


@app.route("/seguimento/<choice_id>", methods=["GET", "POST"])
@perfis("coordenador", "empresa")
def seguimento_empresa(choice_id):
    emp = empresa_do_seguimento(choice_id)
    linhas = seguimento_linhas(emp["empresa"])
    erro = None
    if request.method == "POST":
        erro = gravar_seguimento({(l["sid"], l["empresa"]) for l in linhas})
        if erro is None:
            return redirect(url_for("seguimento_empresa", choice_id=choice_id, ok=1))
    registar("seguimento", empresa=emp["empresa"], detalhe=f"{len(linhas)} candidatos")
    return render_template("seguimento.html", emp=emp, linhas=linhas, seg=seguimento_de(emp["empresa"]),
                           erro=erro, ok=request.args.get("ok"), sim_nao=SIM_NAO, tipos=TIPOS_CONTRATO,
                           assin=assinatura_seguimento, quem=quem_actualizou)


def entrevistas_do_tecnico():
    """As entrevistas feitas na Feira pelos candidatos do técnico da entidade
    (registo da receção), incluindo as acrescentadas no dia, por candidato e empresa."""
    # Também as que já têm seguimento sem a receção ter marcado a entrevista como feita.
    rows = con().execute(
        """SELECT p.id_submissao, p.empresa, r.extra, r.horario_dia, c.horario_entrevista,
                  s.cand_nome, s.cand_telefone, s.cand_email
           FROM (SELECT id_submissao, empresa FROM rececao WHERE entrevistado = 1
                 UNION SELECT id_submissao, empresa FROM seguimento) p
           JOIN submissoes s ON s.id_submissao = p.id_submissao
           LEFT JOIN rececao r ON r.id_submissao = p.id_submissao AND r.empresa = p.empresa
           LEFT JOIN candidaturas c ON c.id_submissao = p.id_submissao AND c.empresa = p.empresa
           WHERE s.removida = 0 AND s.tecnico_id = (SELECT tecnico_id FROM submissoes WHERE id_submissao = ?)""",
        (g.token["ref_submissao"],)).fetchall()
    linhas = [{"sid": r["id_submissao"], "empresa": r["empresa"], "nome": r["cand_nome"], "telefone": r["cand_telefone"],
               "email": r["cand_email"], "hora": r["horario_dia"] or r["horario_entrevista"] or "",
               "extra": bool(r["extra"])} for r in rows]
    return sorted(linhas, key=lambda l: (_chave(l["nome"]), l["sid"], _chave(l["empresa"])))


@app.route("/seguimento/os-meus-candidatos", methods=["GET", "POST"])
@perfis("entidade")
def seguimento_entidade():
    linhas = entrevistas_do_tecnico()
    erro = None
    if request.method == "POST":
        erro = gravar_seguimento({(l["sid"], l["empresa"]) for l in linhas})
        if erro is None:
            return redirect(url_for("seguimento_entidade", ok=1))
    registar("seguimento", detalhe=f"entidade, {len(linhas)} entrevistas")
    return render_template("seguimento_entidade.html", linhas=linhas, seg=seguimento_de(), erro=erro,
                           ok=request.args.get("ok"), sim_nao=SIM_NAO, tipos=TIPOS_CONTRATO,
                           assin=assinatura_seguimento, quem=quem_actualizou)


def gravar_seguimento(permitidas):
    """Grava as linhas do formulário (pares candidatura·empresa de `permitidas`) que a
    pessoa alterou; as que vieram como foram mostradas não se escrevem."""
    pares = [tuple(x.split("|", 1)) for x in request.form.getlist("linha")]
    if not pares or any(len(p) != 2 for p in pares) or not set(pares) <= permitidas:
        return "O formulário não corresponde aos candidatos desta página. Recarregue a página."
    novos = {}
    for i, par in enumerate(pares):
        v = {k: (request.form.get(f"{k}:{i}") or "").strip() or None for k in CAMPOS_SEGUIMENTO}
        if any(v[k] not in (None, *SIM_NAO) for k in ("nova_entrevista", "selecionado", "contratado")):
            return "Valor inválido numa das respostas Sim/Não."
        if v["tipo_contrato"] not in (None, *TIPOS_CONTRATO):
            return "Tipo de contrato inválido."
        if len(v["duracao_contrato"] or "") > 60 or len(v["observacoes"] or "") > 1000:
            return "Texto demasiado comprido (duração: 60 caracteres; observações: 1000)."
        if assinatura_seguimento(v) != request.form.get(f"v:{i}"):
            novos[par] = v
    c = con()
    c.execute("BEGIN IMMEDIATE")
    try:
        actuais = seguimento_de()
        for (sid, emp), v in novos.items():
            antes = actuais.get((sid, emp))
            if antes is None and not any(v.values()):
                continue
            if antes is not None and all(antes[k] == v[k] for k in CAMPOS_SEGUIMENTO):
                continue
            c.execute(
                f"INSERT INTO seguimento (id_submissao, empresa, {', '.join(CAMPOS_SEGUIMENTO)}, alterado_em, "
                f"alterado_por, alterado_perfil) VALUES (?, ?, {', '.join('?' * len(CAMPOS_SEGUIMENTO))}, ?, ?, ?) "
                "ON CONFLICT(id_submissao, empresa) DO UPDATE SET "
                + ", ".join(f"{k} = excluded.{k}" for k in (*CAMPOS_SEGUIMENTO, "alterado_em", "alterado_por",
                                                            "alterado_perfil")),
                [sid, emp, *(v[k] for k in CAMPOS_SEGUIMENTO), bd.agora_utc(), g.token["nome"], g.token["perfil"]])
            registar("seguimento-alterar", id_submissao=sid, empresa=emp)
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    return None


@app.get("/seguimento.csv")
@perfis("coordenador")
def seguimento_csv():
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Empresa", "Candidato/a", "Telefone", "E-mail", "Entidade", "Técnico/a da entidade", "Entrevista",
                "Na Feira", "Fez nova entrevista?", "Foi selecionado?", "Ficou contratado?", "Tipo de contrato",
                "Duração do contrato", "Observações", "Actualizado em", "Por"])
    n = 0
    seg = seguimento_de()
    for e in empresas():
        for l in seguimento_linhas(e["empresa"]):
            s = seg.get((l["sid"], l["empresa"]))
            w.writerow([e["empresa"], l["nome"], l["telefone"], l["email"], l["entidade"], l["tecnico"],
                        ("Acrescentada no dia" + (f" ({l['hora']})" if l["hora"] != "Extra" else "")) if l["extra"]
                        else ("lista de espera" if l["espera"] else l["hora"]),
                        l["feira"][1] if l["feira"] else "",
                        *((s[k] or "" for k in CAMPOS_SEGUIMENTO) if s else [""] * len(CAMPOS_SEGUIMENTO)),
                        bd.para_local(s["alterado_em"], "%d/%m/%Y %H:%M") if s else "",
                        (f"{s['alterado_por']} ({quem_actualizou(s)})" if quem_actualizou(s) else s["alterado_por"])
                        if s else ""])
            n += 1
    registar("seguimento", detalhe=f"csv, {n} linhas")
    data = agora().astimezone(bd.TZ).strftime("%Y%m%d-%H%M")
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="FE2026-acompanhamento-{data}.csv"'})


# ── Avaliação do evento pelas empresas (13 Out 2026) ──────────────────────
# O questionário «Avaliação Final da Qualidade» que se dava em papel às
# empresas, agora no link de cada uma. Várias respostas por empresa, uma por
# pessoa de RH (o link é o mesmo para todos). A coordenação consulta e exporta.

ESCALA = ((1, "Insatisfeito"), (2, "Pouco satisfeito"), (3, "Satisfeito"), (4, "Muito satisfeito"))
GRUPOS_AVALIACAO = (
    ("Organização", (("atendimento", "Atendimento e receção"), ("interesse", "Interesse do evento"))),
    ("Espaço e tempo", (("horario", "Horário"), ("duracao", "Duração do evento"),
                        ("espaco", "Localização/Espaço"), ("equipamento", "Equipamento disponível"))),
    ("Speed Recruitment", (("divulgacao", "Divulgação das ofertas"),
                           ("encaminhamento", "Encaminhamento dos candidatos"),
                           ("matching", "Matching candidatos vs oferta de emprego"))),
    ("Plataforma online", (("plataforma", "Plataforma de acesso à agenda e ao acompanhamento dos candidatos"),)),
    ("Avaliação global", (("global", "Avaliação global do evento"),)),
)
PERGUNTAS = tuple(q for _g, qs in GRUPOS_AVALIACAO for q in qs)
TEXTOS_AVALIACAO = (("positivos", "Aspetos positivos"), ("negativos", "Aspetos negativos"),
                    ("sugestoes", "Comentários e/ou sugestões"))
CAMPOS_AVALIACAO = (*(k for k, _ in PERGUNTAS), *(k for k, _ in TEXTOS_AVALIACAO), "respondido_por", "obs_notas")
NOTA_SEM_OBS = 3         # abaixo disto (1 e 2) a empresa tem de dizer porquê


def avaliacoes(empresa=None):
    """As respostas, pela ordem em que chegaram; de uma empresa ou de todas."""
    if empresa:
        return con().execute("SELECT * FROM avaliacao WHERE empresa = ? ORDER BY criada_em, id", (empresa,)).fetchall()
    return con().execute("SELECT * FROM avaliacao ORDER BY empresa, criada_em, id").fetchall()


def obs_de(a):
    """As observações às notas abaixo de NOTA_SEM_OBS, pela ordem das perguntas."""
    obs = json.loads(a["obs_notas"] or "{}") if a else {}
    return [{"k": k, "rotulo": r, "nota": a[k], "texto": obs[k]} for k, r in PERGUNTAS if obs.get(k)]


def itens_grafico(perguntas, medias):
    """Linhas do gráfico de notas (_grafico_notas.html): rótulo, média, distribuição e N/A."""
    return [{"rotulo": r, "media": medias[k]["media"], "dist": medias[k]["dist"], "na": medias[k].get("na", 0)}
            for k, r, *_ in perguntas]


def mais_baixa(itens):
    """O item com a média mais baixa, para destacar no painel."""
    com_nota = [x for x in itens if x["media"] is not None]
    return min(com_nota, key=lambda x: x["media"]) if com_nota else None


def pct(parte, todo):
    return round(100 * parte / todo) if todo else 0


def resumo_notas(medias, chave="global", topo=4):
    """Indicadores de uma avaliação: nota global, quantos deram a nota máxima ou a seguinte."""
    m = medias[chave]
    n = sum(m["dist"])
    return {"media": m["media"], "n": n, "pct_alta": pct(sum(m["dist"][topo - 2:]), n)}


@app.get("/avaliacao")
@perfis("coordenador", "empresa")
def avaliacao():
    if g.token["perfil"] == "empresa":
        return redirect(url_for("avaliacao_empresa", choice_id=empresa_do_token()["choice_id"]))
    respostas = avaliacoes()
    por_empresa = {}
    for a in respostas:
        por_empresa.setdefault(a["empresa"], []).append({"a": a, "obs": obs_de(a)})
    lista = [{"e": e, "respostas": por_empresa.get(e["empresa"], [])} for e in empresas()]
    medias = {}
    for k, _ in PERGUNTAS:
        notas_q = [a[k] for a in respostas if a[k]]
        medias[k] = {"media": sum(notas_q) / len(notas_q) if notas_q else None,
                     "dist": [sum(1 for n in notas_q if n == v) for v, _ in ESCALA]}
    registar("avaliacao", detalhe="resumo")
    return render_template("avaliacao_resumo.html", lista=lista, n=len(respostas),
                           n_empresas=sum(1 for x in lista if x["respostas"]), medias=medias,
                           grupos=GRUPOS_AVALIACAO, perguntas=PERGUNTAS, textos=TEXTOS_AVALIACAO, escala=ESCALA,
                           nota_sem_obs=NOTA_SEM_OBS, itens=itens_grafico(PERGUNTAS, medias),
                           pior=mais_baixa(itens_grafico(PERGUNTAS, medias)), resumo=resumo_notas(medias, topo=4))


def empresa_da_avaliacao(choice_id):
    emp = con().execute("SELECT * FROM empresas WHERE choice_id = ?", (choice_id,)).fetchone()
    if emp is None:
        abort(404)
    if g.token["perfil"] == "empresa" and emp["empresa"] != g.token["empresa"]:
        registar("recusado", empresa=emp["empresa"], detalhe="avaliação de outra empresa")
        abort(403)
    return emp


@app.get("/avaliacao/<choice_id>")
@perfis("coordenador", "empresa")
def avaliacao_empresa(choice_id):
    """As respostas já dadas pela empresa (uma por pessoa de RH). Sem nenhuma,
    vai directo ao questionário."""
    emp = empresa_da_avaliacao(choice_id)
    respostas = avaliacoes(emp["empresa"])
    if not respostas and not request.args.get("ok"):
        return redirect(url_for("avaliacao_responder", choice_id=choice_id))
    registar("avaliacao", empresa=emp["empresa"], detalhe=f"{len(respostas)} respostas")
    return render_template("avaliacao_empresa.html", emp=emp, respostas=respostas, ok=request.args.get("ok"))


@app.route("/avaliacao/<choice_id>/nova", methods=["GET", "POST"], defaults={"id_resposta": None})
@app.route("/avaliacao/<choice_id>/<int:id_resposta>", methods=["GET", "POST"])
@perfis("coordenador", "empresa")
def avaliacao_responder(choice_id, id_resposta):
    emp = empresa_da_avaliacao(choice_id)
    a = None
    if id_resposta is not None:
        a = con().execute("SELECT * FROM avaliacao WHERE id = ? AND empresa = ?", (id_resposta, emp["empresa"])).fetchone()
        if a is None:
            abort(404)
    erro = None
    if request.method == "POST":
        erro = gravar_avaliacao(emp, a)
        if erro is None:
            return redirect(url_for("avaliacao_empresa", choice_id=choice_id, ok=1))
    obs = ({k: request.form.get(f"obs:{k}", "") for k, _ in PERGUNTAS} if erro
           else {o["k"]: o["texto"] for o in obs_de(a)})
    registar("avaliacao", empresa=emp["empresa"], detalhe=f"resposta {id_resposta}" if a else "nova resposta")
    return render_template("avaliacao.html", emp=emp, a=a, f=request.form if erro else a, obs=obs, erro=erro,
                           nota_sem_obs=NOTA_SEM_OBS, grupos=GRUPOS_AVALIACAO, textos=TEXTOS_AVALIACAO,
                           escala=ESCALA, ja_responderam=len(avaliacoes(emp["empresa"])))


def gravar_avaliacao(emp, a):
    """Grava uma resposta nova (a é None) ou corrige a resposta a."""
    v = {k: (request.form.get(k) or "").strip() or None for k in CAMPOS_AVALIACAO}
    for k, rotulo in PERGUNTAS:
        if v[k] not in {str(n) for n, _ in ESCALA}:
            return f"Falta responder a «{rotulo}»."
        v[k] = int(v[k])
    obs = {}
    for k, rotulo in PERGUNTAS:
        if v[k] < NOTA_SEM_OBS:
            texto = (request.form.get(f"obs:{k}") or "").strip()
            if not texto:
                return f"Deu {v[k]} a «{rotulo}»: nas notas inferiores a {NOTA_SEM_OBS}, diga-nos o motivo nas observações."
            if len(texto) > 1000:
                return f"A observação a «{rotulo}» é demasiado comprida (1000 caracteres)."
            obs[k] = texto
    v["obs_notas"] = json.dumps(obs, ensure_ascii=False) if obs else None
    if not v["respondido_por"]:
        return "Indique o nome de quem responde."
    if len(v["respondido_por"]) > 120 or any(len(v[k] or "") > 2000 for k, _ in TEXTOS_AVALIACAO):
        return "Texto demasiado comprido (nome: 120 caracteres; comentários: 2000)."
    agora_iso = bd.agora_utc()
    if a is None:
        cur = con().execute(
            f"INSERT INTO avaliacao (empresa, {', '.join(CAMPOS_AVALIACAO)}, criada_em, alterado_em, alterado_por) "
            f"VALUES (?, {', '.join('?' * len(CAMPOS_AVALIACAO))}, ?, ?, ?)",
            [emp["empresa"], *(v[k] for k in CAMPOS_AVALIACAO), agora_iso, agora_iso, g.token["nome"]])
        id_resposta = cur.lastrowid
    else:
        con().execute(
            f"UPDATE avaliacao SET {', '.join(f'{k} = ?' for k in CAMPOS_AVALIACAO)}, alterado_em = ?, alterado_por = ? "
            "WHERE id = ?", [*(v[k] for k in CAMPOS_AVALIACAO), agora_iso, g.token["nome"], a["id"]])
        id_resposta = a["id"]
    registar("avaliacao-alterar", empresa=emp["empresa"],
             detalhe=f"resposta {id_resposta} ({'nova' if a is None else 'corrigida'}); global={v['global']}")
    return None


@app.get("/avaliacao.csv")
@perfis("coordenador")
def avaliacao_csv():
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Empresa", "Respondido por", *(r for _, r in PERGUNTAS),
                f"Observações às notas inferiores a {NOTA_SEM_OBS}", *(r for _, r in TEXTOS_AVALIACAO),
                "Respondido em", "Actualizado em"])
    respostas = avaliacoes()
    for a in respostas:
        w.writerow([a["empresa"], a["respondido_por"], *(a[k] for k, _ in PERGUNTAS),
                    " | ".join(f"{o['rotulo']} ({o['nota']}): {o['texto']}" for o in obs_de(a)),
                    *(a[k] or "" for k, _ in TEXTOS_AVALIACAO),
                    bd.para_local(a["criada_em"], "%d/%m/%Y %H:%M"), bd.para_local(a["alterado_em"], "%d/%m/%Y %H:%M")])
    registar("avaliacao", detalhe=f"csv, {len(respostas)} respostas")
    data = agora().astimezone(bd.TZ).strftime("%Y%m%d-%H%M")
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="FE2026-avaliacao-{data}.csv"'})


# ── Avaliação da organização pelos parceiros (19 Out 2026) ────────────────
# Técnicos de encaminhamento (perfil entidade), de triagem (tecnico) e a
# coordenação respondem ao mesmo questionário, para se poderem comparar os
# três olhares. Uma resposta por pessoa (bd.chave_pessoa), seja qual for o
# link com que entra; pode ser revista. A coordenação vê os resultados.

ESCALA_FEIRA = ((1, "Muito mau"), (2, "Mau"), (3, "Razoável"), (4, "Bom"), (5, "Muito bom"),
                (0, "N/A"))
NA_FEIRA = "Não aplicável / sem conhecimento suficiente"
PERGUNTAS_FEIRA = (
    ("planeamento", "Planeamento e preparação da Feira",
     "Definição de objetivos, calendarização das atividades, distribuição de tarefas e cumprimento dos prazos de preparação."),
    ("comunicacao", "Comunicação e articulação entre os parceiros",
     "Clareza e oportunidade da informação partilhada, facilidade de contacto, coordenação e colaboração entre as entidades envolvidas."),
    ("logistica", "Organização e condições logísticas",
     "Adequação dos espaços, mobiliário, equipamentos, sinalética, acessibilidade e condições de funcionamento."),
    ("dia_evento", "Funcionamento e coordenação no dia do evento",
     "Acolhimento e encaminhamento dos participantes, organização das entrevistas, distribuição de funções e capacidade de resposta a imprevistos."),
    ("entidades", "Adequação das entidades e oportunidades de emprego",
     "Diversidade e relevância das entidades empregadoras, adequação das ofertas aos perfis dos candidatos e variedade de oportunidades disponibilizadas."),
    ("global", "Avaliação global da organização da Feira",
     "Qualidade geral da organização, articulação das diferentes componentes e correspondência entre o planeamento e a execução do evento."),
    ("plataforma_uso", "Facilidade de utilização da plataforma digital",
     "Clareza da interface, facilidade de registo, consulta e acompanhamento das candidaturas."),
    ("plataforma_util", "Utilidade da plataforma na gestão da Feira",
     "Contributo para a organização das inscrições, triagens, partilha de informação e agendamento das entrevistas."),
)
# (campo, pergunta, explicação, obrigatória)
TEXTOS_FEIRA = (
    ("correu_bem", "O que correu bem?",
     "Aspetos positivos, boas práticas e situações que contribuíram para o sucesso da Feira.", True),
    ("melhorar", "O que poderá correr melhor?",
     "Dificuldades, constrangimentos e oportunidades de melhoria, tanto na preparação como no decorrer do evento.", True),
    ("comentarios", "Comentários, sugestões e elogios", "Espaço livre para outros contributos.", False),
)
# Blocos só para um papel (19 Out 2026, aprovados pelo utilizador): a triagem, feita
# pela primeira vez na plataforma, e o que só a coordenação vê. As 8 perguntas
# comuns ficam iguais para todos, para se compararem os grupos. Quem respondeu
# antes de os blocos existirem completa-os depois (aviso na lista).
ESCALA_COMPARACAO = ((1, "Muito pior"), (2, "Pior"), (3, "Igual"), (4, "Melhor"), (5, "Muito melhor"), (0, "N/A"))
BLOCOS_FEIRA = (
    {"papel": "triagem", "titulo": "A triagem das candidaturas",
     "intro": "Perguntas só para quem fez a triagem, a primeira vez feita na plataforma da Feira.",
     "perguntas": (
         ("tri_criterios", "Informação para decidir",
          "Clareza dos critérios de validação e anulação e acesso aos requisitos das ofertas (catálogo na ficha da candidatura).",
          ESCALA_FEIRA),
         ("tri_inscricoes", "Qualidade das inscrições recebidas",
          "Dados completos, CV legível e adequação dos perfis dos candidatos às ofertas.", ESCALA_FEIRA),
         ("tri_tempo", "Tempo e volume da triagem",
          "Prazo entre a chegada das inscrições e a Feira e número de candidaturas atribuídas.", ESCALA_FEIRA),
         ("tri_marcacao", "Marcação das entrevistas na plataforma",
          "Escolha do horário, horários de tolerância e lista de espera.", ESCALA_FEIRA),
         ("tri_automatico", "Tratamento automático das candidaturas",
          "Atribuição automática aos técnicos e anulação das inscrições repetidas ou acima das 3 empresas.", ESCALA_FEIRA),
         ("tri_comunicacao", "Comunicação do resultado da triagem",
          "E-mails automáticos aos candidatos e aos técnicos de encaminhamento depois da validação.", ESCALA_FEIRA),
         ("tri_apoio", "Preparação e apoio para a triagem",
          "Formação, demonstração, manual e apoio da coordenação durante a triagem.", ESCALA_FEIRA),
         ("tri_comparacao", "Em comparação com a forma como a triagem era feita antes, fazê-la na plataforma foi…",
          "Se foi a primeira vez que fez triagem numa Feira, escolha N/A.", ESCALA_COMPARACAO),
     ),
     "texto": ("tri_sugestao", "Que alteração ou funcionalidade tornaria a triagem mais fácil na próxima edição?",
               "Opcional.")},
    {"papel": "coordenacao", "titulo": "A coordenação da Feira",
     "intro": "Perguntas só para a coordenação, sobre o que só ela acompanhou de perto.",
     "perguntas": (
         ("coord_empresas", "Articulação com as empresas",
          "Angariação, informação sobre as ofertas, cumprimento do combinado e presença no dia.", ESCALA_FEIRA),
         ("coord_entidades", "Envolvimento das entidades parceiras",
          "Adesão, cumprimento dos prazos de inscrição e qualidade das inscrições.", ESCALA_FEIRA),
         ("coord_recursos", "Recursos disponíveis",
          "Pessoas, tempo e meios face ao que a Feira exigiu.", ESCALA_FEIRA),
         ("coord_divulgacao", "Divulgação externa",
          "Chegada da Feira aos candidatos e à comunidade.", ESCALA_FEIRA),
     ),
     "texto": ("coord_decisao", "Que decisão mudaria na preparação da próxima edição?", "Opcional.")},
)
TODAS_PERGUNTAS_FEIRA = (*((k, r, d, ESCALA_FEIRA) for k, r, d in PERGUNTAS_FEIRA),
                         *(q for b in BLOCOS_FEIRA for q in b["perguntas"]))
CAMPOS_FEIRA = (*(k for k, *_ in TODAS_PERGUNTAS_FEIRA), *(k for k, *_ in TEXTOS_FEIRA),
                *(b["texto"][0] for b in BLOCOS_FEIRA), "obs_notas")
GRUPOS_FEIRA = (("coordenacao", "Coordenação"), ("triagem", "Técnicos de triagem"),
                ("encaminhamento", "Técnicos de encaminhamento"))
PERFIL_GRUPO = {"coordenador": "coordenacao", "tecnico": "triagem", "entidade": "encaminhamento"}
# Notas 1 e 2: aparece uma caixa para explicar porquê, opcional (decisão do
# utilizador, 19 Out 2026: obrigá-la levaria a subir a nota para não escrever).
NOTA_EXPLICAR_FEIRA = 3


def papeis_por_pessoa():
    """{pessoa: papéis} nos links activos: coordenacao, triagem, encaminhamento."""
    out = {}
    for t in con().execute("SELECT perfil, nome FROM tokens WHERE revogado = 0 AND perfil IN "
                           "('coordenador', 'tecnico', 'entidade')"):
        out.setdefault(bd.chave_pessoa(t["nome"]), set()).add(PERFIL_GRUPO[t["perfil"]])
    return out


def papeis_da_pessoa(pessoa, perfil):
    """Os papéis da pessoa em todos os seus links activos, mais o do link com que
    entrou. O grupo da resposta é o principal (coordenação, triagem,
    encaminhamento); os blocos de perguntas são os de todos os papéis: quem
    coordenou e também triou responde aos dois."""
    return papeis_por_pessoa().get(pessoa, set()) | {PERFIL_GRUPO[perfil]}


def avaliacao_feira_de(pessoa):
    return con().execute("SELECT * FROM avaliacao_feira WHERE pessoa = ?", (pessoa,)).fetchone()


def blocos_de(papeis):
    return [b for b in BLOCOS_FEIRA if b["papel"] in papeis]


def perguntas_de(papeis):
    """(campo, rótulo, explicação, escala) das perguntas de escala que a pessoa vê."""
    return [(k, r, d, ESCALA_FEIRA) for k, r, d in PERGUNTAS_FEIRA] + [
        q for b in blocos_de(papeis) for q in b["perguntas"]]


def blocos_por_responder(a, papeis):
    """Os blocos do papel da pessoa que ficaram por responder (respondeu antes de existirem)."""
    return [b for b in blocos_de(papeis) if a is not None and a[b["perguntas"][0][0]] is None]


def obs_feira_de(a):
    """As explicações das notas abaixo de NOTA_EXPLICAR_FEIRA, pela ordem das perguntas."""
    obs = json.loads(a["obs_notas"] or "{}") if a else {}
    return [{"k": k, "rotulo": r, "nota": a[k], "texto": obs[k]} for k, r, *_ in TODAS_PERGUNTAS_FEIRA if obs.get(k)]


@app.route("/avaliacao-feira", methods=["GET", "POST"])
@perfis("coordenador", "tecnico", "entidade")
def avaliacao_feira():
    pessoa = bd.chave_pessoa(g.token["nome"])
    papeis = papeis_da_pessoa(pessoa, g.token["perfil"])
    a = avaliacao_feira_de(pessoa)
    erro = None
    if request.method == "POST":
        erro = gravar_avaliacao_feira(pessoa, a, papeis)
        if erro is None:
            return redirect(url_for("avaliacao_feira", ok=1))
    obs = ({k: request.form.get(f"obs:{k}", "") for k, *_ in perguntas_de(papeis)} if erro
           else {o["k"]: o["texto"] for o in obs_feira_de(a)})
    registar("avaliacao-feira", detalhe="resposta" if a else "nova resposta")
    return render_template("avaliacao_feira.html", a=a, f=request.form if erro else a, erro=erro, obs=obs,
                           ok=request.args.get("ok"), perguntas=PERGUNTAS_FEIRA, textos=TEXTOS_FEIRA,
                           escala=ESCALA_FEIRA, na=NA_FEIRA, blocos=blocos_de(papeis),
                           por_responder=blocos_por_responder(a, papeis))


def gravar_avaliacao_feira(pessoa, a, papeis):
    v = {k: (request.form.get(k) or "").strip() or None for k in CAMPOS_FEIRA}
    visiveis = perguntas_de(papeis)
    textos = [(k, r, d, o) for k, r, d, o in TEXTOS_FEIRA] + [(*b["texto"], False) for b in blocos_de(papeis)]
    for k, rotulo, _d, escala in visiveis:
        if v[k] not in {str(n) for n, _ in escala}:
            return f"Falta responder a «{rotulo}» (se não tem conhecimento suficiente, escolha N/A)."
        v[k] = int(v[k])
    for k, rotulo, _d, obrigatoria in textos:
        if obrigatoria and not v[k]:
            return f"Falta responder a «{rotulo}»."
        if len(v[k] or "") > 3000:
            return f"A resposta a «{rotulo}» é demasiado comprida (3000 caracteres)."
    # Os blocos de outro papel (ex.: a pessoa deixou de ter link de triagem) mantêm-se como estavam
    mostrados = {k for k, *_ in visiveis} | {k for k, *_ in textos}
    for k in CAMPOS_FEIRA:
        if k not in mostrados and k != "obs_notas":
            v[k] = a[k] if a else None
    obs = {o["k"]: o["texto"] for o in obs_feira_de(a) if o["k"] not in mostrados}
    for k, rotulo, *_ in visiveis:
        texto = (request.form.get(f"obs:{k}") or "").strip()
        if texto and 0 < v[k] < NOTA_EXPLICAR_FEIRA:      # numa nota que subiu, a explicação deixa de valer
            if len(texto) > 1000:
                return f"A explicação da nota a «{rotulo}» é demasiado comprida (1000 caracteres)."
            obs[k] = texto
    v["obs_notas"] = json.dumps(obs, ensure_ascii=False) if obs else None
    agora_iso = bd.agora_utc()
    grupo = next(p for p in ("coordenacao", "triagem", "encaminhamento") if p in papeis)
    if a is None:
        try:
            con().execute(
                f"INSERT INTO avaliacao_feira (pessoa, nome, grupo, {', '.join(CAMPOS_FEIRA)}, criada_em, alterado_em, "
                f"alterado_por, alterado_perfil) VALUES (?, ?, ?, {', '.join('?' * len(CAMPOS_FEIRA))}, ?, ?, ?, ?)",
                [pessoa, g.token["nome"], grupo, *(v[k] for k in CAMPOS_FEIRA), agora_iso, agora_iso,
                 g.token["nome"], g.token["perfil"]])
        except sqlite3.IntegrityError:    # a mesma pessoa respondeu entretanto noutro link
            return "Já tinha respondido entretanto (talvez com o seu outro link). Reveja a resposta abaixo e guarde de novo."
    else:
        con().execute(
            f"UPDATE avaliacao_feira SET grupo = ?, {', '.join(f'{k} = ?' for k in CAMPOS_FEIRA)}, alterado_em = ?, "
            "alterado_por = ?, alterado_perfil = ? WHERE id = ?",
            [grupo, *(v[k] for k in CAMPOS_FEIRA), agora_iso, g.token["nome"], g.token["perfil"], a["id"]])
    registar("avaliacao-feira-alterar", detalhe=f"{'nova' if a is None else 'corrigida'}; global={v['global']}; "
                                                f"blocos={','.join(b['papel'] for b in blocos_de(papeis)) or '-'}")
    return None


def medias_feira(respostas, perguntas=None):
    """Por pergunta: média (sem os N/A), n.º de N/A e distribuição de 1 a 5."""
    out = {}
    for k, *_ in perguntas or PERGUNTAS_FEIRA:
        notas_q = [a[k] for a in respostas if a[k]]
        out[k] = {"media": sum(notas_q) / len(notas_q) if notas_q else None, "n": len(notas_q),
                  "na": sum(1 for a in respostas if a[k] == 0),
                  "dist": [sum(1 for n in notas_q if n == v) for v in range(1, 6)]}
    return out


@app.get("/avaliacao-feira/resultados")
@perfis("coordenador")
def avaliacao_feira_resultados():
    respostas = con().execute("SELECT * FROM avaliacao_feira ORDER BY grupo, nome").fetchall()
    por_grupo = [{"k": k, "nome": nome, "respostas": [a for a in respostas if a["grupo"] == k],
                  "medias": medias_feira([a for a in respostas if a["grupo"] == k])} for k, nome in GRUPOS_FEIRA]
    papeis = papeis_por_pessoa()
    blocos = []
    for b in BLOCOS_FEIRA:
        resp_b = [a for a in respostas if a[b["perguntas"][0][0]] is not None]
        em_falta = [a["nome"] for a in respostas if a[b["perguntas"][0][0]] is None
                    and b["papel"] in papeis.get(a["pessoa"], {a["grupo"]})]
        blocos.append({**b, "respostas": resp_b, "medias": medias_feira(resp_b, b["perguntas"]), "em_falta": em_falta})
    explicacoes = [{"a": a, "obs": obs_feira_de(a)} for a in respostas if a["obs_notas"]]
    registar("avaliacao-feira", detalhe=f"resultados, {len(respostas)} respostas")
    return render_template("avaliacao_feira_resultados.html", respostas=respostas, medias=medias_feira(respostas),
                           por_grupo=por_grupo, perguntas=PERGUNTAS_FEIRA, textos=TEXTOS_FEIRA, explicacoes=explicacoes,
                           escala=[e for e in ESCALA_FEIRA if e[0]], grupos=dict(GRUPOS_FEIRA), blocos=blocos,
                           itens=itens_grafico(PERGUNTAS_FEIRA, medias_feira(respostas)),
                           itens_por_grupo=[{"nome": gr["nome"], "n": len(gr["respostas"]),
                                             "itens": itens_grafico(PERGUNTAS_FEIRA, gr["medias"])}
                                            for gr in por_grupo if gr["respostas"]],
                           blocos_itens=[itens_grafico(b["perguntas"], b["medias"]) for b in blocos],
                           resumo=resumo_notas(medias_feira(respostas), topo=5),
                           ja_respondi=avaliacao_feira_de(bd.chave_pessoa(g.token["nome"])))


@app.get("/avaliacao-feira.csv")
@perfis("coordenador")
def avaliacao_feira_csv():
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    blocos_txt = [b["texto"] for b in BLOCOS_FEIRA]
    w.writerow(["Nome", "Grupo", *(r for _k, r, *_ in TODAS_PERGUNTAS_FEIRA), "Explicação das notas 1 e 2",
                *(r for _k, r, *_ in TEXTOS_FEIRA), *(r for _k, r, _d in blocos_txt), "Respondido em", "Actualizado em"])
    respostas = con().execute("SELECT * FROM avaliacao_feira ORDER BY grupo, nome").fetchall()
    grupos = dict(GRUPOS_FEIRA)
    for a in respostas:
        w.writerow([a["nome"], grupos.get(a["grupo"], a["grupo"]),
                    *("" if a[k] is None else (a[k] or "N/A") for k, *_ in TODAS_PERGUNTAS_FEIRA),
                    " | ".join(f"{o['rotulo']} ({o['nota']}): {o['texto']}" for o in obs_feira_de(a)),
                    *(a[k] or "" for k, *_ in TEXTOS_FEIRA), *(a[k] or "" for k, _r, _d in blocos_txt),
                    bd.para_local(a["criada_em"], "%d/%m/%Y %H:%M"), bd.para_local(a["alterado_em"], "%d/%m/%Y %H:%M")])
    registar("avaliacao-feira", detalhe=f"csv, {len(respostas)} respostas")
    data = agora().astimezone(bd.TZ).strftime("%Y%m%d-%H%M")
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="FE2026-avaliacao-organizacao-{data}.csv"'})


# ── Listagens para o dia da Feira (coordenação, 6 Out 2026) ───────────────

POR_VALIDAR = "Por validar"


def hora_do_dia(r, espera):
    """O que as listagens do dia e a receção mostram na coluna da hora: a hora
    marcada, «Espera n» ou, se a candidatura ainda não foi validada, «Por validar»
    (12 Out 2026: podem aparecer na Feira e têm de constar nas listas)."""
    if r["validacao"] == POR_VALIDAR:
        return POR_VALIDAR
    return r["horario_entrevista"] or f"Espera {espera.get((r['id_submissao'], r['empresa']), '')}".strip()


def agendas_para_listagem():
    """Por empresa: entrevistas marcadas pela hora, depois a lista de espera e, no fim,
    as candidaturas ainda por validar. As empresas sem nenhuma ficam de fora."""
    espera = agenda.posicoes_em_espera(con())
    folhas = []
    for e in empresas():
        rows = validadas_da_empresa(e["empresa"])
        rows += sorted(con().execute(BASE + " AND c.empresa = ? AND c.validacao = 'Por validar'",
                                     (e["empresa"],)).fetchall(),
                       key=lambda r: (r["criada_em"], _chave(r["cand_nome"])))
        if rows:
            folhas.append({"e": e, "linhas": [{"r": r, "hora": hora_do_dia(r, espera)} for r in rows]})
    return folhas


def candidatos_para_rececao():
    """Uma linha por candidato (as inscrições com o mesmo NIF ou documento
    juntam-se), por ordem alfabética, com as empresas em que tem entrevista.
    Inclui as candidaturas ainda «Por validar» (12 Out 2026)."""
    espera = agenda.posicoes_em_espera(con())
    rows = con().execute(BASE + " AND c.validacao IN ('Validado', 'Por validar')").fetchall()
    pai = {}

    def raiz(x):
        pai.setdefault(x, x)
        while pai[x] != x:
            x = pai[x]
        return x
    for r in rows:
        for outra in (r["duplicado_de"] or "").split(","):
            if outra.strip():
                a, b = raiz(r["id_submissao"]), raiz(outra.strip())
                if a != b:
                    pai[max(a, b)] = min(a, b)
    pessoas = {}
    for r in sorted(rows, key=lambda r: r["criada_em"]):
        pessoas.setdefault(raiz(r["id_submissao"]), []).append(r)
    lista_p = []
    for linhas in pessoas.values():
        linhas.sort(key=lambda r: (r["horario_entrevista"] is None,
                                   agenda.para_minutos(r["horario_entrevista"]) if r["horario_entrevista"] else 0))
        lista_p.append({"nome": linhas[0]["cand_nome"], "telefone": linhas[0]["cand_telefone"], "linhas": linhas,
                        "empresas": [{"empresa": r["empresa"], "hora": hora_do_dia(r, espera)}
                                     for r in linhas]})
    lista_p.sort(key=lambda p: _chave(p["nome"]))
    return lista_p


def por_validar():
    return con().execute(f"SELECT count(*) FROM candidaturas c JOIN submissoes s ON s.id_submissao = c.id_submissao "
                         f"WHERE {agenda.ACTIVAS} AND c.validacao = 'Por validar'").fetchone()[0]


@app.get("/listagens/rececao-empresas")
@perfis("coordenador")
def listagem_rececao_empresas():
    folhas = agendas_para_listagem()
    registar("listagem", detalhe="receção por empresa")
    return render_template("listagem_empresas.html", folhas=folhas, titulo="Receção de candidatos · por empresa e horário",
                           tecnicos_col=False, pendentes=por_validar(), gerada=agora().astimezone(bd.TZ))


@app.get("/listagens/tecnicos")
@perfis("coordenador")
def listagem_tecnicos():
    folhas = agendas_para_listagem()
    registar("listagem", detalhe="agendas para os técnicos")
    return render_template("listagem_empresas.html", folhas=folhas, titulo="Agenda da empresa",
                           tecnicos_col=True, pendentes=por_validar(), gerada=agora().astimezone(bd.TZ))


@app.get("/listagens/rececao-candidatos")
@perfis("coordenador")
def listagem_rececao_candidatos():
    pessoas = candidatos_para_rececao()
    registar("listagem", detalhe="receção por candidato")
    n_col = max([3] + [len(p["empresas"]) for p in pessoas])
    return render_template("listagem_candidatos.html", pessoas=pessoas, n_col=n_col, pendentes=por_validar(),
                           gerada=agora().astimezone(bd.TZ))


# ── Check-in na receção, no dia da Feira (7 Out 2026) ─────────────────────
# Como a listagem «Receção · por candidato», mas para marcar no tablet quem
# chegou e que entrevistas fez, e acertar a empresa e a hora no próprio dia.
# Fica na tabela rececao: as candidaturas, a agenda e a sincronização não mudam.

HORA = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
AVULSO = "avulso-"       # id_submissao, na tabela rececao, dos candidatos sem inscrição


def pessoas_na_rececao():
    """As pessoas de candidatos_para_rececao(), com o check-in de cada candidatura."""
    reg = {(r["id_submissao"], r["empresa"]): r for r in con().execute("SELECT * FROM rececao")}
    espera = agenda.posicoes_em_espera(con())
    pessoas = candidatos_para_rececao()
    # Os candidatos sem inscrição, registados na própria receção (13 Out 2026)
    pessoas += [{"id": f"{AVULSO}{a['id']}", "nome": a["nome"], "telefone": a["telefone"] or "", "linhas": [],
                 "empresas": [], "avulso": True} for a in con().execute("SELECT * FROM rececao_avulsos")]
    pessoas.sort(key=lambda p: _chave(p["nome"]))
    nomes_empresas = {e["empresa"] for e in empresas()}
    for p in pessoas:
        if not p.get("avulso"):
            p["id"] = p["linhas"][0]["id_submissao"]
        sids = {r["id_submissao"] for r in p["linhas"]} | {p["id"]}
        p["cands"] = []
        for r in p["linhas"]:
            x = reg.get((r["id_submissao"], r["empresa"]))
            p["cands"].append({
                "chave": f"{r['id_submissao']}|{r['empresa']}", "empresa": r["empresa"],
                "hora": hora_do_dia(r, espera), "por_validar": r["validacao"] == POR_VALIDAR,
                "presente": bool(x and x["presente"]), "entrevistado": bool(x and x["entrevistado"]),
                "faltou": bool(x and x["faltou"]), "empresa_dia": (x["empresa_dia"] if x else None) or r["empresa"],
                "horario_dia": (x["horario_dia"] if x else None) or r["horario_entrevista"] or ""})
        for (sid, emp), x in sorted(reg.items(), key=lambda kv: kv[1]["alterado_em"]):
            if x["extra"] and sid in sids:
                p["cands"].append({
                    "chave": f"{sid}|{emp}", "empresa": emp, "hora": "Extra", "por_validar": False, "extra": True,
                    "presente": bool(x["presente"]), "entrevistado": bool(x["entrevistado"]),
                    "faltou": bool(x["faltou"]), "empresa_dia": x["empresa_dia"] or emp,
                    "horario_dia": x["horario_dia"] or ""})
        p["presente"] = any(c["presente"] for c in p["cands"])
        p["faltou"] = not p["presente"] and any(c["faltou"] for c in p["cands"])
        # Sem limite de entrevistas no dia (13 Out 2026): com as faltas, há quem
        # faça excepcionalmente mais de três. Só não repete empresa.
        p["pode_extra"] = bool(nomes_empresas - {c["empresa"] for c in p["cands"]})
    return pessoas


def resumo_rececao(pessoas):
    cands = [c for p in pessoas for c in p["cands"]]
    return {"pessoas": len(pessoas), "presentes": sum(1 for p in pessoas if p["presente"]),
            "entrevistas": len(cands), "feitas": sum(1 for c in cands if c["entrevistado"])}


def analise_rececao(pessoas):
    """A fotografia do dia para os gráficos e o relatório PDF (14 Out 2026): quem
    compareceu (inscritos), quem veio sem inscrição e quem não compareceu, com as
    entrevistas de cada um (feitas, não feitas e acrescentadas no dia) e a
    contagem por empresa onde a entrevista decorreu (a «empresa no dia»)."""
    presentes = [p for p in pessoas if p["presente"] and not p.get("avulso")]
    avulsos = [p for p in pessoas if p.get("avulso")]
    ausentes = [p for p in pessoas if not p["presente"] and not p.get("avulso")]
    por_emp = {}

    def linha(emp):
        return por_emp.setdefault(emp, {"empresa": emp, "feitas": 0, "extra": 0, "nao_feitas": 0, "faltas": 0})
    for p in pessoas:
        for c in p["cands"]:
            x = linha(c["empresa_dia"])
            if not p["presente"]:
                x["faltas"] += 1
            elif c["entrevistado"]:
                x["extra" if c.get("extra") else "feitas"] += 1
            else:
                x["nao_feitas"] += 1
    por_empresa = sorted(por_emp.values(), key=lambda x: (-(x["feitas"] + x["extra"]), _chave(x["empresa"])))
    for x in por_empresa:
        x["total"] = x["feitas"] + x["extra"] + x["nao_feitas"] + x["faltas"]
    feitas = sum(x["feitas"] + x["extra"] for x in por_empresa)
    inscritos = len(presentes) + len(ausentes)
    return {
        "presentes": presentes, "avulsos": avulsos, "ausentes": ausentes, "por_empresa": por_empresa,
        "inscritos": inscritos, "n_presentes": len(presentes), "n_ausentes": len(ausentes), "n_avulsos": len(avulsos),
        "pct_presentes": round(100 * len(presentes) / inscritos) if inscritos else 0,
        "feitas": feitas, "extra": sum(x["extra"] for x in por_empresa),
        "nao_feitas": sum(x["nao_feitas"] for x in por_empresa),
        "media": round(feitas / (len(presentes) + len(avulsos)), 1) if presentes or avulsos else 0,
        "max_empresa": max([x["total"] for x in por_empresa] + [1]),
    }


@app.get("/rececao")
@perfis("coordenador", "rececao")
def rececao():
    pessoas = pessoas_na_rececao()
    registar("rececao", detalhe=f"{len(pessoas)} candidatos")
    return render_template("rececao.html", pessoas=pessoas, resumo=resumo_rececao(pessoas),
                           analise=analise_rececao(pessoas) if g.token["perfil"] == "coordenador" else None,
                           empresas=empresas(), ok=request.args.get("ok"), erro=request.args.get("erro"),
                           fecho=fecho_feito(), envio=estado_envio())


@app.get("/rececao/relatorio")
@perfis("coordenador")
def rececao_relatorio():
    """Relatório do dia para imprimir ou guardar em PDF (A4 horizontal), com o
    cabeçalho e o rodapé da Feira: gráficos, presentes, inscritos no dia e faltas."""
    pessoas = pessoas_na_rececao()
    registar("listagem", detalhe="relatório da receção")
    return render_template("rececao_relatorio.html", analise=analise_rececao(pessoas), pendentes=0,
                           gerada=agora().astimezone(bd.TZ), fecho=fecho_feito())


@app.post("/rececao")
@perfis("coordenador", "rececao")
def rececao_gravar():
    """Grava o check-in de uma pessoa: presente (em todas as suas candidaturas)
    e, por candidatura, entrevista feita, empresa e hora do dia."""
    chaves = request.form.getlist("cand")
    validas = {f"{r['id_submissao']}|{r['empresa']}" for r in con().execute(
        BASE + " AND c.validacao IN ('Validado', 'Por validar')").fetchall()}
    validas |= {f"{r['id_submissao']}|{r['empresa']}" for r in con().execute(
        "SELECT id_submissao, empresa FROM rececao WHERE extra = 1")}
    nomes_empresas = {e["empresa"] for e in empresas()}
    erro = None
    if not chaves or not set(chaves) <= validas:
        erro = "Este candidato já não está na lista (anulado ou removido). Recarregue a página."
    novos = []
    for i, chave in enumerate(chaves if erro is None else []):
        sid, emp = chave.split("|", 1)
        emp_dia = request.form.get(f"empresa_dia:{i}") or emp
        hora_dia = (request.form.get(f"horario_dia:{i}") or "").strip()[:5]
        if emp_dia not in nomes_empresas or (hora_dia and not HORA.match(hora_dia)):
            erro = "Empresa ou hora inválida."
            break
        novos.append((sid, emp, 1 if request.form.get("presente") else 0,
                      1 if request.form.get(f"entrevistado:{i}") else 0,
                      None if emp_dia == emp else emp_dia, hora_dia or None))
    if any(n[3] for n in novos):        # quem fez uma entrevista esteve presente
        novos = [(sid, emp, 1, *resto) for sid, emp, _p, *resto in novos]
    fetch = request.headers.get("X-Pedido") == "fetch"
    if erro:
        if fetch:
            return {"ok": False, "erro": erro}, 400
        return redirect(url_for("rececao", erro=erro))
    c = con()
    c.execute("BEGIN IMMEDIATE")
    try:
        for sid, emp, presente, entrevistado, emp_dia, hora_dia in novos:
            c.execute(
                "INSERT INTO rececao (id_submissao, empresa, presente, entrevistado, empresa_dia, horario_dia, "
                "alterado_em, alterado_por) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id_submissao, empresa) DO UPDATE SET presente = excluded.presente, "
                "faltou = CASE WHEN excluded.presente = 1 THEN 0 ELSE rececao.faltou END, "
                "entrevistado = excluded.entrevistado, empresa_dia = excluded.empresa_dia, "
                "horario_dia = excluded.horario_dia, alterado_em = excluded.alterado_em, "
                "alterado_por = excluded.alterado_por",
                (sid, emp, presente, entrevistado, emp_dia, hora_dia, bd.agora_utc(), g.token["nome"]))
            registar("rececao-alterar", id_submissao=sid, empresa=emp,
                     detalhe=f"presente={presente}; entrevista={entrevistado}"
                             + (f"; empresa_dia={emp_dia}" if emp_dia else "") + (f"; hora={hora_dia}" if hora_dia else ""))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    if fetch:
        return {"ok": True, "resumo": resumo_rececao(pessoas_na_rececao()),
                "hora": agora().astimezone(bd.TZ).strftime("%H:%M")}
    return redirect(url_for("rececao", ok=1) + f"#p-{novos[0][0]}")


@app.post("/rececao/novo")
@perfis("coordenador", "rececao")
def rececao_novo():
    """Regista um candidato que chegou sem inscrição (13 Out 2026), já presente,
    com a primeira entrevista; as seguintes acrescentam-se em «Nova entrevista»."""
    nome = " ".join((request.form.get("nome") or "").split())
    telefone = (request.form.get("telefone") or "").strip()
    empresa = request.form.get("empresa") or ""
    hora = (request.form.get("hora") or "").strip()[:5]
    if not nome or len(nome) > 120 or len(telefone) > 30:
        erro = "Indique o nome do candidato (até 120 caracteres; telefone até 30)."
    elif empresa not in {e["empresa"] for e in empresas()} or (hora and not HORA.match(hora)):
        erro = "Escolha a empresa da entrevista (e uma hora válida, se a indicar)."
    elif any(_chave(p["nome"]) == _chave(nome) for p in pessoas_na_rececao()):
        erro = (f"Já há «{nome}» na lista: procure-o e use «Nova entrevista». Se for outra pessoa com o mesmo "
                "nome, acrescente um apelido para os distinguir.")
    else:
        erro = None
    if erro:
        return redirect(url_for("rececao", erro=erro, novo=1))
    c = con()
    agora_iso = bd.agora_utc()
    c.execute("BEGIN IMMEDIATE")
    try:
        cur = c.execute("INSERT INTO rececao_avulsos (nome, telefone, criado_em, criado_por) VALUES (?, ?, ?, ?)",
                        (nome, telefone or None, agora_iso, g.token["nome"]))
        sid = f"{AVULSO}{cur.lastrowid}"
        # Chegou: fica presente e, como os outros, com a entrevista já marcada como feita.
        c.execute("INSERT INTO rececao (id_submissao, empresa, presente, entrevistado, extra, horario_dia, alterado_em, "
                  "alterado_por) VALUES (?, ?, 1, 1, 1, ?, ?, ?)", (sid, empresa, hora or None, agora_iso, g.token["nome"]))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    registar("rececao-novo", id_submissao=sid, empresa=empresa, detalhe="candidato sem inscrição"
             + (f"; hora={hora}" if hora else ""))
    return redirect(url_for("rececao", ok=1) + f"#p-{sid}")


@app.post("/rececao/extra")
@perfis("coordenador", "rececao")
def rececao_extra():
    """Acrescenta no dia uma entrevista numa empresa onde o candidato ainda não
    tem nenhuma, mesmo que já tenha três. Não mexe nas candidaturas."""
    sid = request.form.get("id") or ""
    empresa = request.form.get("empresa") or ""
    hora = (request.form.get("hora") or "").strip()[:5]
    pessoa = next((p for p in pessoas_na_rececao() if p["id"] == sid), None)
    if pessoa is None:
        erro = "Este candidato já não está na lista. Recarregue a página."
    elif empresa not in {e["empresa"] for e in empresas()} or (hora and not HORA.match(hora)):
        erro = "Empresa ou hora inválida."
    elif empresa in {c["empresa"] for c in pessoa["cands"]}:
        erro = "Este candidato já tem entrevista nessa empresa."
    else:
        erro = None
    if erro:
        return redirect(url_for("rececao", erro=erro) + (f"#p-{sid}" if pessoa else ""))
    c = con()
    c.execute("INSERT INTO rececao (id_submissao, empresa, presente, entrevistado, extra, horario_dia, alterado_em, "
              "alterado_por) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
              (sid, empresa, int(pessoa["presente"]), int(pessoa["presente"]), hora or None, bd.agora_utc(),
               g.token["nome"]))
    registar("rececao-extra", id_submissao=sid, empresa=empresa, detalhe="entrevista acrescentada no dia"
             + (f"; hora={hora}" if hora else ""))
    return redirect(url_for("rececao", ok=1) + f"#p-{sid}")



# ── Fecho do dia na receção (13 Out 2026) ─────────────────────────────────
# Depois da Feira: quem não foi marcado «Presente» passa a «Não compareceu» e
# cada técnico de entidade recebe um ponto de situação final dos candidatos que
# inscreveu (as faltas, para os poder contactar, e as entrevistas feitas,
# incluindo as acrescentadas no dia), com a apresentação do seguimento no seu
# dashboard. O conteúdo fica em fecho_emails no momento da confirmação, e o
# cron (correio.py fecho) envia-o. Faz-se uma vez.

def fecho_feito():
    return con().execute("SELECT * FROM rececao_fecho ORDER BY id DESC LIMIT 1").fetchone()


def estado_envio():
    r = con().execute("SELECT count(*) AS n, sum(enviado_em IS NOT NULL) AS enviados, "
                      "sum(enviado_em IS NULL AND tentativas >= ?) AS falhados FROM fecho_emails",
                      (correio.TENTATIVAS_FECHO,)).fetchone()
    n, enviados, falhados = r["n"] or 0, r["enviados"] or 0, r["falhados"] or 0
    return {"n": n, "enviados": enviados, "falhados": falhados, "por_enviar": n - enviados - falhados}


def _entrevista_do_dia(c, presente):
    """Uma entrevista no e-mail final: (classe, texto)."""
    empresa = c["empresa_dia"] or c["empresa"]
    hora = c["horario_dia"] or ("" if c["hora"] in ("Extra", POR_VALIDAR) or c["hora"].startswith("Espera")
                                else c["hora"])
    if not presente:
        h = c["hora"]
        prevista = ("" if h == "Extra" else "lista de espera" if h.startswith("Espera")
                    else "inscrição por validar" if h == POR_VALIDAR else f"às {h}")
        return {"empresa": empresa, "classe": "faltou", "texto": prevista}
    if c["entrevistado"]:
        return {"empresa": empresa, "classe": "extra" if c.get("extra") else "feita",
                "texto": ("Entrevista feita, acrescentada no dia" if c.get("extra") else "Entrevista feita")
                         + (f" ({hora})" if hora else "")}
    return {"empresa": empresa, "classe": "nao-feita", "texto": "Não fez esta entrevista"}


def fecho_por_tecnico(pessoas=None):
    """O ponto de situação final de cada técnico de entidade: os seus candidatos
    que não compareceram e, dos que compareceram, as entrevistas feitas (com as
    acrescentadas no dia) e as que ficaram por fazer. Os candidatos sem inscrição
    não têm técnico e não entram."""
    pessoas = pessoas if pessoas is not None else pessoas_na_rececao()
    subs = {r["id_submissao"]: r for r in con().execute(
        "SELECT id_submissao, tecnico_id, cand_email, COALESCE(tecnico_canon, entidade_tecnico) AS tecnico, "
        "COALESCE(tecnico_email_canon, lower(trim(entidade_email))) AS email, "
        "COALESCE(entidade_canon, entidade_nome) AS entidade FROM submissoes")}
    correcoes = correio.correcoes_email()
    grupos = {}
    for p in pessoas:
        if p.get("avulso"):
            continue
        por_tec = {}
        for c in p["cands"]:
            s = subs.get(c["chave"].split("|", 1)[0])
            ref = s and (s["tecnico_id"] or s["email"])
            if ref:
                por_tec.setdefault(ref, (s, []))[1].append(c)
        for ref, (s, cands) in por_tec.items():
            email = (s["email"] or "").strip().lower()
            gr = grupos.setdefault(ref, {"ref": ref, "tecnico": s["tecnico"] or "", "email": correcoes.get(email, email),
                                         "entidade": s["entidade"] or "", "faltaram": [], "presentes": []})
            item = {"nome": p["nome"], "telefone": p["telefone"] or "",
                    "email": (s["cand_email"] or "").strip().lower(),
                    "entrevistas": [_entrevista_do_dia(c, p["presente"]) for c in cands]}
            gr["presentes" if p["presente"] else "faltaram"].append(item)
    for gr in grupos.values():
        feitas = [e for x in gr["presentes"] for e in x["entrevistas"]]
        gr["resumo"] = {"presentes": len(gr["presentes"]), "faltaram": len(gr["faltaram"]),
                        "feitas": sum(1 for e in feitas if e["classe"] in ("feita", "extra")),
                        "extra": sum(1 for e in feitas if e["classe"] == "extra"),
                        "nao_feitas": sum(1 for e in feitas if e["classe"] == "nao-feita")}
        gr["email_ok"] = bool(correio.EMAIL_OK.match(gr["email"]))
    return sorted(grupos.values(), key=lambda gr: (_chave(gr["entidade"]), _chave(gr["tecnico"])))


def contexto_fecho(gr):
    return {"g": gr, "nome": correio.primeiro_nome(gr["tecnico"]), "validade": correio.VALIDADE_SEGUIMENTO,
            "base_url": correio.BASE_URL}


@app.get("/rececao/fecho")
@perfis("coordenador", "rececao")
def rececao_fecho():
    fecho = fecho_feito()
    if fecho:
        envios = con().execute("SELECT id, ref, email, nome, entidade, enviado_em, tentativas, resultado "
                               "FROM fecho_emails WHERE fecho = ? ORDER BY entidade, nome", (fecho["id"],)).fetchall()
        registar("rececao-fecho", detalhe="estado do envio")
        return render_template("rececao_fecho.html", fecho=fecho, envios=envios, envio=estado_envio(),
                               tentativas=correio.TENTATIVAS_FECHO, ok=request.args.get("ok"))
    pessoas = pessoas_na_rececao()
    grupos = fecho_por_tecnico(pessoas)
    faltas = [p for p in pessoas if not p["presente"]]
    registar("rececao-fecho", detalhe="pré-visualização")
    return render_template("rececao_fecho.html", fecho=None, grupos=grupos, faltas=faltas,
                           resumo=resumo_rececao(pessoas), erro=request.args.get("erro"))


@app.get("/rececao/fecho/email/<path:ref>")
@perfis("coordenador", "rececao")
def rececao_fecho_email(ref):
    """O e-mail como o técnico o vai receber (antes do fecho) ou recebeu (depois)."""
    fecho = fecho_feito()
    if fecho:
        row = con().execute("SELECT * FROM fecho_emails WHERE ref = ? ORDER BY id DESC LIMIT 1", (ref,)).fetchone()
        gr = json.loads(row["dados"]) if row else None
    else:
        gr = next((x for x in fecho_por_tecnico() if x["ref"] == ref), None)
    if gr is None:
        abort(404)
    html = correio._env.get_template("fecho.html").render(assunto=correio.ASSUNTO_FECHO, **contexto_fecho(gr))
    for cid, ficheiro in correio.IMAGENS:
        html = html.replace(f"cid:{cid}@fe2026", url_for("static", filename=ficheiro))
    registar("rececao-fecho", detalhe=f"e-mail de {gr['tecnico']}")
    # Os e-mails levam os estilos em linha: só esta página os aceita.
    g.csp = ("default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'; "
             "form-action 'none'; base-uri 'none'")
    return Response(html, mimetype="text/html")


@app.post("/rececao/fecho")
@perfis("coordenador", "rececao")
def rececao_fechar():
    if request.form.get("confirmo") != "1":
        return redirect(url_for("rececao_fecho", erro="Confirme, na caixa, que a Feira terminou."))
    c = con()
    c.execute("BEGIN IMMEDIATE")
    try:
        if fecho_feito():
            c.execute("ROLLBACK")
            return redirect(url_for("rececao_fecho"))
        pessoas = pessoas_na_rececao()
        agora_iso = bd.agora_utc()
        faltas = 0
        for p in pessoas:
            if p["presente"]:
                continue
            faltas += 1
            for x in p["cands"]:
                sid, emp = x["chave"].split("|", 1)
                c.execute("INSERT INTO rececao (id_submissao, empresa, presente, entrevistado, faltou, alterado_em, "
                          "alterado_por) VALUES (?, ?, 0, 0, 1, ?, ?) ON CONFLICT(id_submissao, empresa) DO UPDATE "
                          "SET presente = 0, entrevistado = 0, faltou = 1, alterado_em = excluded.alterado_em, "
                          "alterado_por = excluded.alterado_por", (sid, emp, agora_iso, g.token["nome"]))
        grupos = [gr for gr in fecho_por_tecnico(pessoas_na_rececao()) if gr["email_ok"]]
        cur = c.execute("INSERT INTO rececao_fecho (em, por, faltas, emails) VALUES (?, ?, ?, ?)",
                        (agora_iso, g.token["nome"], faltas, len(grupos)))
        for gr in grupos:
            c.execute("INSERT INTO fecho_emails (fecho, ref, email, nome, entidade, dados) VALUES (?, ?, ?, ?, ?, ?)",
                      (cur.lastrowid, gr["ref"], gr["email"], gr["tecnico"], gr["entidade"],
                       json.dumps(gr, ensure_ascii=False)))
        registar("rececao-fecho", detalhe=f"{faltas} não compareceram; {len(grupos)} e-mails aos técnicos")
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    return redirect(url_for("rececao_fecho", ok=1))


@app.get("/rececao.csv")
@perfis("coordenador")
def rececao_csv():
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Candidato/a", "Telefone", "Presente", "Empresa da candidatura", "Hora marcada", "Empresa no dia",
                "Hora no dia", "Entrevista feita", "Sem inscrição"])
    pessoas = pessoas_na_rececao()
    for p in pessoas:
        for c in p["cands"]:
            w.writerow([p["nome"], p["telefone"], "Sim" if p["presente"] else "Não", c["empresa"], c["hora"],
                        c["empresa_dia"], c["horario_dia"], "Sim" if c["entrevistado"] else "Não",
                        "Sim" if p.get("avulso") else "Não"])
    registar("rececao", detalhe=f"csv, {len(pessoas)} candidatos")
    data = agora().astimezone(bd.TZ).strftime("%Y%m%d-%H%M")
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="FE2026-rececao-{data}.csv"'})


LIMITE_GRUPO = 3   # grupos com menos pessoas juntam-se em «Outros» (dados anonimizados)


def contagem(sql, args=()):
    """[{rotulo, valor}] de um SELECT rótulo, n; os grupos pequenos juntam-se em «Outros»."""
    linhas = [(r[0] or "Sem indicação", r[1]) for r in con().execute(sql, args)]
    grandes = [{"rotulo": r, "valor": n} for r, n in linhas if n >= LIMITE_GRUPO]
    resto = sum(n for _r, n in linhas if n < LIMITE_GRUPO)
    grandes.sort(key=lambda x: -x["valor"])
    return grandes + ([{"rotulo": "Outros", "valor": resto}] if resto else [])


def estatisticas_gerais():
    """Números agregados e anónimos para a análise e a preparação da edição seguinte:
    sem nomes de pessoas, sem contactos e sem textos livres. Os grupos com menos de
    LIMITE_GRUPO pessoas juntam-se (habilitações, localidades)."""
    ACT = f"FROM candidaturas c JOIN submissoes s ON s.id_submissao = c.id_submissao WHERE {agenda.ACTIVAS}"
    estados = dict(con().execute(f"SELECT c.validacao, count(*) {ACT} GROUP BY c.validacao").fetchall())
    n_sub = con().execute("SELECT count(*) FROM submissoes WHERE removida = 0").fetchone()[0]
    por_empresa = con().execute(
        f"SELECT e.empresa, e.capacidade_maxima AS cap, "
        f"sum(c.validacao = 'Validado') AS validadas, sum(c.validacao = 'Por validar') AS por_validar, "
        f"sum(c.validacao = 'Anulado') AS anuladas, count(*) AS total "
        f"FROM candidaturas c JOIN submissoes s ON s.id_submissao = c.id_submissao "
        f"JOIN empresas e ON e.empresa = c.empresa WHERE {agenda.ACTIVAS} GROUP BY e.empresa "
        f"ORDER BY validadas DESC, e.empresa").fetchall()
    seg = f"FROM seguimento sg JOIN candidaturas c ON c.id_submissao = sg.id_submissao AND c.empresa = sg.empresa " \
          f"JOIN submissoes s ON s.id_submissao = c.id_submissao WHERE {agenda.ACTIVAS} AND c.validacao = 'Validado'"
    seguimento = {"linhas": con().execute(f"SELECT count(*) {seg}").fetchone()[0],
                  "entrevistados": con().execute(f"SELECT count(*) {seg} AND sg.nova_entrevista IS NOT NULL").fetchone()[0],
                  "nova_entrevista": con().execute(f"SELECT count(*) {seg} AND sg.nova_entrevista = 'Sim'").fetchone()[0],
                  "selecionados": con().execute(f"SELECT count(*) {seg} AND sg.selecionado = 'Sim'").fetchone()[0],
                  "contratados": con().execute(f"SELECT count(*) {seg} AND sg.contratado = 'Sim'").fetchone()[0],
                  "validadas": estados.get("Validado", 0)}
    pessoas = pessoas_na_rececao()
    # A avaliação das empresas e a dos parceiros: só números
    av = con().execute("SELECT * FROM avaliacao").fetchall()
    medias_emp = {}
    for k, _ in PERGUNTAS:
        notas_q = [a[k] for a in av if a[k]]
        medias_emp[k] = {"media": sum(notas_q) / len(notas_q) if notas_q else None,
                         "dist": [sum(1 for n in notas_q if n == v) for v, _ in ESCALA]}
    avf = con().execute("SELECT * FROM avaliacao_feira").fetchall()
    medias_org = medias_feira(avf)
    return {
        "n_candidatos": n_sub, "estados": estados, "n_candidaturas": sum(estados.values()),
        "por_empresa": por_empresa,
        "n_empresas": con().execute("SELECT count(*) FROM empresas WHERE removida = 0").fetchone()[0],
        "por_entidade": contagem("SELECT COALESCE(entidade_canon, entidade_nome), count(*) FROM submissoes "
                                 "WHERE removida = 0 GROUP BY 1"),
        "por_n_empresas": [{"rotulo": f"{n} empresa{'s' if n != 1 else ''}", "valor": v} for n, v in con().execute(
            "SELECT n_empresas, count(*) FROM submissoes WHERE removida = 0 GROUP BY 1 ORDER BY 1")],
        "habilitacoes": contagem("SELECT cand_habilitacoes, count(*) FROM submissoes WHERE removida = 0 GROUP BY 1"),
        "localidades": contagem("SELECT cand_localidade, count(*) FROM submissoes WHERE removida = 0 GROUP BY 1"),
        "contratos": contagem(f"SELECT sg.tipo_contrato, count(*) {seg} AND sg.contratado = 'Sim' GROUP BY 1"),
        "seguimento": seguimento, "rececao": analise_rececao(pessoas),
        "av_emp": {"n": len(av), "n_empresas": len({a["empresa"] for a in av}),
                   "itens": itens_grafico(PERGUNTAS, medias_emp), "resumo": resumo_notas(medias_emp, topo=4)},
        "av_org": {"n": len(avf), "itens": itens_grafico(PERGUNTAS_FEIRA, medias_org),
                   "resumo": resumo_notas(medias_org, topo=5),
                   "grupos": [(nome, sum(1 for a in avf if a["grupo"] == k)) for k, nome in GRUPOS_FEIRA]},
    }


# Respostas que não dizem nada («NA», «xx»…) não contam como preenchidas
SEM_CONTEUDO = {"x", "xx", "na", "n/a", "nada", "nada a referir", "nao senti", "nada a apontar"}


def preenchido(texto):
    t = re.sub(r"[^\w/ ]", "", unicodedata.normalize("NFD", texto or "").encode("ascii", "ignore").decode()).strip().lower()
    return bool(t) and t not in SEM_CONTEUDO


def preenchimento_textos():
    """Quantas respostas preencheram as perguntas abertas das duas avaliações. Só números:
    sem textos, nomes nem grupos (a coordenação tem poucas pessoas)."""
    def conta(linhas, campos, rotulos):
        feitas = [[preenchido(a[k]) for k in campos] for a in linhas]
        return {"n": len(linhas), "alguma": sum(1 for f in feitas if any(f)), "todas": sum(1 for f in feitas if all(f)),
                "nenhuma": sum(1 for f in feitas if not any(f)),
                "por_pergunta": [(r, sum(f[i] for f in feitas)) for i, r in enumerate(rotulos)]}
    emp = con().execute("SELECT * FROM avaliacao").fetchall()
    org = con().execute("SELECT * FROM avaliacao_feira").fetchall()
    # nas empresas, a explicação de uma nota baixa também conta como texto livre
    emp_c = conta(emp, [k for k, _ in TEXTOS_AVALIACAO], [r for _, r in TEXTOS_AVALIACAO])
    com_obs_so = sum(1 for a in emp if not any(preenchido(a[k]) for k, _ in TEXTOS_AVALIACAO) and a["obs_notas"])
    emp_c["alguma"] += com_obs_so
    emp_c["nenhuma"] -= com_obs_so
    return {"emp": emp_c,
            "org": conta(org, [k for k, *_ in TEXTOS_FEIRA], [r for _, r, *_ in TEXTOS_FEIRA])}


@app.get("/estatisticas")
def estatisticas():
    """Página de números para a análise, aberta a todos os perfis: só dados agregados.
    O resumo dos comentários livres é só da coordenação e dos técnicos de triagem."""
    registar("estatisticas")
    ver_comentarios = g.token["perfil"] in ("coordenador", "tecnico")
    return render_template("estatisticas.html", d=estatisticas_gerais(), escala=ESCALA,
                           escala_feira=[e for e in ESCALA_FEIRA if e[0]],
                           ver_empresas=g.token["perfil"] != "empresa", ver_comentarios=ver_comentarios,
                           txt=preenchimento_textos() if ver_comentarios else None)


@app.get("/sair")
def saida():
    resp = make_response(render_template("erro.html", titulo="Sessão terminada",
                                         mensagem="Para voltar a entrar, use de novo o link que recebeu."))
    resp.delete_cookie(COOKIE)
    return resp


if os.path.exists(bd.DB_PATH):
    _c = bd.ligar()
    bd.garantir_schema(_c)
    _c.close()
