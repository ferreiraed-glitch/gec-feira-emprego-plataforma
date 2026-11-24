#!/usr/bin/env python3
"""BD de demonstração do dashboard FE2026, para a formação dos técnicos
validadores (30 Set 2026) e, desde 13 Out 2026, para acompanhar o manual
unificado dos dashboards: um link por perfil (coordenação, técnico de triagem,
técnico da entidade, empresa e receção), com o check-in do dia, entrevistas
acrescentadas na receção, seguimento e avaliação já com exemplos. Só dados
fictícios. Os e-mails do fecho do dia nunca saem daqui: o envio só lê a BD de
produção.

Recria a BD de raiz a cada execução (repõe o estado inicial depois de uma
formação) e mostra os links de acesso, que só aparecem nesse momento.

    FE2026_DB=/var/lib/gec-fe2026-demo/demo.db python3 criar_demo.py [--base-url URL] [--cv caminho.pdf]

Recusa-se a escrever na BD de produção.
"""
import argparse
import os
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
sys.path[:0] = [RAIZ, os.path.join(RAIZ, "dashboard")]

import exportar_candidaturas as ex  # noqa: E402

PRODUCAO = "/var/lib/gec-fe2026/fe2026.db"
DB = os.environ.get("FE2026_DB", "")
if not DB or os.path.realpath(DB) == PRODUCAO:
    sys.exit("Defina FE2026_DB com o caminho da BD de demonstração (nunca a de produção).")

import bd  # noqa: E402
import tokens  # noqa: E402

ECI, STEAM, PLAT, HORTO = "EL CORTE INGLÉS", "STEAM", "PLATEFORM", "HORTO DO CAMPO GRANDE"

# Ofertas publicadas no formulário (informação pública das empresas).
OFERTAS = {
    "DOMINO'S PIZZA": "Driver (Part-Time) | Pizzeiro/a (PT) | Gestor de Turno",
    ECI: "Caixa de Supermercado (PT) | Repositor/a Supermercado | Perecíveis de supermercado | "
         "Repositor/Moço de Piso | Repositor de Brinquedos | Repositor/a de Adornos de Natal",
    "HORAS DE SONHO": "BabySitters",
    HORTO: "Auxiliar de jardineiro | Jardineiro/a",
    "INDITEX": "ARMAZÉM (apenas com atestado multiusos)",
    "LIDL": "Operador/a loja (atestado multiusos)",
    "MERISTEMA": "Auxiliar de Jardinagem | Auxiliar de Jardinagem (atestado multiusos)",
    "McDONALD'S": "Funcionário/a Restaurante",
    PLAT: "Cozinheiro/a - 3ª, 2ª, 1ª | Empregado/a de balcão (FT/PT) | Empregado/a de mesa - 2ª, 1ª (FT/PT)",
    "PRIMEFOOD": "Ajudante de cozinha | Empregado/a de balcão",
    "SANTA CASA DA MISERICÓRDIA DE LISBOA (SCML)": "Auxiliar de ação médica | Auxiliar de geriatria e apoio à "
         "comunidade | Técnico/a de ação educativa",
    "SCML Babysitting": "Babysitter",
    STEAM: "Assistente administrativo (PARQUE DAS NAÇÕES E BENFICA) | Assistente de serviço ao cliente "
           "(ALFRAGIDE) | Assistente de serviço ao cliente (ODIVELAS) | Promotor de evento desportivo (ALVERCA) | "
           "Técnico de sistemas eletrónicos",
    "STRONG CHARON": "Vigilante de segurança privada",
}
ELEMENTO = {c: f"ofertas_{i}" for i, c in enumerate(OFERTAS)}

ENTIDADES = [
    ("Gabinete de Inserção Profissional (exemplo)", "Rita Exemplo"),
    ("Espaço Emprego (exemplo)", "Paulo Exemplo"),
    ("Associação Local (exemplo)", "Sofia Exemplo"),
]

