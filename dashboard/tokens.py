#!/usr/bin/env python3
"""Links de acesso ao dashboard FE2026 (um token por pessoa ou empresa).

O token só é mostrado uma vez, ao ser criado: na BD fica apenas o seu hash.

    tokens.py criar --perfil coordenador --nome "Nome Apelido"
    tokens.py criar --perfil tecnico --id T01 --nome "Ana Silva"
    tokens.py criar --perfil empresa --empresa "LIDL"
    tokens.py criar --perfil entidade --email tecnico@entidade.pt
    tokens.py criar --perfil rececao --nome "Receção 1"   # check-in no dia da Feira (tablet)
    tokens.py criar-empresas                 # um link para cada empresa que ainda não tenha
    tokens.py criar-entidades [--simular]    # um link para cada técnico de entidade que ainda não tenha
    tokens.py listar
    tokens.py revogar <prefixo>
    tokens.py prolongar --perfil coordenador --ate "2027-01-11 23:59"
    tokens.py prolongar --perfil tecnico --ate "2026-11-30 23:59" --incluir-expirados

Validade por omissão: técnicos validadores e técnicos das entidades até ao
fim da semana do evento (18 Out 2026). Coordenação até 90 dias depois da Feira
(11 Jan 2027, prolongado a 6 Out 2026: acompanha o seguimento das empresas).
Empresas até 90 dias
depois da Feira (11 Jan 2027), para preencherem o seguimento dos candidatos
(6 Out 2026; antes era até ao dia do evento, §5.1.3); os CV deixam de lhes ser
servidos uma semana depois da Feira (FE2026_CV_EMPRESAS_ATE, em app.py).

O link de uma entidade é por técnico: as várias grafias do nome, e-mail ou
telefone do mesmo técnico já vêm juntas pela sincronização (entidades.py).
"""
import argparse
import os
import secrets
import sys
from datetime import datetime, timezone

import bd

BASE_URL = os.environ.get("FE2026_BASE_URL", "http://127.0.0.1:8080")
EXPIRA = {
    "empresa": "2027-01-11 23:59",
    "coordenador": "2027-01-11 23:59",
    "tecnico": "2026-10-18 23:59",
    "entidade": "2027-01-11 23:59",    # seguimento dos candidatos depois da Feira (13 Out 2026)
    "rececao": "2026-10-14 23:59",     # só serve no dia da Feira (7 Out 2026)
}


def expira_utc(local):
    return (datetime.strptime(local, "%Y-%m-%d %H:%M").replace(tzinfo=bd.TZ)
            .astimezone(timezone.utc).isoformat(timespec="seconds"))


def criar(con, perfil, nome, id_tecnico=None, empresa=None, expira=None, ref_submissao=None, email=None):
    token = secrets.token_urlsafe(24)
    con.execute(
        "INSERT INTO tokens (token_hash, prefixo, perfil, nome, id_tecnico, empresa, criado_em, expira_em, "
        "ref_submissao, email) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (bd.hash_token(token), token[:6], perfil, nome, id_tecnico, empresa, bd.agora_utc(),
         expira_utc(expira or EXPIRA[perfil]), ref_submissao, email),
    )
    return token


def tecnicos_de_entidades(con):
    """Um registo por técnico de entidade (submissoes.tecnico_id), com o nome,
    e-mail, telefone e entidade unificados, e se já tem link activo."""
    cobertos = {r[0] for r in con.execute(
        "SELECT s.tecnico_id FROM tokens t JOIN submissoes s ON s.id_submissao = t.ref_submissao "
        "WHERE t.perfil = 'entidade' AND t.revogado = 0 AND t.expira_em > ?", (bd.agora_utc(),))}
    rows = con.execute(
        "SELECT tecnico_id, tecnico_canon, tecnico_email_canon, tecnico_tel_canon, entidade_canon, count(*) AS n "
        "FROM submissoes WHERE removida = 0 AND tecnico_id IS NOT NULL "
        "GROUP BY tecnico_id ORDER BY entidade_canon, tecnico_canon").fetchall()
    return [dict(r, tem_link=r["tecnico_id"] in cobertos) for r in rows]