# (id, dia de Set, hora, nome, empresas {empresa: [ofertas]}, nif, documento, localidade, habilitações)
CANDIDATOS = [
    ("demo01", 21, "10:12", "Ana Marques", {ECI: ["Caixa de Supermercado (PT)"], STEAM: ["Assistente de serviço ao cliente (ODIVELAS)"]}, "900000011", "10000011", "Carnide, Lisboa", "12.º ano"),
    ("demo02", 21, "11:40", "Bruno Carvalho", {ECI: ["Repositor/a Supermercado", "Repositor/Moço de Piso"]}, "900000022", "10000022", "Benfica, Lisboa", "9.º ano"),
    ("demo03", 22, "09:05", "Carla Mendes", {ECI: ["Caixa de Supermercado (PT)"], PLAT: ["Empregado/a de balcão (FT/PT)"]}, "900000033", "10000033", "Carnide, Lisboa", "12.º ano"),
    ("demo04", 22, "15:30", "Diogo Pereira", {STEAM: ["Técnico de sistemas eletrónicos"]}, "900000044", "10000044", "Odivelas", "Curso profissional"),
    ("demo05", 23, "10:00", "Eva Rodrigues", {ECI: ["Repositor de Brinquedos"], STEAM: ["Assistente administrativo (PARQUE DAS NAÇÕES E BENFICA)"], HORTO: ["Jardineiro/a"]}, "900000055", "10000055", "Lumiar, Lisboa", "Licenciatura"),
    ("demo06", 23, "14:22", "Filipe Sousa", {ECI: ["Perecíveis de supermercado"]}, "900000066", "10000066", "Carnide, Lisboa", "9.º ano"),
    ("demo07", 24, "09:48", "Gabriela Nunes", {STEAM: ["Promotor de evento desportivo (ALVERCA)"], ECI: ["Repositor/a de Adornos de Natal"]}, "900000077", "10000077", "Pontinha, Odivelas", "12.º ano"),
    ("demo08", 24, "16:05", "Hugo Fernandes", {ECI: ["Repositor/a Supermercado"]}, "900000088", "10000088", "Carnide, Lisboa", "6.º ano"),
    ("demo09", 25, "11:15", "Inês Correia", {STEAM: ["Assistente de serviço ao cliente (ALFRAGIDE)"]}, "900000099", "10000099", "Amadora", "12.º ano"),
    ("demo10", 25, "12:30", "João Lopes", {ECI: ["Caixa de Supermercado (PT)"], PLAT: ["Cozinheiro/a - 3ª, 2ª, 1ª"]}, "900000101", "10000101", "Carnide, Lisboa", "9.º ano"),
    # A mesma pessoa que demo01 (NIF igual): a sincronização anula-a como inscrição repetida (R3).
    ("demo11", 26, "10:40", "Ana Marques", {STEAM: ["Assistente administrativo (PARQUE DAS NAÇÕES E BENFICA)"]}, "900000011", "10000011", "Carnide, Lisboa", "12.º ano"),
    # Escolheu a empresa mas «Não me candidato» à oferta: fica sinalizada "Sem oferta".
    ("demo12", 26, "15:10", "Luísa Tavares", {ECI: []}, "900000121", "10000121", "Benfica, Lisboa", "12.º ano"),
    ("demo13", 27, "09:30", "Marco Gomes", {STEAM: ["Técnico de sistemas eletrónicos"]}, "900000131", "10000131", "Carnide, Lisboa", "Curso profissional"),
    ("demo14", 28, "17:45", "Nuno Batista", {ECI: ["Repositor/Moço de Piso"]}, "900000141", "10000141", "Pontinha, Odivelas", "9.º ano"),
]


def resposta(i, c):
    rid, dia, hora, nome, emp, nif, doc, loc, hab = c
    entidade, tecnico = ENTIDADES[i % len(ENTIDADES)]
    primeiro = nome.split()[0].lower()
    data = {
        "entidade_nome": entidade, "entidade_tecnico": tecnico,
        "entidade_email": "gabinete@exemplo.pt", "entidade_telefone": "210 000 000",
        "cand_nome": nome, "cand_telefone": f"910 000 {i:03d}", "cand_email": f"{primeiro}{i}@exemplo.pt",
        "cand_localidade": loc, "cand_habilitacoes": hab,
        "cand_nif": nif, "cand_tipo_doc": "Cartão de Cidadão", "cand_num_doc": doc,
        "cand_cv": [f"https://demo.invalid/storage/{rid}.pdf"],
        "rgpd_1": "accepted", "rgpd_2": "accepted", "rgpd_3": "accepted",
        "empresas_master": [f"{e} — x" for e in emp],
    }
    for e, ofertas in emp.items():
        data[ELEMENTO[e]] = ofertas or [ex.NENHUMA_FALLBACK]
    h = int(hora[:2]) - 1   # hora de Lisboa (UTC+1) → UTC
    return {"id": rid, "createdAt": f"2026-09-{dia:02d}T{h:02d}:{hora[3:]}:00.000Z", "updatedAt": "",
            "finished": True, "data": data}