def main():
    p = argparse.ArgumentParser(description="Links de acesso ao dashboard FE2026")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("criar")
    c.add_argument("--perfil", choices=bd.PERFIS, required=True)
    c.add_argument("--nome")
    c.add_argument("--id", dest="id_tecnico", help="perfil tecnico: identificador curto, ex. T01")
    c.add_argument("--empresa", help="perfil empresa: nome exacto da empresa")
    c.add_argument("--email", help="perfil entidade: um e-mail usado pelo técnico no formulário")
    c.add_argument("--expira", help="'AAAA-MM-DD HH:MM', hora de Lisboa")
    sub.add_parser("criar-empresas")
    ce = sub.add_parser("criar-entidades")
    ce.add_argument("--simular", action="store_true", help="só mostra quem receberia link")
    sub.add_parser("listar")
    r = sub.add_parser("revogar")
    r.add_argument("prefixo")
    pr = sub.add_parser("prolongar", help="muda a validade dos links activos de um perfil")
    pr.add_argument("--perfil", choices=bd.PERFIS, required=True)
    pr.add_argument("--ate", required=True, help="'AAAA-MM-DD HH:MM', hora de Lisboa")
    pr.add_argument("--incluir-expirados", action="store_true",
                    help="também os já expirados (nunca os revogados): reactiva-os")
    a = p.parse_args()

    con = bd.ligar()
    bd.garantir_schema(con)
    if a.cmd == "criar":
        if a.perfil == "tecnico" and not a.id_tecnico:
            sys.exit("perfil tecnico precisa de --id")
        if a.perfil == "empresa":
            if not con.execute("SELECT 1 FROM empresas WHERE empresa = ?", (a.empresa,)).fetchone():
                sys.exit(f"empresa desconhecida: {a.empresa!r} (ver tokens.py listar ou a tabela empresas)")
            a.nome = a.nome or a.empresa
        ref = None
        if a.perfil == "entidade":
            s = con.execute("SELECT tecnico_id, tecnico_canon, tecnico_email_canon FROM submissoes "
                            "WHERE removida = 0 AND (lower(trim(entidade_email)) = lower(trim(?)) "
                            "OR tecnico_email_canon = lower(trim(?))) LIMIT 1", (a.email, a.email)).fetchone()
            if not a.email or s is None or s["tecnico_id"] is None:
                sys.exit(f"nenhuma inscrição com o e-mail {a.email!r} (ou a sincronização ainda não unificou)")
            ref, a.nome, a.email = s["tecnico_id"], a.nome or s["tecnico_canon"], s["tecnico_email_canon"]
        if not a.nome:
            sys.exit("falta --nome")
        token = criar(con, a.perfil, a.nome, a.id_tecnico, a.empresa if a.perfil == "empresa" else None, a.expira,
                      ref_submissao=ref, email=a.email if a.perfil == "entidade" else None)
        print(f"{a.nome} ({a.perfil}): {BASE_URL}/?t={token}")
    elif a.cmd == "criar-empresas":
        com = {r[0] for r in con.execute("SELECT empresa FROM tokens WHERE perfil = 'empresa' AND revogado = 0")}
        for (empresa,) in con.execute("SELECT empresa FROM empresas WHERE removida = 0 ORDER BY empresa").fetchall():
            if empresa not in com:
                print(f"{empresa}: {BASE_URL}/?t={criar(con, 'empresa', empresa, empresa=empresa)}")
    elif a.cmd == "criar-entidades":
        # Separado por tabulações: nome, entidade, e-mail, telefone, inscrições, link.
        for r in tecnicos_de_entidades(con):
            if r["tem_link"]:
                continue
            link = "(simulação)" if a.simular else f"{BASE_URL}/?t=" + criar(
                con, "entidade", r["tecnico_canon"] or "?", ref_submissao=r["tecnico_id"],
                email=r["tecnico_email_canon"])
            print("\t".join([r["tecnico_canon"] or "", r["entidade_canon"] or "", r["tecnico_email_canon"] or "",
                             r["tecnico_tel_canon"] or "", str(r["n"]), link]))
    elif a.cmd == "listar":
        for t in con.execute("SELECT * FROM tokens ORDER BY perfil, nome"):
            estado = "REVOGADO" if t["revogado"] else ("expirado" if t["expira_em"] <= bd.agora_utc() else "activo")
            print(f"{t['prefixo']}…  {t['perfil']:<11} {t['nome']:<45} {t['id_tecnico'] or t['email'] or '':<5} "
                  f"até {bd.para_local(t['expira_em'], '%d/%m %H:%M')}  {estado}  "
                  f"último acesso: {bd.para_local(t['ultimo_acesso']) or '—'}")
    elif a.cmd == "revogar":
        n = con.execute("UPDATE tokens SET revogado = 1 WHERE prefixo = ? AND revogado = 0", (a.prefixo,)).rowcount
        print(f"{n} link(s) revogado(s).")
    elif a.cmd == "prolongar":
        # Só os activos: um link revogado ou já expirado não volta a funcionar. Com
        # --incluir-expirados, um expirado volta (técnicos de triagem para a avaliação
        # da organização, 19 Out 2026); um revogado nunca.
        n = con.execute("UPDATE tokens SET expira_em = ? WHERE perfil = ? AND revogado = 0 AND expira_em > ?",
                        (expira_utc(a.ate), a.perfil, "" if a.incluir_expirados else bd.agora_utc())).rowcount
        print(f"{n} link(s) {a.perfil} válidos até {a.ate}.")


if __name__ == "__main__":
    main()