def cv_exemplo(caminho):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(caminho, pagesize=A4)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(72, 760, "Currículo de exemplo")
    c.setFont("Helvetica", 12)
    for n, linha in enumerate([
        "Documento fictício para a formação dos técnicos validadores (FE2026).",
        "Na Feira, aqui aparece o CV em PDF enviado no formulário de inscrição.",
        "", "Experiência profissional", "  2022–2026  Funções de exemplo, Empresa Exemplo, Lda.",
        "", "Formação", "  12.º ano — Escola Secundária de Exemplo"]):
        c.drawString(72, 720 - n * 18, linha)
    c.save()


def dia_da_feira(con):
    """O check-in na receção (com uma entrevista acrescentada no dia e um candidato
    sem inscrição), o seguimento de dois candidatos pela empresa e uma avaliação (13 Out 2026)."""
    import json
    em = "2026-10-13T12:00:00+00:00"

    def rec(sid, emp, presente, feita, extra=0, hora=None):
        con.execute("INSERT OR REPLACE INTO rececao (id_submissao, empresa, presente, entrevistado, extra, horario_dia, "
                    "alterado_em, alterado_por) VALUES (?, ?, ?, ?, ?, ?, ?, 'Receção (demonstração)')",
                    (sid, emp, presente, feita, extra, hora, em))
    rec("demo01", ECI, 1, 1, hora="10:30")
    rec("demo01", HORTO, 1, 1, extra=1, hora="11:40")
    rec("demo02", ECI, 1, 1, hora="10:00")
    rec("demo06", ECI, 1, 1, hora="10:15")
    rec("demo04", STEAM, 1, 1, hora="10:00")
    rec("demo13", STEAM, 1, 0)
    rec("demo05", STEAM, 1, 1, hora="10:15")
    cur = con.execute("INSERT INTO rececao_avulsos (nome, telefone, criado_em, criado_por) VALUES "
                      "('Rui Visitante', '910 999 000', ?, 'Receção (demonstração)')", (em,))
    rec(f"avulso-{cur.lastrowid}", "LIDL", 1, 1, extra=1, hora="11:20")
    campos = ("nova_entrevista", "selecionado", "contratado", "tipo_contrato", "duracao_contrato", "observacoes")
    for sid, valores in (("demo02", ("Sim", "Sim", "Sim", "Termo certo", "6 meses", "Começa a 3 de Novembro.")),
                         ("demo01", ("Sim", "Não", None, None, None, None))):
        con.execute(f"INSERT INTO seguimento (id_submissao, empresa, {', '.join(campos)}, alterado_em, alterado_por, "
                    f"alterado_perfil) VALUES (?, ?, {', '.join('?' * len(campos))}, ?, ?, 'empresa')",
                    (sid, ECI, *valores, "2026-10-20T10:00:00+00:00", ECI))
    con.execute("INSERT INTO avaliacao (empresa, atendimento, interesse, horario, duracao, espaco, equipamento, "
                "divulgacao, encaminhamento, matching, plataforma, global, positivos, sugestoes, obs_notas, "
                "respondido_por, criada_em, alterado_em, alterado_por) "
                "VALUES (?, 4, 4, 3, 3, 4, 2, 4, 3, 3, 4, 4, ?, ?, ?, 'Joana Exemplo', ?, ?, ?)",
                (ECI, "Candidatos bem preparados e pontuais.", "Mais tempo entre entrevistas.",
                 json.dumps({"equipamento": "Faltou uma tomada perto da mesa."}, ensure_ascii=False), em, em, ECI))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default=tokens.BASE_URL)
    p.add_argument("--cv", help="onde gravar o CV de exemplo (PDF)")
    a = p.parse_args()

    for suf in ("", "-wal", "-shm"):
        if os.path.exists(DB + suf):
            os.remove(DB + suf)

    mapping = ex.read_json(ex.MAPPING_FILE)
    empresas = mapping["company_choice_ids"]
    config, _ = ex.load_config(ex.CONFIG_FILE, list(empresas))
    respostas = [resposta(i, c) for i, c in enumerate(CANDIDATOS, 1)]
    subs, cands, avisos = ex.transform(respostas, list(empresas), {v: k for k, v in ELEMENTO.items()},
                                       {ex.NENHUMA_FALLBACK})
    ex.compute_flags(subs)
    formulario = {e: {"ofertas": OFERTAS.get(e), "no_formulario": int(e not in ex.DESISTENCIAS)} for e in empresas}
    con = ex.connect(DB)
    ex.sync(con, subs, cands, empresas, len(respostas), avisos, config, formulario)
    con.close()

    con = bd.ligar(DB)
    bd.garantir_schema(con)
    expira = "2027-01-11 23:59"
    paulo = con.execute("SELECT tecnico_id FROM submissoes WHERE id_submissao = 'demo01'").fetchone()[0]
    links = [
        ("Coordenação (demonstração)", tokens.criar(con, "coordenador", "Coordenação (demonstração)", expira=expira)),
        ("Técnico/a de triagem (El Corte Inglés + STEAM)",
         tokens.criar(con, "tecnico", "Técnico/a de demonstração", id_tecnico="T-DEMO", expira=expira)),
        ("Técnico/a da entidade que inscreveu (Paulo Exemplo)",
         tokens.criar(con, "entidade", "Paulo Exemplo", ref_submissao=paulo, email="paulo@exemplo.invalid",
                       expira=expira)),
        ("Empresa EL CORTE INGLÉS (o que a empresa vê)", tokens.criar(con, "empresa", ECI, empresa=ECI, expira=expira)),
        ("Receção (check-in no tablet)", tokens.criar(con, "rececao", "Receção (demonstração)", expira=expira)),
    ]
    tokens.criar(con, "tecnico", "Outro técnico (demonstração)", id_tecnico="T-OUTRO", expira=expira)

    # Atribuição: ECI e STEAM ao técnico da demonstração, o resto a outro técnico.
    con.execute("UPDATE candidaturas SET id_tecnico = CASE WHEN empresa IN (?, ?) THEN 'T-DEMO' ELSE 'T-OUTRO' END",
                (ECI, STEAM))

    # Alguns casos já tratados, para mostrar cada estado.
    def tratar(sid, empresa, validacao, horario=None, obs=None, minuto=0, quem="Técnico/a de demonstração"):
        con.execute("UPDATE candidaturas SET validacao = ?, horario_entrevista = ?, observacoes = ?, "
                    "validacao_alterada_em = ?, validacao_alterada_por = ? WHERE id_submissao = ? AND empresa = ?",
                    (validacao, horario, obs, f"2026-09-29T09:{minuto:02d}:00.000000+00:00", quem, sid, empresa))
    tratar("demo02", ECI, "Validado", "10:00", "Confirmou presença por telefone.", 1)
    tratar("demo06", ECI, "Validado", "10:15", None, 2)
    tratar("demo03", PLAT, "Validado", "10:30", None, 3, "Outro técnico (demonstração)")
    tratar("demo08", ECI, "Anulado", None, "Já está empregado (informou por telefone a 26/09).", 4)
    tratar("demo04", STEAM, "Validado", "10:00", None, 5)
    tratar("demo09", STEAM, "Validado", None, "Sem horário por agora: fica em lista de espera.", 6)
    tratar("demo01", ECI, "Validado", "10:30", None, 7)
    tratar("demo07", STEAM, "Validado", "10:30", None, 8)
    tratar("demo10", ECI, "Validado", "10:45", None, 9)
    tratar("demo13", STEAM, "Validado", None, None, 10)
    tratar("demo05", STEAM, "Validado", "10:15", None, 11)
    dia_da_feira(con)
    con.close()

    if a.cv:
        cv_exemplo(a.cv)
    print(f"BD de demonstração criada em {DB}: {len(subs)} candidatos, {len(cands)} candidaturas.")
    for nome, t in links:
        print(f"{nome}:\n  {a.base_url}/?t={t}")


if __name__ == "__main__":
    main()
