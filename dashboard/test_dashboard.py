#!/usr/bin/env python3
"""Testes do dashboard FE2026, só com dados fictícios.

    cd dashboard && python3 -m unittest test_dashboard -v
"""
import atexit
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(AQUI))  # exportar_candidaturas.py e r1_capacidade.py

import exportar_candidaturas as ex  # noqa: E402

TMP = tempfile.TemporaryDirectory()
atexit.register(TMP.cleanup)
os.environ["FE2026_DB"] = os.path.join(TMP.name, "fe2026.db")
os.environ["FE2026_COOKIE_SECURE"] = "auto"
ENVF = os.path.join(TMP.name, "fb.env")
with open(ENVF, "w") as f:
    f.write("FORMBRICKS_GEC_URL=http://formbricks.invalid\nFORMBRICKS_GEC_API_KEY=x\n")
os.environ["FE2026_ENV_FILE"] = ENVF

import agenda  # noqa: E402
import bd  # noqa: E402
import cv  # noqa: E402
import tokens  # noqa: E402

# Os links de triagem e da receção expiram por omissão a 18/10 e a 14/10 de 2026;
# os testes não podem depender do relógio (falhavam desde 19 Out 2026).
_criar_token = tokens.criar


def _criar_token_sem_prazo(con, perfil, nome, *a, expira=None, **kw):
    if expira is None and perfil in ("tecnico", "rececao"):
        expira = "2099-12-31 23:59"
    return _criar_token(con, perfil, nome, *a, expira=expira, **kw)


tokens.criar = _criar_token_sem_prazo

COMPANIES = {"LIDL": "emp_lidl", "STEAM": "emp_steam"}
OFFERS = {"ofertas_lidl": "LIDL", "ofertas_steam": "STEAM"}
CONFIG = {"hora_inicio": "10:00", "hora_fim": "11:00", "empresas": {
    "LIDL": {"n_entrevistadores": 1, "duracao_entrevista_min": 20, "capacidade_maxima": 4},
    "STEAM": {"n_entrevistadores": 2, "duracao_entrevista_min": 15, "capacidade_maxima": 10},
}}


def resp(rid, minuto, master, nif, doc=None):
    return {"id": rid, "createdAt": f"2026-09-21T15:{minuto:02d}:00.000Z", "updatedAt": "x", "finished": True,
            "data": {"cand_nome": f"Pessoa {rid}", "cand_nif": nif, "cand_num_doc": doc or f"D{rid}",
                     "cand_cv": [f"https://formularios.exemplo.org/storage/e/private/{rid}.pdf"],
                     "rgpd_1": "accepted", "rgpd_2": "accepted", "rgpd_3": "accepted",
                     "empresas_master": [f"{c} — x" for c in master]}}


def pdf_minimo():
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(100, 700, "Curriculo de teste")
    c.save()
    return buf.getvalue()


class DashboardTest(unittest.TestCase):
    def setUp(self):
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(os.environ["FE2026_DB"] + suf):
                os.remove(os.environ["FE2026_DB"] + suf)
        responses = [
            resp("a", 1, ["LIDL", "STEAM"], "204813751"),
            resp("b", 2, ["LIDL"], "241590639"),
            resp("c", 3, ["STEAM"], "204813751"),    # a mesma pessoa que "a" (NIF igual), repete a STEAM
            resp("d", 4, ["LIDL"], "273608142"),
        ]
        subs, cands, av = ex.transform(responses, list(COMPANIES), OFFERS, {ex.NENHUMA_FALLBACK})
        ex.compute_flags(subs)
        c = ex.connect(os.environ["FE2026_DB"])
        ex.sync(c, subs, cands, COMPANIES, len(responses), av, CONFIG)
        c.close()
        con = bd.ligar()
        bd.garantir_schema(con)
        self.coord = tokens.criar(con, "coordenador", "Coord")
        self.t1 = tokens.criar(con, "tecnico", "Técnica Um", id_tecnico="T1")
        self.t2 = tokens.criar(con, "tecnico", "Técnico Dois", id_tecnico="T2")
        self.lidl = tokens.criar(con, "empresa", "LIDL", empresa="LIDL")
        self.velho = tokens.criar(con, "coordenador", "Expirado", expira="2020-01-01 00:00")
        con.execute("UPDATE candidaturas SET id_tecnico = 'T1'")
        con.execute("UPDATE candidaturas SET id_tecnico = 'T2' WHERE id_submissao = 'd'")
        con.close()
        import app as appmod
        self.app = appmod
        appmod.app.config.update(TESTING=True, DB_PATH=os.environ["FE2026_DB"])
        self.client = appmod.app.test_client()

    def entrar(self, token):
        self.client = self.app.app.test_client()
        r = self.client.get(f"/?t={token}")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("t=", r.headers["Location"])
        return self.client

    def csrf(self, token):
        return self.app.csrf_de(token)

    def gravar(self, token, sid, choice, validacao, horario="", **extra):
        return self.client.post(f"/c/{sid}/{choice}", data={"csrf": self.csrf(token), "validacao": validacao,
                                                            "horario": horario, **extra})

    def acessos(self, accao):
        con = bd.ligar()
        n = con.execute("SELECT count(*) FROM acessos WHERE accao = ?", (accao,)).fetchone()[0]
        con.close()
        return n

    def cand(self, sid, empresa):
        con = bd.ligar()
        r = con.execute("SELECT * FROM candidaturas WHERE id_submissao = ? AND empresa = ?", (sid, empresa)).fetchone()
        con.close()
        return r

    # ── estatísticas (4 Nov 2026) ──
    def test_estatisticas_para_todos_os_perfis_sem_dados_pessoais(self):
        for tok in (self.coord, self.t1, self.lidl):
            c = self.entrar(tok)
            r = c.get("/estatisticas")
            self.assertEqual(r.status_code, 200, tok)
            html = r.get_data(as_text=True)
            self.assertIn("Estatísticas da Feira", html)
            for pessoal in ("@", "Telefone", "cand_nif"):
                self.assertNotIn(pessoal, html)

    def test_resumo_dos_comentarios_so_coordenacao_e_triagem(self):
        con = bd.ligar()
        con.execute("INSERT INTO avaliacao_feira (pessoa, nome, grupo, correu_bem, melhorar, comentarios, criada_em, "
                    "alterado_em, alterado_por, alterado_perfil) VALUES ('a b', 'Ana Segredo', 'triagem', 'Boa', 'NA', NULL, "
                    "'2026-10-20', '2026-10-20', 'x', 'tecnico'), ('c d', 'Rui Oculto', 'triagem', 'xx', NULL, NULL, "
                    "'2026-10-20', '2026-10-20', 'x', 'tecnico')")
        con.commit()
        con.close()
        for tok in (self.coord, self.t1):
            html = self.entrar(tok).get("/estatisticas").get_data(as_text=True)
            self.assertIn("Comentários das avaliações: resumo", html)
            self.assertIn("sem nenhuma preenchida", html)
            for nome in ("Ana Segredo", "Rui Oculto"):
                self.assertNotIn(nome, html)
        self.assertNotIn("Comentários das avaliações: resumo",
                         self.entrar(self.lidl).get("/estatisticas").get_data(as_text=True))
        self.assertEqual(self.app.preenchimento_textos.__name__, "preenchimento_textos")

    def test_paineis_de_graficos_das_avaliacoes(self):
        c = self.entrar(self.coord)
        for url in ("/avaliacao", "/avaliacao-feira/resultados"):
            r = c.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertIn("<svg", r.get_data(as_text=True)) if "resultados" in url else None

    # ── acesso ──
    def test_sem_token_ou_token_invalido(self):
        self.assertEqual(self.client.get("/candidaturas").status_code, 403)
        self.assertEqual(self.client.get("/?t=inventado").status_code, 403)
        self.assertEqual(self.client.get(f"/?t={self.velho}").status_code, 403)
        self.assertGreaterEqual(self.acessos("recusado"), 2)

    def test_revogado(self):
        self.entrar(self.t1)
        con = bd.ligar()
        con.execute("UPDATE tokens SET revogado = 1 WHERE perfil = 'tecnico' AND id_tecnico = 'T1'")
        con.close()
        self.assertEqual(self.client.get("/candidaturas").status_code, 403)

    def test_cookie_secure_so_por_https(self):
        r = self.client.get(f"/?t={self.coord}")
        self.assertNotIn("Secure", r.headers["Set-Cookie"])
        r = self.client.get(f"/?t={self.coord}", headers={"X-Forwarded-Proto": "https"})
        self.assertIn("Secure", r.headers["Set-Cookie"])
        self.assertIn("HttpOnly", r.headers["Set-Cookie"])

    def test_cabecalhos_privacidade(self):
        self.entrar(self.coord)
        r = self.client.get("/candidaturas")
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertIn("noindex", r.headers["X-Robots-Tag"])
        self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])

    def test_tecnico_so_ve_as_suas(self):
        self.entrar(self.t1)
        html = self.client.get("/candidaturas").get_data(as_text=True)
        self.assertIn("Pessoa a", html)
        self.assertNotIn("Pessoa d", html)
        self.assertEqual(self.client.get("/c/d/emp_lidl").status_code, 404)
        self.assertEqual(self.client.get("/exportar.csv").status_code, 403)

    def test_empresa_so_leitura_e_so_validados(self):
        self.entrar(self.lidl)
        self.assertEqual(self.client.get("/candidaturas").status_code, 403)
        self.assertEqual(self.client.get("/c/a/emp_lidl").status_code, 403)
        self.assertEqual(self.client.get("/agenda/emp_steam").status_code, 403)
        html = self.client.get("/agenda/emp_lidl").get_data(as_text=True)
        self.assertNotIn("Pessoa a", html)          # ainda por validar
        self.entrar(self.t1)
        self.gravar(self.t1, "a", "emp_lidl", "Validado", "10:00")
        self.entrar(self.lidl)
        html = self.client.get("/agenda/emp_lidl").get_data(as_text=True)
        self.assertIn("Pessoa a", html)
        self.assertNotIn("204813751", html)                # NIF escondido às empresas

    def test_rodape_com_bipzip_e_credito(self):
        """Todos os ecrãs, incluindo os das empresas e as listagens, levam o crédito (6 Out 2026); nos das
        empresas e dos técnicos das entidades, com link para cloudkeep.pt (14 Out 2026)."""
        credito = "<b>Cloudkeep<span>.pt</span></b>"
        link = '<a href="https://cloudkeep.pt" target="_blank" rel="noopener">'
        self.entrar(self.lidl)
        for url in ("/agenda/emp_lidl", "/seguimento/emp_lidl", "/candidaturas"):
            html = self.client.get(url).get_data(as_text=True)
            self.assertIn(credito, html, url)
            self.assertIn("lisboa-bipzip.png", html, url)
            # REDEmprega entre o GEC e o BIP/ZIP (7 Out 2026)
            self.assertLess(html.index("gec-simbolo.png", html.index("rodape")), html.index("redemprega.png"), url)
            self.assertLess(html.index("redemprega.png"), html.index("lisboa-bipzip.png"), url)
            self.assertIn(link + credito, html, url)
        html = self.client.get("/agenda/emp_lidl").get_data(as_text=True)
        self.assertIn('class="nav-sec amarelo" href="/agenda/emp_lidl" aria-current="page"', html)
        html = self.client.get("/seguimento/emp_lidl").get_data(as_text=True)
        self.assertIn('class="nav-sec azul" href="/seguimento" aria-current="page"', html)
        self.entrar(self.coord)
        self.assertIn(credito, self.client.get("/listagens/tecnicos").get_data(as_text=True))
        html = self.client.get("/candidaturas").get_data(as_text=True)
        self.assertNotIn('href="https://cloudkeep.pt"', html)          # a coordenação fica sem link
        self.assertIn('class="nav-sec amarelo" href="/candidaturas" aria-current="page"', html)
        self.assertIn('class="nav-sec azul" href="/seguimento">', html)
        html = self.client.get("/seguimento").get_data(as_text=True)
        self.assertIn('class="nav-sec azul" href="/seguimento" aria-current="page"', html)

    def test_post_sem_csrf_recusado(self):
        self.entrar(self.t1)
        r = self.client.post("/c/a/emp_lidl", data={"validacao": "Validado"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.cand("a", "LIDL")["validacao"], "Por validar")

    def test_post_do_browser_com_origin(self):
        # Todas as gravações davam 403 em produção: com no-referrer o browser
        # mandava "Origin: null". Com same-origin manda a origem verdadeira.
        self.entrar(self.t1)
        r = self.client.get("/candidaturas")
        self.assertEqual(r.headers["Referrer-Policy"], "same-origin")
        r = self.client.post("/c/a/emp_lidl", headers={"Origin": "http://localhost"},
                             data={"csrf": self.csrf(self.t1), "validacao": "Validado", "horario": ""})
        self.assertEqual(r.status_code, 302)
        r = self.client.post("/c/a/emp_lidl", headers={"Origin": "https://outro.exemplo"},
                             data={"csrf": self.csrf(self.t1), "validacao": "Anulado", "horario": ""})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.cand("a", "LIDL")["validacao"], "Validado")

    def test_reverter_duplicado_so_coordenacao(self):
        c = self.cand("c", "STEAM")
        self.assertEqual((c["validacao"], c["validacao_alterada_por"]), ("Anulado", "Automático (duplicado)"))
        self.entrar(self.t1)
        # o técnico nem a vê (30 Set 2026): as anuladas pelo sistema ou pela coordenação ficam fora do seu âmbito
        self.assertEqual(self.client.get("/c/c/emp_steam").status_code, 404)
        self.assertEqual(self.gravar(self.t1, "c", "emp_steam", "Por validar").status_code, 404)
        self.assertEqual(self.cand("c", "STEAM")["validacao"], "Anulado")
        self.entrar(self.coord)
        self.assertIn("Para a reverter", self.client.get("/c/c/emp_steam").get_data(as_text=True))
        r = self.gravar(self.coord, "c", "emp_steam", "Por validar", id_tecnico="T1")
        self.assertEqual(r.status_code, 302)
        c = self.cand("c", "STEAM")
        self.assertEqual((c["validacao"], c["validacao_alterada_por"]), ("Por validar", "Coord"))

    # ── marcação ──
    def test_grelha(self):
        self.assertEqual(agenda.grelha({"hora_inicio": "10:00", "hora_fim": "13:00", "duracao_entrevista_min": 15})[-1],
                         "12:45")
        self.assertEqual(len(agenda.grelha({"hora_inicio": "10:00", "hora_fim": "13:00",
                                            "duracao_entrevista_min": 20})), 9)

    def test_validar_e_marcar(self):
        self.entrar(self.t1)
        r = self.gravar(self.t1, "a", "emp_lidl", "Validado", "10:20")
        self.assertEqual(r.status_code, 302)
        c = self.cand("a", "LIDL")
        self.assertEqual((c["validacao"], c["horario_entrevista"], c["validacao_alterada_por"]),
                         ("Validado", "10:20", "Técnica Um"))

    def test_horario_cheio_nesta_empresa(self):
        self.entrar(self.t1)
        self.gravar(self.t1, "a", "emp_lidl", "Validado", "10:00")
        r = self.gravar(self.t1, "b", "emp_lidl", "Validado", "10:00")   # LIDL tem 1 entrevistador
        self.assertEqual(r.status_code, 200)
        self.assertIn("ocupado nesta empresa", r.get_data(as_text=True))
        self.assertEqual(self.cand("b", "LIDL")["validacao"], "Por validar")  # nada gravado

    def test_dois_entrevistadores(self):
        self.entrar(self.coord)
        con = bd.ligar()
        con.execute("UPDATE candidaturas SET id_tecnico = NULL")
        con.close()
        self.gravar(self.coord, "a", "emp_steam", "Validado", "10:00")
        # "c" é a mesma pessoa que "a": 10:00 na STEAM sobrepõe-se; 10:15 não
        r = self.gravar(self.coord, "c", "emp_steam", "Validado", "10:00")
        self.assertIn("candidato já marcado às 10:00 em STEAM", r.get_data(as_text=True))

    def test_sobreposicao_entre_empresas(self):
        self.entrar(self.t1)
        self.gravar(self.t1, "a", "emp_lidl", "Validado", "10:00")      # LIDL 10:00–10:20
        r = self.gravar(self.t1, "a", "emp_steam", "Validado", "10:15")  # STEAM 10:15–10:30
        self.assertIn("candidato já marcado às 10:00 em LIDL", r.get_data(as_text=True))
        # outra submissão, mesmo NIF: anulada sozinha, só a coordenação lhe chega
        self.entrar(self.coord)
        r = self.gravar(self.coord, "c", "emp_steam", "Validado", "10:15", id_tecnico="T1")
        self.assertIn("candidato já marcado", r.get_data(as_text=True))
        self.entrar(self.t1)
        self.assertEqual(self.gravar(self.t1, "a", "emp_steam", "Validado", "10:30").status_code, 302)

    def test_anular_liberta_horario(self):
        self.entrar(self.t1)
        self.gravar(self.t1, "a", "emp_lidl", "Validado", "10:00")
        self.gravar(self.t1, "a", "emp_lidl", "Anulado", "10:00")
        self.assertIsNone(self.cand("a", "LIDL")["horario_entrevista"])
        self.assertEqual(self.gravar(self.t1, "b", "emp_lidl", "Validado", "10:00").status_code, 302)

    def test_horario_pre_seleccionado(self):
        """8 Out 2026: por omissão, o primeiro horário livre; a lista de espera vem no fim e só fica
        seleccionada quando não há horário livre, nem de tolerância."""
        import re

        def seleccionado(sid):
            html = self.client.get(f"/c/{sid}/emp_lidl").get_data(as_text=True)
            opcoes = re.search(r'<select name="horario">(.*?)</select>', html, re.S).group(1)
            self.assertIn("Sem horário", opcoes.split("<option")[-1])  # a lista de espera é a última opção
            return re.search(r'<option value="([^"]*)"[^>]*\bselected\b', html).group(1)

        def marcar(sid, horario):
            con = bd.ligar()
            con.execute("UPDATE candidaturas SET validacao = 'Validado', horario_entrevista = ? "
                        "WHERE id_submissao = ? AND empresa = 'LIDL'", (horario, sid))
            con.close()

        self.entrar(self.coord)
        self.assertEqual(seleccionado("b"), "10:00")
        marcar("a", "10:00")
        self.assertEqual(seleccionado("b"), "10:20")
        self.assertEqual(seleccionado("a"), "10:00")                 # o já marcado
        self.com_tolerancia(1)                                        # 10:00 10:20 10:40 + 11:00
        marcar("d", "10:20")
        self.assertEqual(seleccionado("b"), "10:40")                 # a tolerância só depois dos outros
        con = bd.ligar()
        con.execute("UPDATE empresas SET hora_fim = '10:40' WHERE empresa = 'LIDL'")   # 10:00 10:20 + 10:40
        con.close()
        self.assertEqual(seleccionado("b"), "10:40")                 # só resta a tolerância
        self.gravar(self.coord, "b", "emp_lidl", "Validado", "", id_tecnico="T1")
        self.assertEqual(seleccionado("b"), "")                      # validada em espera: continua em espera
        con = bd.ligar()
        con.execute("UPDATE candidaturas SET validacao = 'Por validar' WHERE id_submissao = 'b'")
        con.close()
        self.com_tolerancia(0)
        self.assertEqual(seleccionado("b"), "")                      # vagas e tolerância esgotadas

    def test_horario_exige_validado(self):
        self.entrar(self.t1)
        self.gravar(self.t1, "a", "emp_lidl", "Por validar", "10:00")
        self.assertIsNone(self.cand("a", "LIDL")["horario_entrevista"])

    def test_lista_de_espera(self):
        self.entrar(self.coord)
        self.gravar(self.coord, "b", "emp_lidl", "Validado", "", id_tecnico="T1")
        self.gravar(self.coord, "a", "emp_lidl", "Validado", "", id_tecnico="T1")
        con = bd.ligar()
        espera = [r["id_submissao"] for r in agenda.lista_de_espera(con, "LIDL")]
        con.close()
        self.assertEqual(espera, ["b", "a"])             # ordem de validação, não de inscrição
        html = self.client.get("/candidaturas?estado=espera").get_data(as_text=True)
        self.assertIn("Em espera (n.º 1)", html)

    def test_tecnico_nao_mexe_no_tecnico_atribuido(self):
        self.entrar(self.t1)
        self.gravar(self.t1, "a", "emp_lidl", "Validado", "", id_tecnico="T2")
        self.assertEqual(self.cand("a", "LIDL")["id_tecnico"], "T1")

    def test_resync_preserva_marcacoes(self):
        self.entrar(self.t1)
        self.gravar(self.t1, "a", "emp_lidl", "Validado", "10:40")
        self.setUp_resync()
        self.assertEqual(self.cand("a", "LIDL")["horario_entrevista"], "10:40")

    def setUp_resync(self):
        responses = [resp("a", 1, ["LIDL", "STEAM"], "204813751"), resp("b", 2, ["LIDL"], "241590639"),
                     resp("c", 3, ["STEAM"], "204813751"), resp("d", 4, ["LIDL"], "273608142")]
        subs, cands, av = ex.transform(responses, list(COMPANIES), OFFERS, {ex.NENHUMA_FALLBACK})
        ex.compute_flags(subs)
        c = ex.connect(os.environ["FE2026_DB"])
        ex.sync(c, subs, cands, COMPANIES, 4, av, CONFIG)
        c.close()

    def test_atribuir_em_bloco(self):
        con = bd.ligar()
        con.execute("UPDATE candidaturas SET id_tecnico = NULL WHERE empresa = 'LIDL' AND id_submissao = 'b'")
        con.close()
        self.entrar(self.coord)
        r = self.client.post("/atribuir", data={"csrf": self.csrf(self.coord), "empresa": "emp_lidl",
                                                "id_tecnico": "T2", "so_sem_tecnico": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.cand("b", "LIDL")["id_tecnico"], "T2")
        self.assertEqual(self.cand("a", "LIDL")["id_tecnico"], "T1")

    def test_atribuir_a_coordenador_com_id(self):
        con = bd.ligar()
        coord_id = tokens.criar(con, "coordenador", "Coord Valida", id_tecnico="CV")
        con.close()
        self.entrar(self.coord)
        r = self.client.post("/atribuir", data={"csrf": self.csrf(self.coord), "empresa": "emp_lidl",
                                                "id_tecnico": "CV"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.cand("a", "LIDL")["id_tecnico"], "CV")
        self.entrar(coord_id)                                   # continua a ver tudo
        self.assertEqual(self.client.get("/candidaturas").status_code, 200)

    # ── CV ──
    def test_cv_com_marca_de_agua_para_empresa(self):
        pdf = pdf_minimo()
        self.entrar(self.t1)
        self.gravar(self.t1, "a", "emp_lidl", "Validado", "10:00")
        with mock.patch.object(cv, "descarregar", return_value=pdf) as desc:
            r = self.client.get("/cv/a/emp_lidl")
            self.assertEqual(r.data, pdf)                       # técnico: original
            self.assertTrue(desc.call_args[0][1].startswith("http://formbricks.invalid"))
            self.entrar(self.lidl)
            r = self.client.get("/cv/a/emp_lidl")
            self.assertEqual(r.mimetype, "application/pdf")
            self.assertNotEqual(r.data, pdf)
            from pypdf import PdfReader
            self.assertIn("Fornecido a LIDL", PdfReader(io.BytesIO(r.data)).pages[0].extract_text())
            self.assertEqual(self.client.get("/cv/b/emp_lidl").status_code, 404)  # não validado
        self.assertEqual(self.acessos("cv"), 2)

    def test_cv_ilegivel_nao_e_servido_a_empresa(self):
        self.entrar(self.t1)
        self.gravar(self.t1, "a", "emp_lidl", "Validado", "10:00")
        self.entrar(self.lidl)
        with mock.patch.object(cv, "descarregar", return_value=b"isto nao e um pdf"):
            r = self.client.get("/cv/a/emp_lidl")
        self.assertEqual(r.status_code, 422)
        self.assertNotIn(b"isto nao", r.data)

    def test_cv_que_nao_e_pdf(self):
        """29 Set 2026: um CV vazio ou que não é PDF dá uma mensagem clara, a
        todos os perfis, em vez de um ficheiro estragado."""
        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        for conteudo, texto in ((b"", "está vazio"), (b"\n<div id='x'>pagina</div>", "é de outro formato")):
            with mock.patch("urllib.request.urlopen", return_value=Resp(conteudo)):
                with self.assertRaises(cv.CVIndisponivel) as e:
                    cv.descarregar("https://formularios.exemplo.org/storage/x.pdf", "http://fb.invalid", "k")
            self.assertIn(texto, str(e.exception))
        with mock.patch("urllib.request.urlopen", return_value=Resp(pdf_minimo())):
            self.assertTrue(cv.descarregar("https://formularios.exemplo.org/storage/x.pdf", "http://fb.invalid", "k")
                            .startswith(b"%PDF"))
        self.entrar(self.t1)
        with mock.patch.object(cv, "descarregar", side_effect=cv.CVIndisponivel("o ficheiro enviado no formulário "
                                                                                 "não é um PDF válido (está vazio)")):
            r = self.client.get("/cv/a/emp_lidl")
        self.assertEqual(r.status_code, 422)
        self.assertIn("não é um PDF válido", r.get_data(as_text=True))
        self.assertEqual(self.acessos("cv-falhou"), 1)

    def test_exportar_csv(self):
        self.entrar(self.coord)
        r = self.client.get("/exportar.csv")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_data(as_text=True).startswith("﻿id_submissao;"))
        self.assertEqual(self.acessos("exportar"), 1)

    def test_paginas_renderizam(self):
        self.entrar(self.coord)
        for url in ("/candidaturas", "/candidaturas?estado=Validado&q=Pessoa", "/c/a/emp_lidl",
                    "/agenda/emp_lidl", "/agenda/emp_steam"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.gravar(self.coord, "a", "emp_lidl", "Validado", "10:00", id_tecnico="T1")
        self.entrar(self.t2)
        html = self.client.get("/agenda/emp_lidl").get_data(as_text=True)
        self.assertIn("Ocupado (candidatura de outro técnico)", html)   # vê a ocupação, não o nome
        self.assertNotIn("Pessoa a", html)

    # ── 29 Set 2026: tolerância, observações na lista, vagas ──
    def com_tolerancia(self, n=2):
        con = bd.ligar()
        con.execute("UPDATE empresas SET slots_tolerancia = ? WHERE empresa = 'LIDL'", (n,))
        con.close()

    def test_horarios_de_tolerancia(self):
        self.com_tolerancia(2)
        con = bd.ligar()
        emp = agenda.empresa_por_nome(con, "LIDL")
        self.assertEqual(agenda.grelha(emp), ["10:00", "10:20", "10:40", "11:00", "11:20"])
        self.assertEqual([h for h in agenda.grelha(emp) if agenda.e_tolerancia(emp, h)], ["11:00", "11:20"])
        con.close()
        self.entrar(self.coord)
        self.assertEqual(self.gravar(self.coord, "a", "emp_lidl", "Validado", "11:20").status_code, 302)
        self.assertEqual(self.cand("a", "LIDL")["horario_entrevista"], "11:20")
        html = self.client.get("/agenda/emp_lidl").get_data(as_text=True)
        self.assertIn('class=tolerancia', html)
        self.assertIn("2 horários de tolerância depois das 11:00", html)
        self.assertIn("11:20 · tolerância", self.client.get("/c/b/emp_lidl").get_data(as_text=True))
        # Sem tolerância, o horário já marcado continua visível na agenda
        self.com_tolerancia(0)
        self.assertIn("Pessoa a", self.client.get("/agenda/emp_lidl").get_data(as_text=True))
        self.assertIn("fora da grelha", self.gravar(self.coord, "b", "emp_lidl", "Validado", "11:20")
                      .get_data(as_text=True))

    def test_sem_tolerancia_grelha_igual(self):
        con = bd.ligar()
        self.assertEqual(agenda.grelha(agenda.empresa_por_nome(con, "LIDL")), ["10:00", "10:20", "10:40"])
        con.close()

    def test_observacoes_na_lista(self):
        self.entrar(self.coord)
        self.gravar(self.coord, "b", "emp_lidl", "Anulado", observacoes="Não compareceu à pré-entrevista")
        html = self.client.get("/candidaturas").get_data(as_text=True)
        self.assertIn("<th>Observações</th>", html)
        self.assertIn("Não compareceu à pré-entrevista", html)

    def test_vagas(self):
        con = bd.ligar()
        v = {x["e"]["empresa"]: x for x in agenda.vagas(con)}
        con.close()
        self.assertEqual((v["LIDL"]["inscritas"], v["LIDL"]["livres"]), (3, 1))
        self.assertEqual((v["STEAM"]["inscritas"], v["STEAM"]["livres"]), (1, 9))   # "c" repete o NIF de "a"
        self.entrar(self.coord)
        self.gravar(self.coord, "b", "emp_lidl", "Anulado")      # anulação à mão liberta o lugar (1 Out 2026)
        html = self.client.get("/vagas").get_data(as_text=True)
        self.assertIn("Vagas disponíveis nas ofertas", html)
        self.assertIn("• LIDL: 2 vagas", html)
        csv_txt = self.client.get("/vagas.csv").get_data(as_text=True)
        self.assertIn("LIDL;;Aberta;2;2;4", csv_txt)
        # Desistência e empresa retirada pelo R1
        con = bd.ligar()
        con.execute("UPDATE empresas SET desistiu = 1 WHERE empresa = 'LIDL'")
        con.execute("UPDATE empresas SET no_formulario = 0 WHERE empresa = 'STEAM'")
        con.close()
        html = self.client.get("/vagas").get_data(as_text=True)
        self.assertIn("Desistiu da Feira", html)
        self.assertIn("Retirada do formulário", html)
        self.assertIn("Nenhuma empresa tem vagas", html)

    def test_vagas_so_coordenacao(self):
        self.entrar(self.t1)
        self.assertEqual(self.client.get("/vagas").status_code, 403)
        self.assertEqual(self.client.get("/vagas.csv").status_code, 403)
        self.entrar(self.lidl)
        self.assertEqual(self.client.get("/vagas").status_code, 403)

    # ── 30 Set 2026: técnicos sem NIF/documento, listagem por entidade ──
    def test_tecnico_nao_ve_nif_nem_documento(self):
        self.entrar(self.t1)
        html = self.client.get("/c/a/emp_lidl").get_data(as_text=True)
        self.assertIn("Pessoa a", html)
        for escondido in ("<dt>NIF</dt>", "<dt>Documento</dt>", '"num">204813751<', '"num">Da<'):
            self.assertNotIn(escondido, html)
        # procurar pelo NIF não devolve nada; pelo nome, sim
        self.assertNotIn("Pessoa a", self.client.get("/candidaturas?q=204813751").get_data(as_text=True))
        self.assertIn("Pessoa a", self.client.get("/candidaturas?q=ssoa a").get_data(as_text=True))
        self.entrar(self.coord)
        html = self.client.get("/c/a/emp_lidl").get_data(as_text=True)
        self.assertIn('"num">204813751<', html)
        self.assertIn('"num">Da<', html)
        self.assertIn("Pessoa a", self.client.get("/candidaturas?q=204813751").get_data(as_text=True))

    def test_pesquisa_ignora_acentos(self):
        """8 Out 2026: «Inácio» encontra «Inacio» e vice-versa, sem distinguir maiúsculas."""
        con = bd.ligar()
        con.execute("UPDATE submissoes SET cand_nome = 'Inácio Conceição' WHERE id_submissao = 'a'")
        con.execute("UPDATE submissoes SET cand_nome = 'Inacio Conceicao' WHERE id_submissao = 'b'")
        con.close()
        for perfil in (self.coord, self.t1):
            self.entrar(perfil)
            for q in ("Inácio", "inacio", "INÁCIO", "conceicao", "ConceiçÃo"):
                html = self.client.get(f"/candidaturas?q={q}").get_data(as_text=True)
                self.assertIn("Inácio Conceição", html, q)
                self.assertIn("Inacio Conceicao", html, q)
            self.assertNotIn("Inacio", self.client.get("/candidaturas?q=Ignácio").get_data(as_text=True))

    def test_listagem_por_entidade(self):
        con = bd.ligar()
        for sid, ent, tec in (("a", "Gabinete Zeta", "Rui"), ("b", "Área Social", "Ana"), ("c", "Gabinete Zeta", "Rui"),
                              ("d", "Gabinete Zeta", "Bia")):
            con.execute("UPDATE submissoes SET entidade_nome = ?, entidade_tecnico = ?, entidade_email = ? "
                        "WHERE id_submissao = ?", (ent, tec, f"{tec.lower()}@exemplo.pt", sid))
        con.close()
        self.entrar(self.t2)
        html = self.client.get("/candidaturas").get_data(as_text=True)
        self.assertIn("/comunicacao", html)
        self.assertIn("Ainda não há candidaturas validadas nem anuladas", self.client.get("/comunicacao").get_data(as_text=True))
        self.entrar(self.t1)
        self.gravar(self.t1, "a", "emp_lidl", "Validado", "10:00")
        self.gravar(self.t1, "a", "emp_steam", "Validado")                     # sem horário: lista de espera
        self.gravar(self.t1, "b", "emp_lidl", "Anulado", observacoes="Sem atestado multiusos")
        self.entrar(self.t2)
        self.gravar(self.t2, "d", "emp_lidl", "Anulado", observacoes="Desistiu")
        self.entrar(self.t1)
        html = self.client.get("/comunicacao").get_data(as_text=True)
        # "Área Social" antes de "Gabinete Zeta", apesar do acento
        self.assertLess(html.index("Área Social"), html.index("Gabinete Zeta"))
        self.assertIn("Validada · entrevista às 10:00–10:20", html)
        self.assertIn("Validada · em lista de espera (n.º 1)", html)
        self.assertIn("Sem atestado multiusos", html)
        self.assertIn("ana@exemplo.pt", html)
        self.assertNotIn("Pessoa d", html)               # é do outro técnico
        self.assertNotIn("Desistiu", html)
        self.assertNotIn("204813751", html)
        csv_txt = self.client.get("/comunicacao.csv").get_data(as_text=True)
        linhas = csv_txt.strip().splitlines()
        self.assertTrue(linhas[0].startswith("﻿Entidade;Técnico/a da entidade;"))
        self.assertEqual(len(linhas), 4)                 # b e a (2 empresas); a repetida "c", anulada sozinha, não entra
        self.assertTrue(linhas[1].startswith("Área Social;Ana;ana@exemplo.pt;;Pessoa b;LIDL;"), linhas[1])
        self.assertTrue(linhas[1].endswith(";Anulada;Sem atestado multiusos;Técnica Um"), linhas[1])
        self.assertEqual(self.acessos("comunicacao"), 3)
        # a coordenação vê tudo, com os dois técnicos da mesma entidade em grupos separados e por ordem
        self.entrar(self.coord)
        html = self.client.get("/comunicacao").get_data(as_text=True)
        self.assertLess(html.index("<b>Bia</b>"), html.index("<b>Rui</b>"))
        self.assertIn("Desistiu", html)
        self.entrar(self.lidl)
        self.assertEqual(self.client.get("/comunicacao").status_code, 403)
        self.assertEqual(self.client.get("/comunicacao.csv").status_code, 403)

    def test_tecnico_nao_ve_anuladas_pela_coordenacao(self):
        self.entrar(self.coord)
        self.gravar(self.coord, "b", "emp_lidl", "Anulado", id_tecnico="T1", observacoes="Fora do perfil")
        self.gravar(self.coord, "a", "emp_steam", "Anulado", id_tecnico="T1")
        self.entrar(self.t1)
        html = self.client.get("/candidaturas").get_data(as_text=True)
        self.assertNotIn("Pessoa b", html)
        self.assertNotIn("Pessoa c", html)               # inscrição repetida, anulada pelo sistema
        self.assertIn("<b>0</b> anuladas", html)
        self.assertEqual(self.client.get("/c/b/emp_lidl").status_code, 404)
        self.assertEqual(self.client.get("/cv/b/emp_lidl").status_code, 404)
        self.assertNotIn("Fora do perfil", self.client.get("/comunicacao").get_data(as_text=True))
        # nem nas «outras candidaturas do mesmo candidato»
        self.assertNotIn("STEAM", self.client.get("/c/a/emp_lidl").get_data(as_text=True).split("Outras candidaturas")[1])
        # as que o próprio técnico anula continuam à vista
        self.gravar(self.t1, "a", "emp_lidl", "Anulado", observacoes="Não atende")
        html = self.client.get("/candidaturas").get_data(as_text=True)
        self.assertIn("Pessoa a", html)
        self.assertIn("<b>1</b> anuladas", html)
        self.assertIn("Não atende", self.client.get("/comunicacao").get_data(as_text=True))
        # a coordenação continua a ver tudo e, se repuser «Por validar», o técnico volta a ver
        self.entrar(self.coord)
        self.assertIn("Pessoa b", self.client.get("/candidaturas").get_data(as_text=True))
        self.gravar(self.coord, "b", "emp_lidl", "Por validar", id_tecnico="T1")
        self.entrar(self.t1)
        self.assertIn("Pessoa b", self.client.get("/candidaturas").get_data(as_text=True))

    def test_inicio_as_0930(self):
        """Com entrevistas de 20 min a grelha desde as 09:30 não passa pelas
        10:00; a LIDL começa às 09:40 para manter as marcações já feitas."""
        base = {"hora_fim": "13:00", "duracao_entrevista_min": 20, "slots_tolerancia": 2}
        g = agenda.grelha({**base, "hora_inicio": "09:30"})
        self.assertEqual((g[0], g[1], g[-3], g[-2], g[-1]), ("09:30", "09:50", "12:30", "12:50", "13:10"))
        self.assertNotIn("10:00", g)
        g = agenda.grelha({**base, "hora_inicio": "09:40"})
        self.assertEqual(g[:2] + g[-3:], ["09:40", "10:00", "12:40", "13:00", "13:20"])
        cfg = ex.read_json(ex.CONFIG_FILE)
        self.assertEqual(cfg["hora_inicio"], "09:30")
        # As empresas que já tinham entrevistas marcadas a contar das 10:00 mantêm esses horários na grelha
        for nome in ("LIDL", "DOMINO'S PIZZA", "INDITEX"):
            e = cfg["empresas"][nome]
            g = agenda.grelha({"hora_inicio": e.get("hora_inicio", cfg["hora_inicio"]), "hora_fim": cfg["hora_fim"],
                               "duracao_entrevista_min": e["duracao_entrevista_min"]})
            self.assertIn("10:00", g, nome)
            self.assertLess(g[0], "10:00", nome)

    # ── 6 Out 2026: entidades unificadas e links dos técnicos das entidades ──
    def sincronizar_com_entidades(self):
        """Volta a sincronizar as mesmas respostas, agora com os dados da entidade:
        a, b e c são do mesmo técnico, escrito de maneiras diferentes; d é de outro."""
        ent = {"a": ("Gabinete Zeta", "Rui", "rui@zeta.pt", "910000001"),
               "b": ("GABINETE ZETA", "Rui Lopes", "Rui@Zeta.pt", "910 000 001"),
               "c": ("Gabinete Zeta", "Rui Lopes", "ruilopes@gmail.com", "910000001"),
               "d": ("Outra Casa", "Bia", "bia@outra.pt", "920000002")}
        responses = [resp("a", 1, ["LIDL", "STEAM"], "204813751"), resp("b", 2, ["LIDL"], "241590639"),
                     resp("c", 3, ["STEAM"], "204813751"), resp("d", 4, ["LIDL"], "273608142")]
        for r in responses:
            n, t, e, tel = ent[r["id"]]
            r["data"].update(entidade_nome=n, entidade_tecnico=t, entidade_email=e, entidade_telefone=tel)
        subs, cands, av = ex.transform(responses, list(COMPANIES), OFFERS, {ex.NENHUMA_FALLBACK})
        ex.compute_flags(subs)
        c = ex.connect(os.environ["FE2026_DB"])
        ex.sync(c, subs, cands, COMPANIES, len(responses), av, CONFIG, aliases=[])
        c.close()
        con = bd.ligar()
        tid = con.execute("SELECT tecnico_id FROM submissoes WHERE id_submissao = 'b'").fetchone()[0]
        tok = tokens.criar(con, "entidade", "Rui Lopes", ref_submissao=tid, email="rui@zeta.pt")
        con.close()
        return tok

    def test_entidade_unificada(self):
        self.sincronizar_com_entidades()
        con = bd.ligar()
        rows = {r["id_submissao"]: r for r in con.execute("SELECT * FROM submissoes")}
        con.close()
        self.assertEqual({rows[s]["tecnico_id"] for s in "abc"}, {"a"})
        self.assertEqual(rows["a"]["entidade_canon"], "Gabinete Zeta")
        self.assertEqual(rows["c"]["tecnico_canon"], "Rui Lopes")
        self.assertEqual(rows["c"]["tecnico_email_canon"], "rui@zeta.pt")
        self.assertNotEqual(rows["d"]["tecnico_id"], "a")
        self.entrar(self.coord)
        html = self.client.get("/candidaturas?entidade=Gabinete+Zeta").get_data(as_text=True)
        self.assertIn("Pessoa b", html)
        self.assertNotIn("Pessoa d", html)
        html = self.client.get("/c/c/emp_steam").get_data(as_text=True)
        self.assertIn("Como foi escrito", html)                 # o original continua visível à coordenação
        self.assertIn("ruilopes@gmail.com", html)

    def test_entidade_so_ve_os_seus_e_so_le(self):
        tok = self.sincronizar_com_entidades()
        self.entrar(tok)
        self.assertEqual(self.client.get("/").headers["Location"].rsplit("/", 1)[-1], "candidaturas")
        html = self.client.get("/candidaturas").get_data(as_text=True)
        for nome in ("Pessoa a", "Pessoa b"):
            self.assertIn(nome, html)
        self.assertNotIn("Pessoa d", html)
        self.assertNotIn("Atribuir candidaturas", html)
        self.assertNotIn("/agenda/", html)
        self.assertNotIn("/comunicacao", html)
        html = self.client.get("/c/a/emp_lidl").get_data(as_text=True)
        self.assertIn("Consulta apenas", html)
        self.assertNotIn('name="validacao"', html)
        self.assertNotIn('name="id_tecnico"', html)
        self.assertNotIn("204813751", html)                      # sem NIF
        r = self.client.post("/c/a/emp_lidl", data={"csrf": self.csrf(tok), "validacao": "Validado"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.cand("a", "LIDL")["validacao"], "Por validar")
        self.assertEqual(self.client.get("/c/d/emp_lidl").status_code, 404)
        for url in ("/agenda/emp_lidl", "/comunicacao", "/vagas", "/exportar.csv", "/seguimento/emp_lidl",
                    "/seguimento.csv", "/rececao", "/rececao/fecho", "/listagens/tecnicos", "/listagens/rececao-candidatos"):
            self.assertEqual(self.client.get(url).status_code, 403, url)
        con = bd.ligar()
        tecs = tokens.tecnicos_de_entidades(con)
        con.close()
        self.assertEqual(sorted((t["tecnico_canon"], t["tem_link"]) for t in tecs), [("Bia", False), ("Rui Lopes", True)])

    def test_migracao_da_tabela_tokens(self):
        """A tabela criada antes de 6 Out não aceita o perfil entidade; a migração copia os links."""
        caminho = os.path.join(TMP.name, "antiga.db")
        con = bd.ligar(caminho)
        con.executescript("""CREATE TABLE empresas (empresa TEXT PRIMARY KEY);
            CREATE TABLE tokens (token_hash TEXT PRIMARY KEY, prefixo TEXT NOT NULL,
              perfil TEXT NOT NULL CHECK (perfil IN ('coordenador', 'tecnico', 'empresa')), nome TEXT NOT NULL,
              id_tecnico TEXT, empresa TEXT REFERENCES empresas(empresa), criado_em TEXT NOT NULL,
              expira_em TEXT NOT NULL, revogado INTEGER NOT NULL DEFAULT 0, ultimo_acesso TEXT);
            INSERT INTO tokens VALUES ('h', 'abc', 'tecnico', 'Ana', 'T1', NULL, 'x', '2099-01-01', 0, NULL);""")
        bd.garantir_schema(con)
        bd.garantir_schema(con)                                  # a 2.ª vez não faz nada
        self.assertEqual(con.execute("SELECT nome, id_tecnico FROM tokens").fetchall()[0][:], ("Ana", "T1"))
        tokens.criar(con, "entidade", "Rui", ref_submissao="a", email="r@x.pt")
        self.assertEqual(con.execute("SELECT count(*) FROM tokens").fetchone()[0], 2)
        con.close()

    # ── 6 Out 2026: seguimento pelas empresas e prazos ──
    def test_seguimento_pela_empresa(self):
        self.entrar(self.t1)
        self.gravar(self.t1, "a", "emp_lidl", "Validado", "10:00")
        self.entrar(self.lidl)
        r = self.client.get("/seguimento")
        self.assertTrue(r.headers["Location"].endswith("/seguimento/emp_lidl"))
        html = self.client.get("/seguimento/emp_lidl").get_data(as_text=True)
        self.assertIn("Pessoa a", html)
        self.assertNotIn("Pessoa b", html)                       # por validar
        vazio = self.app.assinatura_seguimento(None)
        dados = {"csrf": self.csrf(self.lidl), "linha": "a|LIDL", "v:0": vazio, "nova_entrevista:0": "Sim",
                 "selecionado:0": "Sim", "contratado:0": "Sim", "tipo_contrato:0": "Termo certo",
                 "duracao_contrato:0": "6 meses", "observacoes:0": "Começa a 2 de Novembro"}
        r = self.client.post("/seguimento/emp_lidl", data=dados)
        self.assertEqual(r.status_code, 302)
        con = bd.ligar()
        s = con.execute("SELECT * FROM seguimento").fetchone()
        con.close()
        self.assertEqual((s["contratado"], s["tipo_contrato"], s["duracao_contrato"], s["alterado_por"],
                          s["alterado_perfil"]), ("Sim", "Termo certo", "6 meses", "LIDL", "empresa"))
        r = self.client.post("/seguimento/emp_lidl", data={**dados, "contratado:0": "Talvez"})
        self.assertIn("Valor inválido", r.get_data(as_text=True))
        r = self.client.post("/seguimento/emp_lidl", data={**dados, "linha": "b|LIDL"})   # não é validada da LIDL
        self.assertIn("não corresponde", r.get_data(as_text=True))
        self.assertEqual(self.client.get("/seguimento/emp_steam").status_code, 403)
        self.assertEqual(self.client.get("/seguimento.csv").status_code, 403)
        self.entrar(self.coord)
        html = self.client.get("/seguimento").get_data(as_text=True)
        self.assertIn("LIDL", html)
        csv_txt = self.client.get("/seguimento.csv").get_data(as_text=True)
        self.assertIn("Pessoa a", csv_txt)
        self.assertIn("Sim;Sim;Sim;Termo certo;6 meses;Começa a 2 de Novembro", csv_txt)
        self.entrar(self.t1)
        self.assertEqual(self.client.get("/seguimento").status_code, 403)

    def checkin(self, rec, cands, presente=True, feitas=()):
        """Check-in de uma pessoa na receção: cands = ["sid|EMPRESA", ...]; feitas = índices das entrevistas feitas."""
        dados = {"csrf": self.csrf(rec), "cand": cands}
        if presente:
            dados["presente"] = "1"
        for i, chave in enumerate(cands):
            dados[f"empresa_dia:{i}"] = chave.split("|")[1]
            if i in feitas:
                dados[f"entrevistado:{i}"] = "1"
        r = self.client.post("/rececao", data=dados, headers={"X-Pedido": "fetch"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def outra_empresa(self, nome="OUTRA"):
        con = bd.ligar()
        cols = [r[1] for r in con.execute("PRAGMA table_info(empresas)")]
        linha = dict(con.execute("SELECT * FROM empresas WHERE empresa = 'LIDL'").fetchone())
        linha.update(empresa=nome, choice_id=f"emp_{nome.lower()}")
        con.execute(f"INSERT INTO empresas ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    [linha[c] for c in cols])
        con.close()

    def test_seguimento_pelo_tecnico_da_entidade(self):
        """13 Out 2026: o técnico da entidade vê e actualiza o seguimento dos seus candidatos nas empresas
        em que fizeram entrevista (incluindo as acrescentadas no dia); é o mesmo registo da empresa."""
        tok = self.sincronizar_com_entidades()
        self.outra_empresa()
        self.entrar(self.t1)
        self.gravar(self.t1, "a", "emp_lidl", "Validado", "10:00")
        con = bd.ligar()
        rec = tokens.criar(con, "rececao", "Receção 1")
        con.close()
        self.entrar(rec)
        self.checkin(rec, ["a|LIDL", "a|STEAM"], feitas=(0,))
        r = self.client.post("/rececao/extra", data={"csrf": self.csrf(rec), "id": "a", "empresa": "OUTRA", "hora": "12:00"})
        self.assertIn("ok=1", r.headers["Location"])
        self.entrar(tok)
        self.assertTrue(self.client.get("/seguimento").headers["Location"].endswith("/seguimento/os-meus-candidatos"))
        html = self.client.get("/seguimento/os-meus-candidatos").get_data(as_text=True)
        self.assertIn('<a href="https://cloudkeep.pt" target="_blank" rel="noopener"><b>Cloudkeep', html)
        self.assertIn("Pessoa a", html)
        self.assertIn("OUTRA", html)
        self.assertIn('hora-ent extra">Extra', html)
        self.assertNotIn("STEAM", html.split("<tbody>")[1])              # inscrita, mas sem entrevista feita
        self.assertNotIn("Pessoa d", html)
        self.assertIn(">Acompanhamento</a>", html)
        vazio = self.app.assinatura_seguimento(None)
        dados = {"csrf": self.csrf(tok), "linha": ["a|LIDL", "a|OUTRA"], "v:0": vazio, "v:1": vazio,
                 "contratado:0": "Sim", "observacoes:0": "Soube pelo candidato"}
        r = self.client.post("/seguimento/os-meus-candidatos", data=dados)
        self.assertEqual(r.status_code, 302)
        r = self.client.post("/seguimento/os-meus-candidatos", data={**dados, "linha": ["d|LIDL"]})
        self.assertIn("não corresponde", r.get_data(as_text=True))
        con = bd.ligar()
        linhas = {(x["id_submissao"], x["empresa"]): x for x in con.execute("SELECT * FROM seguimento")}
        con.close()
        self.assertEqual(list(linhas), [("a", "LIDL")])                    # a linha da OUTRA não foi mexida
        self.assertEqual((linhas["a", "LIDL"]["contratado"], linhas["a", "LIDL"]["alterado_perfil"]), ("Sim", "entidade"))
        # A empresa vê o que o técnico registou; a página dela, aberta antes, não o apaga ao gravar
        self.entrar(self.lidl)
        html = self.client.get("/seguimento/emp_lidl").get_data(as_text=True)
        self.assertIn("pelo/a técnico/a da entidade", html)
        self.assertIn("Soube pelo candidato", html)
        self.assertIn("Entrevista feita", html)
        r = self.client.post("/seguimento/emp_lidl", data={"csrf": self.csrf(self.lidl), "linha": "a|LIDL", "v:0": vazio})
        self.assertEqual(r.status_code, 302)
        con = bd.ligar()
        self.assertEqual(con.execute("SELECT contratado FROM seguimento").fetchone()[0], "Sim")
        con.close()
        # A coordenação vê a entrevista extra no seguimento da OUTRA e no CSV
        self.entrar(self.coord)
        self.assertIn("Pessoa a", self.client.get("/seguimento/emp_outra").get_data(as_text=True))
        csv_txt = self.client.get("/seguimento.csv").get_data(as_text=True)
        self.assertIn("OUTRA;Pessoa a", csv_txt)
        self.assertIn("Acrescentada no dia (12:00);Entrevista feita", csv_txt)
        self.assertIn("Rui Lopes (pelo/a técnico/a da entidade)", csv_txt)

    def test_acompanhamento_avisos(self):
        """14 Out 2026: de hora a hora, o técnico da entidade recebe as alterações ao acompanhamento
        dos seus candidatos feitas pela empresa ou pela coordenação, com CC à Ana Coordenadora; as que
        o técnico faz vão só para ela. Um candidato só volta a um e-mail se mudou desde o último envio."""
        import correio
        mafalda = "coordenacao@exemplo.pt"
        para = lambda m: m["To"].addresses[0].addr_spec  # noqa: E731
        tok = self.sincronizar_com_entidades()
        self.entrar(self.t1)
        self.gravar(self.t1, "a", "emp_lidl", "Validado", "10:00")
        self.entrar(self.lidl)
        vazio = self.app.assinatura_seguimento(None)
        r = self.client.post("/seguimento/emp_lidl", data={"csrf": self.csrf(self.lidl), "linha": "a|LIDL", "v:0": vazio,
                                                           "nova_entrevista:0": "Sim", "contratado:0": "Sim"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.correr(correio, correio.cmd_acompanhamento), [])   # antes do fecho do dia, nada
        con = bd.ligar()
        con.execute("INSERT INTO rececao_fecho (em, por, faltas, emails) VALUES ('2026-10-13T18:00:00+00:00', 'x', 0, 0)")
        con.close()
        caixa = self.correr(correio, correio.cmd_acompanhamento)
        self.assertEqual(len(caixa), 1)
        msg = caixa[0]
        self.assertEqual((para(msg), msg["Cc"], msg["Subject"]), ("rui@zeta.pt", mafalda, correio.ASSUNTO_ACOMPANHAMENTO))
        html = msg.get_body(("html",)).get_content()
        for trecho in ("Olá Rui", "Pessoa a", "LIDL", "Actualizado pela empresa", "Ficou contratado?",
                       "Ana Coordenadora", 'href="https://cloudkeep.pt"'):
            self.assertIn(trecho, html)
        self.assertIn("* Ficou contratado? Sim", msg.get_body(("plain",)).get_content())
        self.assertEqual(self.correr(correio, correio.cmd_acompanhamento), [])   # sem alterações, não repete
        # O que o técnico grava vai só para a Ana, uma vez
        con = bd.ligar()
        actual = con.execute("SELECT * FROM seguimento").fetchone()
        con.close()
        self.entrar(tok)
        r = self.client.post("/seguimento/os-meus-candidatos", data={
            "csrf": self.csrf(tok), "linha": "a|LIDL", "v:0": self.app.assinatura_seguimento(actual),
            "nova_entrevista:0": "Sim", "contratado:0": "Sim", "observacoes:0": "Soube pelo candidato"})
        self.assertIn("ok=1", r.headers["Location"])
        caixa = self.correr(correio, correio.cmd_acompanhamento)
        self.assertEqual([(para(m), m["Cc"], m["Subject"]) for m in caixa],
                         [(mafalda, None, correio.ASSUNTO_ACOMPANHAMENTO_TECNICOS)])
        texto = caixa[0].get_body(("plain",)).get_content()
        for trecho in ("Olá Ana", "Actualizado pelo/a técnico/a Rui Lopes (Gabinete Zeta)",
                       "* Observações: Soube pelo candidato", "  Ficou contratado? Sim"):
            self.assertIn(trecho, texto)
        self.assertEqual(self.correr(correio, correio.cmd_acompanhamento), [])
        # Gravado de novo com os valores já comunicados: não volta a ser enviado
        con = bd.ligar()
        con.execute("UPDATE seguimento SET alterado_perfil = 'empresa', alterado_em = '2026-10-15T09:00:00+00:00'")
        con.close()
        self.assertEqual(self.correr(correio, correio.cmd_acompanhamento), [])
        # A coordenação muda um campo: novo aviso ao técnico, só com esse campo assinalado
        con = bd.ligar()
        con.execute("UPDATE seguimento SET tipo_contrato = 'Termo certo', alterado_perfil = 'coordenador', "
                    "alterado_em = '2026-10-15T10:00:00+00:00'")
        con.close()
        caixa = self.correr(correio, correio.cmd_acompanhamento)
        self.assertEqual([para(m) for m in caixa], ["rui@zeta.pt"])
        texto = caixa[0].get_body(("plain",)).get_content()
        self.assertIn("Actualizado pela coordenação", texto)
        self.assertIn("* Tipo de contrato: Termo certo", texto)
        self.assertNotIn("* Observações", texto)                           # já comunicado à Ana
        # Teste com --para: vai para esse endereço, sem CC, e não marca nada
        self.assertEqual(self.correr(correio, correio.cmd_acompanhamento, para="eu@exemplo.pt"), [])
        con = bd.ligar()
        con.execute("UPDATE seguimento SET duracao_contrato = '6 meses', alterado_em = '2026-10-15T11:00:00+00:00'")
        con.close()
        caixa = self.correr(correio, correio.cmd_acompanhamento, para="eu@exemplo.pt")
        self.assertEqual([(para(m), m["Cc"]) for m in caixa], [("eu@exemplo.pt", None)])
        self.assertEqual(len(self.correr(correio, correio.cmd_acompanhamento)), 1)  # o --para não marcou

    def test_fecho_do_dia(self):
        """13 Out 2026: no fim da Feira, quem não está presente passa a «Não compareceu» e cada técnico
        de entidade recebe o ponto de situação final (faltas e entrevistas feitas, com as extra)."""
        import correio
        self.sincronizar_com_entidades()
        self.outra_empresa()
        con = bd.ligar()
        con.execute("UPDATE submissoes SET cand_telefone = '91000000' || id_submissao")
        rec = tokens.criar(con, "rececao", "Receção 1")
        con.close()
        self.entrar(self.t1)
        self.gravar(self.t1, "a", "emp_lidl", "Validado", "10:00")
        self.gravar(self.t1, "b", "emp_lidl", "Validado", "10:20")
        self.entrar(rec)
        self.checkin(rec, ["a|LIDL", "a|STEAM"], feitas=(0,))              # a STEAM ficou por fazer
        self.client.post("/rececao/extra", data={"csrf": self.csrf(rec), "id": "a", "empresa": "OUTRA"})
        html = self.client.get("/rececao").get_data(as_text=True)
        self.assertIn("Fechar o dia", html)
        html = self.client.get("/rececao/fecho").get_data(as_text=True)
        self.assertIn("Rui Lopes", html)
        self.assertIn("Bia", html)
        self.assertIn("<b>2</b> / 3 passam a «Não compareceu»", html)
        r = self.client.get("/rececao/fecho/email/a")                      # o técnico_id do Rui é a 1.ª submissão
        self.assertIn("'unsafe-inline'", r.headers["Content-Security-Policy"])
        html = r.get_data(as_text=True)
        for trecho in ("Olá Rui", "Não compareceram", "Pessoa b", "91000000b", "LIDL <span", "· às 10:20",
                       "Entrevista feita (10:00)", "Entrevista feita, acrescentada no dia", "Não fez esta entrevista",
                       "«Acompanhamento»", "Ana Coordenadora", "11 de Janeiro de 2027", "/static/gec-simbolo.png"):
            self.assertIn(trecho, html)
        self.assertNotIn("cid:", html)
        self.assertNotIn("Pessoa d", html)                                 # é da Bia
        r = self.client.post("/rececao/fecho", data={"csrf": self.csrf(rec)})
        self.assertIn("erro=", r.headers["Location"])                     # sem confirmar
        r = self.client.post("/rececao/fecho", data={"csrf": self.csrf(rec), "confirmo": "1"})
        self.assertIn("ok=1", r.headers["Location"])
        self.client.post("/rececao/fecho", data={"csrf": self.csrf(rec), "confirmo": "1"})   # só uma vez
        con = bd.ligar()
        self.assertEqual([tuple(x) for x in con.execute("SELECT faltas, emails FROM rececao_fecho")], [(2, 2)])
        faltou = {(x["id_submissao"], x["empresa"]): x["faltou"] for x in con.execute("SELECT * FROM rececao")}
        con.close()
        self.assertEqual((faltou["b", "LIDL"], faltou["d", "LIDL"], faltou["a", "LIDL"]), (1, 1, 0))
        html = self.client.get("/rececao").get_data(as_text=True)
        self.assertIn('flag faltou">Não compareceu', html)
        self.assertNotIn("Fechar o dia:", html)
        self.assertIn("Dia fechado", html)
        self.assertIn("Por enviar", self.client.get("/rececao/fecho").get_data(as_text=True))
        # Marcar presente depois do fecho tira o «Não compareceu»
        self.checkin(rec, ["b|LIDL"])
        con = bd.ligar()
        self.assertEqual(con.execute("SELECT faltou FROM rececao WHERE id_submissao = 'b'").fetchone()[0], 0)
        con.close()
        # O envio (cron): um e-mail a cada técnico, uma só vez; o teste com --para não marca nada
        self.assertEqual(len(self.correr(correio, correio.cmd_fecho, para="eu@exemplo.pt")), 2)
        caixa = self.correr(correio, correio.cmd_fecho)
        self.assertEqual(sorted(m["To"].addresses[0].addr_spec for m in caixa), ["bia@outra.pt", "rui@zeta.pt"])
        rui = next(m for m in caixa if m["To"].addresses[0].addr_spec == "rui@zeta.pt")
        self.assertEqual(rui["Subject"], correio.ASSUNTO_FECHO)
        texto = rui.get_body(("plain",)).get_content()
        self.assertIn("- Pessoa b · 91000000b", texto)                    # a fotografia do fecho: b ainda faltava
        self.assertIn("· OUTRA: Entrevista feita, acrescentada no dia", texto)
        self.assertEqual(self.correr(correio, correio.cmd_fecho), [])
        self.assertIn("Enviado", self.client.get("/rececao/fecho").get_data(as_text=True))

    def test_prazos_da_empresa(self):
        """Link da empresa até 90 dias depois da Feira; CV só até uma semana depois."""
        self.assertEqual(tokens.EXPIRA["empresa"], "2027-01-11 23:59")
        self.entrar(self.t1)
        self.gravar(self.t1, "a", "emp_lidl", "Validado", "10:00")
        self.entrar(self.lidl)
        futuro = self.app._hora_local("2099-01-01 00:00")    # antes da Feira, independentemente do dia em que o teste corre
        with mock.patch.object(self.app, "CV_EMPRESAS_ATE", futuro), mock.patch.object(self.app, "FIM_FEIRA", futuro):
            self.assertIn("/cv/a/emp_lidl", self.client.get("/agenda/emp_lidl").get_data(as_text=True))
            self.assertTrue(self.client.get("/").headers["Location"].endswith("/agenda/emp_lidl"))
        passado = self.app._hora_local("2020-01-01 00:00")
        with mock.patch.object(self.app, "CV_EMPRESAS_ATE", passado), mock.patch.object(self.app, "FIM_FEIRA", passado):
            html = self.client.get("/agenda/emp_lidl").get_data(as_text=True)
            self.assertNotIn("/cv/a/emp_lidl", html)
            self.assertNotIn("/cv/", self.client.get("/seguimento/emp_lidl").get_data(as_text=True))
            r = self.client.get("/cv/a/emp_lidl")
            self.assertEqual(r.status_code, 403)
            self.assertIn("já não disponível", r.get_data(as_text=True))
            self.assertTrue(self.client.get("/").headers["Location"].endswith("/seguimento/emp_lidl"))

    # ── 6 Out 2026: listagens para o dia da Feira ──
    def test_listagens_da_coordenacao(self):
        self.entrar(self.t1)
        self.gravar(self.t1, "a", "emp_lidl", "Validado", "10:20")
        self.gravar(self.t1, "a", "emp_steam", "Validado")          # lista de espera
        self.gravar(self.t1, "b", "emp_lidl", "Validado", "10:00")
        self.assertEqual(self.client.get("/listagens/rececao-empresas").status_code, 403)
        self.entrar(self.coord)
        html = self.client.get("/candidaturas").get_data(as_text=True)
        for url in ("/listagens/rececao-empresas", "/listagens/rececao-candidatos", "/listagens/tecnicos"):
            self.assertIn(url, html)
        html = self.client.get("/listagens/rececao-empresas").get_data(as_text=True)
        self.assertIn("banner.jpg", html)
        self.assertLess(html.index("Pessoa b"), html.index("Pessoa a"))      # 10:00 antes de 10:20
        self.assertIn("Espera 1", html)                                         # a, na STEAM
        self.assertIn("ainda por validar", html)                                # d continua por validar
        self.assertIn("Pessoa d", html)                                         # 12 Out: mas consta, no fim da LIDL
        self.assertIn(">Por validar<", html)
        self.assertLess(html.index("Pessoa a"), html.index("Pessoa d"))
        html = self.client.get("/listagens/rececao-candidatos").get_data(as_text=True)
        self.assertEqual(html.count("<b>Pessoa a</b>"), 1)                       # uma linha com as duas empresas
        self.assertLess(html.index("Pessoa a"), html.index("Pessoa b"))
        self.assertIn("Pessoa d", html)                                         # por validar também consta
        self.assertIn('class="hora-ent pv">Por validar', html)
        html = self.client.get("/listagens/tecnicos").get_data(as_text=True)
        self.assertIn("Telefone técnico/a", html)
        self.assertEqual(html.count('class="folha"'), 2)                         # uma folha por empresa
        self.assertGreaterEqual(self.acessos("listagem"), 3)


    def test_prolongar_links_e_renovar_cookie(self):
        """6 Out 2026: os links da coordenação passam a ir até 11/01/2027 e o cookie acompanha."""
        self.assertEqual(tokens.EXPIRA["coordenador"], "2027-01-11 23:59")
        self.entrar(self.coord)
        con = bd.ligar()
        n = con.execute("UPDATE tokens SET expira_em = ? WHERE perfil = 'coordenador' AND revogado = 0 AND expira_em > ?",
                        (tokens.expira_utc("2027-01-11 23:59"), bd.agora_utc())).rowcount
        con.close()
        self.assertEqual(n, 1)                                   # o expirado não é prolongado
        r = self.client.get("/candidaturas")
        self.assertIn("2027", r.headers.get("Set-Cookie", ""))
        self.assertIn("HttpOnly", r.headers["Set-Cookie"])

    # ── 7 Out 2026 ──
    NOTA_23SET = "Duplicado de cmufa69ej007x01owtb8cuj6i: anulada automaticamente (mesmo NIF ou documento)."

    def test_nota_de_duplicado_sai_ao_validar(self):
        """Ao voltar a «Por validar» ou ser validada, a nota automática sai; o resto fica."""
        con = bd.ligar()
        con.execute("UPDATE candidaturas SET validacao = 'Anulado', validacao_alterada_por = 'Coord', observacoes = ? "
                    "WHERE id_submissao = 'b'", (f"Técnico avisado\r\n {self.NOTA_23SET}\r\nConfirmado por telefone",))
        con.close()
        self.entrar(self.coord)
        self.gravar(self.coord, "b", "emp_lidl", "Anulado", observacoes=self.cand("b", "LIDL")["observacoes"])
        self.assertIn("Duplicado de", self.cand("b", "LIDL")["observacoes"])        # anulada: a nota fica
        self.gravar(self.coord, "b", "emp_lidl", "Validado", observacoes=self.cand("b", "LIDL")["observacoes"])
        self.assertEqual(self.cand("b", "LIDL")["observacoes"], "Técnico avisado\nConfirmado por telefone")

    def test_sincronizacao_limpa_notas_das_nao_anuladas(self):
        con = bd.ligar()
        con.execute("UPDATE candidaturas SET observacoes = ? WHERE id_submissao = 'b'", (self.NOTA_23SET,))
        con.execute("UPDATE candidaturas SET validacao = 'Anulado', observacoes = ? WHERE id_submissao = 'd'",
                    (self.NOTA_23SET,))
        self.assertEqual(ex.limpar_notas_automaticas(con), 1)
        self.assertIsNone(self.cand("b", "LIDL")["observacoes"])
        self.assertEqual(self.cand("d", "LIDL")["observacoes"], self.NOTA_23SET)
        self.assertEqual(ex.limpar_notas_automaticas(con), 0)
        con.close()

    def test_catalogo_de_ofertas(self):
        from reportlab.pdfgen import canvas
        caminho = os.path.join(TMP.name, "catalogo.pdf")
        c = canvas.Canvas(caminho)
        for i in range(1, 31):
            c.drawString(100, 700, f"Pagina {i}")
            c.showPage()
        c.save()
        with mock.patch.object(self.app, "CATALOGO", caminho):
            self.entrar(self.t1)
            html = self.client.get("/c/a/emp_lidl").get_data(as_text=True)
            self.assertIn("/ofertas/emp_lidl.pdf", html)
            r = self.client.get("/ofertas/emp_lidl.pdf")
            self.assertEqual(r.status_code, 200)
            from pypdf import PdfReader
            paginas = PdfReader(io.BytesIO(r.data)).pages
            self.assertEqual(len(paginas), 2)                                   # LIDL: pp. 23–24
            self.assertIn("Pagina 23", paginas[0].extract_text())
            self.entrar(self.lidl)
            self.assertEqual(self.client.get("/ofertas/emp_lidl.pdf").status_code, 200)
            self.assertEqual(self.client.get("/ofertas/emp_steam.pdf").status_code, 403)
        with mock.patch.object(self.app, "CATALOGO", os.path.join(TMP.name, "nao-existe.pdf")):
            self.entrar(self.t1)
            self.assertNotIn("/ofertas/", self.client.get("/c/a/emp_lidl").get_data(as_text=True))
            self.assertEqual(self.client.get("/ofertas/emp_lidl.pdf").status_code, 404)

    def test_avaliacao_empresas(self):
        """Questionário de avaliação do evento (13 Out 2026): a empresa responde
        no seu link, só à sua, e cada pessoa de RH à parte; a coordenação
        consulta e exporta."""
        notas_ok = {k: "3" for k, _ in self.app.PERGUNTAS}
        nova = "/avaliacao/emp_lidl/nova"
        self.entrar(self.lidl)
        self.assertTrue(self.client.get("/avaliacao").headers["Location"].endswith("/avaliacao/emp_lidl"))
        self.assertTrue(self.client.get("/avaliacao/emp_lidl").headers["Location"].endswith(nova))  # sem respostas
        html = self.client.get(nova).get_data(as_text=True)
        self.assertIn("Matching candidatos vs oferta de emprego", html)
        self.assertIn("Plataforma de acesso à agenda e ao acompanhamento dos candidatos", html)
        for url in ("/avaliacao/emp_steam", "/avaliacao/emp_steam/nova"):
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.assertEqual(self.client.get("/avaliacao.csv").status_code, 403)
        csrf = self.csrf(self.lidl)
        r = self.client.post(nova, data={"csrf": csrf, **notas_ok, "global": ""})
        self.assertIn("Falta responder", r.get_data(as_text=True))
        r = self.client.post(nova, data={"csrf": csrf, **notas_ok, "global": "9", "respondido_por": "Ana RH"})
        self.assertIn("Falta responder", r.get_data(as_text=True))
        r = self.client.post(nova, data={"csrf": csrf, **notas_ok, "global": "4", "respondido_por": ""})
        self.assertIn("Indique o nome", r.get_data(as_text=True))
        r = self.client.post(nova, data={"csrf": csrf, **notas_ok, "global": "2", "respondido_por": "Ana RH"})
        self.assertIn("diga-nos o motivo", r.get_data(as_text=True))        # nota < 3 sem observação
        r = self.client.post(nova, data={"csrf": csrf, **notas_ok, "global": "4", "positivos": "Boa organização",
                                         "respondido_por": "Ana RH"})
        self.assertIn("ok=1", r.headers["Location"])
        r = self.client.post(nova, data={"csrf": csrf, **notas_ok, "global": "4", "respondido_por": "Rui RH"})
        self.assertIn("ok=1", r.headers["Location"])                         # outra pessoa: não substitui
        con = bd.ligar()
        ids = {x["respondido_por"]: x["id"] for x in con.execute("SELECT * FROM avaliacao")}
        con.close()
        self.assertEqual(set(ids), {"Ana RH", "Rui RH"})
        html = self.client.get("/avaliacao/emp_lidl").get_data(as_text=True)
        self.assertIn("Respostas já enviadas (2)", html)
        r = self.client.post(f"/avaliacao/emp_lidl/{ids['Ana RH']}",
                             data={"csrf": csrf, **notas_ok, "global": "2", "obs:global": "Pouca afluência",
                                   "obs:horario": "ignorada", "positivos": "Boa organização",
                                   "respondido_por": "Ana RH"})
        self.assertIn("ok=1", r.headers["Location"])                         # correcção da própria resposta
        con = bd.ligar()
        self.assertEqual(sorted((x["respondido_por"], x["global"]) for x in con.execute("SELECT * FROM avaliacao")),
                         [("Ana RH", 2), ("Rui RH", 4)])
        con.close()
        self.assertEqual(self.client.get("/avaliacao/emp_lidl/999").status_code, 404)
        self.entrar(self.coord)
        html = self.client.get("/avaliacao").get_data(as_text=True)
        self.assertIn("<b>1</b> / 2 empresas responderam", html)
        self.assertIn("<b>2</b> respostas no total", html)
        self.assertIn("Boa organização", html)
        self.assertIn("Pouca afluência", html)
        self.assertNotIn("ignorada", html)                                     # só se guarda nas notas < 3
        self.assertIn("Ainda não respondeu", html)
        self.assertEqual(self.client.get("/avaliacao/emp_steam/nova").status_code, 200)
        csv_txt = self.client.get("/avaliacao.csv").get_data(as_text=True)
        self.assertIn("LIDL;Ana RH;3;3;3;3;3;3;3;3;3;3;2;Avaliação global do evento (2): Pouca afluência;Boa organização;;;", csv_txt)
        self.assertIn("LIDL;Rui RH;", csv_txt)

    def test_avaliacao_migra_uma_por_empresa(self):
        """A tabela da 1.ª versão (empresa como chave) passa a ter id, com as respostas."""
        con = bd.ligar()
        con.execute("DROP TABLE avaliacao")
        con.execute("CREATE TABLE avaliacao (empresa TEXT PRIMARY KEY, global INTEGER, positivos TEXT, "
                    "obs_notas TEXT, respondido_por TEXT NOT NULL, alterado_em TEXT NOT NULL, alterado_por TEXT NOT NULL)")
        con.execute("INSERT INTO avaliacao VALUES ('LIDL', 4, 'Bom', NULL, 'Ana', '2026-10-13T10:00:00+00:00', 'LIDL')")
        bd.garantir_schema(con)
        bd.garantir_schema(con)                                                 # 2.ª vez não faz nada
        r = dict(con.execute("SELECT * FROM avaliacao").fetchone())
        con.close()
        self.assertEqual((r["id"], r["empresa"], r["global"], r["positivos"], r["respondido_por"], r["criada_em"]),
                         (1, "LIDL", 4, "Bom", "Ana", "2026-10-13T10:00:00+00:00"))

    def test_avaliacao_feira_parceiros(self):
        """Avaliação da organização (19 Out 2026): triagem, encaminhamento e coordenação
        respondem ao mesmo questionário, uma vez por pessoa mesmo com dois links; a
        coordenação vê os resultados; as empresas não."""
        con = bd.ligar()
        ent = tokens.criar(con, "entidade", "Técnica Maria Um", ref_submissao="a", email="t1@x.pt")  # = Técnica Um
        con.close()
        notas = {k: "4" for k, _r, _d in self.app.PERGUNTAS_FEIRA}
        tri = {k: "4" for k in bd.COLUNAS_TRIAGEM}
        coord = {k: "4" for k in bd.COLUNAS_COORDENACAO}
        self.entrar(self.t1)
        html = self.client.get("/candidaturas").get_data(as_text=True)
        self.assertIn("Responder à avaliação", html)                       # aviso na lista
        html = self.client.get("/avaliacao-feira").get_data(as_text=True)
        self.assertIn("Utilidade da plataforma na gestão da Feira", html)
        self.assertIn("A triagem das candidaturas", html)                   # bloco da triagem
        self.assertNotIn("A coordenação da Feira", html)
        self.assertIn('href="/avaliacao-feira" aria-current="page"', html)
        self.assertEqual(self.client.get("/avaliacao-feira/resultados").status_code, 403)
        self.assertEqual(self.client.get("/avaliacao-feira.csv").status_code, 403)
        csrf = self.csrf(self.t1)
        r = self.client.post("/avaliacao-feira", data={"csrf": csrf, **notas, "logistica": "",
                                                        "correu_bem": "x", "melhorar": "y"})
        self.assertIn("escolha N/A", r.get_data(as_text=True))
        r = self.client.post("/avaliacao-feira", data={"csrf": csrf, **notas, "correu_bem": "Tudo"})
        self.assertIn("O que poderá correr melhor?", r.get_data(as_text=True))
        r = self.client.post("/avaliacao-feira", data={"csrf": csrf, **notas, "correu_bem": "x", "melhorar": "y"})
        self.assertIn("Informação para decidir", r.get_data(as_text=True))   # o bloco da triagem é obrigatório
        r = self.client.post("/avaliacao-feira", data={"csrf": csrf, **notas, **tri, "tri_comparacao": "9",
                                                        "correu_bem": "x", "melhorar": "y"})
        self.assertIn("Em comparação com a forma", r.get_data(as_text=True))
        r = self.client.post("/avaliacao-feira", data={"csrf": csrf, **notas, **tri, "logistica": "0", "planeamento": "2",
                                                        "correu_bem": "A receção", "melhorar": "Mais tempo"})
        self.assertIn("ok=1", r.headers["Location"])
        self.assertNotIn("Responder à avaliação", self.client.get("/candidaturas").get_data(as_text=True))
        # o link da entidade da mesma pessoa abre a mesma resposta e corrige-a
        self.entrar(ent)
        html = self.client.get("/avaliacao-feira").get_data(as_text=True)
        self.assertIn("Já respondeu", html)
        self.assertIn("deixe-nos uma breve explicação (opcional)", html)    # caixa das notas 1 e 2, não obrigatória
        self.assertIn("Mais tempo", html)
        self.assertIn("A triagem das candidaturas", html)                   # o papel conta em todos os links
        r = self.client.post("/avaliacao-feira", data={"csrf": self.csrf(ent), **notas, **tri, "logistica": "0",
                                                        "planeamento": "2", "obs:planeamento": "Prazos curtos",
                                                        "obs:global": "ignorada", "correu_bem": "A receção",
                                                        "melhorar": "Mais tempo", "comentarios": "Parabéns",
                                                        "tri_comparacao": "5", "tri_sugestao": "Filtro por oferta"})
        self.assertIn("ok=1", r.headers["Location"])
        self.entrar(self.t2)
        self.client.post("/avaliacao-feira", data={"csrf": self.csrf(self.t2), **notas, **tri, "planeamento": "5",
                                                    "correu_bem": "Plataforma", "melhorar": "Sinalética"})
        # A coordenação respondeu antes de existir o seu bloco: a resposta fica e só falta completá-lo
        con = bd.ligar()
        con.execute("INSERT INTO avaliacao_feira (pessoa, nome, grupo, planeamento, comunicacao, logistica, dia_evento, "
                    "entidades, global, plataforma_uso, plataforma_util, correu_bem, melhorar, criada_em, alterado_em, "
                    "alterado_por, alterado_perfil) VALUES ('coord coord', 'Coord', 'coordenacao', 4, 4, 4, 4, 4, 4, 4, 4, "
                    "'Equipa', 'Prazos', '2026-10-19T10:00:00+00:00', '2026-10-19T10:00:00+00:00', 'Coord', 'coordenador')")
        con.close()
        self.entrar(self.coord)
        html = self.client.get("/candidaturas").get_data(as_text=True)
        self.assertIn("Completar a avaliação", html)
        self.assertIn("a coordenação da feira", html)
        html = self.client.get("/avaliacao-feira").get_data(as_text=True)
        self.assertIn('class="novo-av">Novo', html)
        self.assertIn("Equipa", html)                                         # a resposta anterior vem preenchida
        r = self.client.post("/avaliacao-feira", data={"csrf": self.csrf(self.coord), **notas, **coord,
                                                        "coord_recursos": "2", "obs:coord_recursos": "Pouca gente",
                                                        "coord_decisao": "Começar mais cedo",
                                                        "correu_bem": "Equipa", "melhorar": "Prazos"})
        self.assertIn("ok=1", r.headers["Location"])
        self.assertNotIn("Completar a avaliação", self.client.get("/candidaturas").get_data(as_text=True))
        con = bd.ligar()
        rows = {r["nome"]: dict(r) for r in con.execute("SELECT * FROM avaliacao_feira")}
        con.close()
        self.assertEqual(len(rows), 3)
        um = rows["Técnica Um"]
        self.assertEqual((um["grupo"], um["logistica"], um["planeamento"], um["comentarios"], um["alterado_perfil"]),
                         ("triagem", 0, 2, "Parabéns", "entidade"))
        self.assertEqual((rows["Técnico Dois"]["grupo"], rows["Coord"]["grupo"]), ("triagem", "coordenacao"))
        html = self.client.get("/avaliacao-feira/resultados").get_data(as_text=True)
        self.assertIn("<b>3</b> respostas", html)
        self.assertIn("<td class=\"dir num\"><b>3.7</b></td>", html)          # planeamento: (2+5+4)/3
        self.assertIn("Sinalética", html)
        self.assertIn("Parabéns", html)
        self.assertIn("Prazos curtos", html)
        self.assertNotIn("ignorada", html)                                    # só se guarda nas notas 1 e 2
        self.assertIn("Pouca gente", html)
        self.assertIn("Filtro por oferta", html)
        self.assertIn("Começar mais cedo", html)
        self.assertIn("<b>2</b> responderam", html)                           # bloco da triagem: Um e Dois
        html = self.client.get("/avaliacao").get_data(as_text=True)               # dois botões irmãos no cabeçalho
        self.assertIn('href="/avaliacao" aria-current="page">Avaliação · Empresas', html)
        self.assertIn('href="/avaliacao-feira/resultados">Avaliação · Técnicos e coordenação', html)
        html = self.client.get("/avaliacao-feira/resultados").get_data(as_text=True)
        self.assertIn('href="/avaliacao-feira/resultados" aria-current="page">Avaliação · Técnicos e coordenação', html)
        self.assertNotIn('aria-current="page">Avaliação · Empresas', html)
        csv_txt = self.client.get("/avaliacao-feira.csv").get_data(as_text=True)
        self.assertIn("Técnica Um;Técnicos de triagem;2;4;N/A;4;4;4;4;4;4;4;4;4;4;4;4;5;;;;;"
                      "Planeamento e preparação da Feira (2): Prazos curtos;A receção;Mais tempo;Parabéns;Filtro por oferta;;",
                      csv_txt)
        # quem só encaminhou não vê os blocos
        con = bd.ligar()
        rui = tokens.criar(con, "entidade", "Rui Lopes", ref_submissao="b", email="rui@x.pt")
        con.close()
        self.entrar(rui)
        html = self.client.get("/avaliacao-feira").get_data(as_text=True)
        self.assertNotIn("A triagem das candidaturas", html)
        self.assertNotIn("A coordenação da Feira", html)
        self.entrar(self.lidl)
        self.assertEqual(self.client.get("/avaliacao-feira").status_code, 403)

    def test_email_avaliacao_feira(self):
        """Aviso do questionário aos técnicos (19 Out 2026): um e-mail por pessoa, com os
        seus links (triagem e entidade) vindos da entrada; e-mail da entidade ou do
        lembrete de triagem; não repete nem envia a quem já respondeu."""
        import correio
        con = bd.ligar()
        ent = tokens.criar(con, "entidade", "Técnica Maria Um", ref_submissao="a", email="t1@x.pt")
        so_ent = tokens.criar(con, "entidade", "Rui Lopes", ref_submissao="b", email="rui@x.pt")
        con.execute("INSERT INTO envios (em, tipo, destinatario, ref, assunto, resultado) VALUES "
                    "('2026-10-08T10:00:00+00:00', 'triagem', 'dois@x.pt', 'dois@x.pt|Técnico Dois', 'x', 'enviado')")
        con.close()
        base = correio.BASE_URL
        entrada = (f"# triagem\nLIDL — Técnica Um (tecnico): {base}/?t={self.t1}\n"
                   f"STEAM — Técnico Dois (tecnico): {base}/?t={self.t2}\n"
                   f"Técnica Maria Um\tEnt\tt1@x.pt\t\t2\t{base}/?t={ent}\n"
                   f"Falso\tEnt\tf@x.pt\t\t1\t{base}/?t=inventadoinventadoinventado\n")
        with mock.patch("sys.stdin", io.StringIO(entrada)):
            caixa = self.correr(correio, correio.cmd_avaliacao)
        por = {str(m["To"].addresses[0].addr_spec): m for m in caixa}
        self.assertEqual(set(por), {"t1@x.pt", "dois@x.pt"})       # o Rui não tem link na entrada: retido
        txt = por["t1@x.pt"].get_body(("plain",)).get_content()
        self.assertIn(f"{base}/?t={self.t1}", txt)
        self.assertIn(f"{base}/?t={ent}", txt)
        self.assertIn("basta responder uma vez", txt)
        self.assertNotIn(so_ent, txt)
        self.assertIn("Dashboard de triagem", por["dois@x.pt"].get_body(("html",)).get_content())
        self.assertNotIn(f"?t={self.t1}", por["dois@x.pt"].get_body(("plain",)).get_content())
        with mock.patch("sys.stdin", io.StringIO(entrada)):
            self.assertEqual(self.correr(correio, correio.cmd_avaliacao), [])       # não repete
        con = bd.ligar()
        con.execute("DELETE FROM envios WHERE tipo = 'avaliacao-feira'")
        con.execute("INSERT INTO avaliacao_feira (pessoa, nome, grupo, criada_em, alterado_em, alterado_por, "
                    "alterado_perfil) VALUES ('tecnica um', 'Técnica Um', 'triagem', 'x', 'x', 'x', 'tecnico')")
        con.close()
        with mock.patch("sys.stdin", io.StringIO(entrada)):
            caixa = self.correr(correio, correio.cmd_avaliacao)
        self.assertEqual([str(m["To"].addresses[0].addr_spec) for m in caixa], ["dois@x.pt"])   # já respondeu

    def test_rececao_candidato_sem_inscricao(self):
        """A receção regista quem chega sem inscrição (13 Out 2026), presente e com
        a primeira entrevista; depois trata-o como aos outros."""
        con = bd.ligar()
        rec = tokens.criar(con, "rececao", "Receção 1")
        con.close()
        self.entrar(rec)
        csrf = self.csrf(rec)
        r = self.client.post("/rececao/novo", data={"csrf": csrf, "nome": "  ", "empresa": "LIDL"})
        self.assertIn("erro=", r.headers["Location"])
        r = self.client.post("/rececao/novo", data={"csrf": csrf, "nome": "Zé Novo", "empresa": "NAO EXISTE"})
        self.assertIn("erro=", r.headers["Location"])
        r = self.client.post("/rececao/novo", data={"csrf": csrf, "nome": "pessoa A", "empresa": "LIDL"})
        self.assertIn("erro=", r.headers["Location"])                           # já está na lista
        r = self.client.post("/rececao/novo", data={"csrf": csrf, "nome": "Zé  Novo", "telefone": "912345678",
                                                    "empresa": "LIDL", "hora": "15:00"})
        self.assertIn("ok=1", r.headers["Location"])
        self.assertTrue(r.headers["Location"].endswith("#p-avulso-1"))
        html = self.client.get("/rececao").get_data(as_text=True)
        self.assertIn('<b>Zé Novo</b> <span class="flag">Sem inscrição</span>', html)
        self.assertEqual(self.client.get("/rececao").status_code, 200)
        con = bd.ligar()
        x = dict(con.execute("SELECT * FROM rececao WHERE id_submissao = 'avulso-1'").fetchone())
        con.close()
        self.assertEqual((x["empresa"], x["presente"], x["entrevistado"], x["extra"], x["horario_dia"]),
                         ("LIDL", 1, 1, 1, "15:00"))
        r = self.client.post("/rececao/extra", data={"csrf": csrf, "id": "avulso-1", "empresa": "STEAM"})
        self.assertIn("ok=1", r.headers["Location"])                            # segunda entrevista
        r = self.client.post("/rececao", data={"csrf": csrf, "cand": ["avulso-1|LIDL", "avulso-1|STEAM"],
                                               "presente": "1", "entrevistado:1": "1",
                                               "empresa_dia:0": "LIDL", "horario_dia:0": "15:00", "empresa_dia:1": "STEAM"},
                             headers={"X-Pedido": "fetch"})                      # desiste da LIDL
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["resumo"]["pessoas"], 4)
        self.entrar(self.coord)
        csv_txt = self.client.get("/rececao.csv").get_data(as_text=True)
        self.assertIn("Zé Novo;912345678;Sim;LIDL;Extra;LIDL;15:00;Não;Sim", csv_txt)
        self.assertIn("Zé Novo;912345678;Sim;STEAM;Extra;STEAM;;Sim;Sim", csv_txt)
        self.assertIn(";Não\r\n", csv_txt)                                        # os inscritos: «Sem inscrição» = Não

    def test_rececao_relatorio_pdf_e_graficos(self):
        """Relatório do dia (14 Out 2026): presentes, inscritos no dia e faltas, com gráficos; só a coordenação."""
        self.entrar(self.t1)
        self.gravar(self.t1, "a", "emp_lidl", "Validado", "10:20")
        self.gravar(self.t1, "a", "emp_steam", "Validado")
        con = bd.ligar()
        rec = tokens.criar(con, "rececao", "Receção 1")
        con.close()
        self.entrar(rec)
        csrf = self.csrf(rec)
        self.client.post("/rececao", data={"csrf": csrf, "cand": ["a|LIDL", "a|STEAM"], "presente": "1",
                                           "entrevistado:0": "1", "empresa_dia:0": "LIDL", "empresa_dia:1": "STEAM"},
                         headers={"X-Pedido": "fetch"})
        self.client.post("/rececao/novo", data={"csrf": csrf, "nome": "Zé Novo", "empresa": "LIDL", "hora": "15:00"})
        self.assertNotIn("rececao-analise", self.client.get("/rececao").get_data(as_text=True))   # a receção não vê a análise
        for url in ("/rececao/relatorio",):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.entrar(self.coord)
        html = self.client.get("/rececao").get_data(as_text=True)
        self.assertIn("rececao-analise", html)
        self.assertIn('href="/rececao/relatorio"', html)
        r = self.client.get("/rececao/relatorio")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("1. Candidatos presentes <small>(1)", html)
        self.assertIn("2. Inscritos no dia <small>(1)", html)
        self.assertIn("3. Candidatos que não compareceram", html)
        self.assertIn("<b>Zé Novo</b>", html)
        self.assertIn("<b>Pessoa a</b>", html)
        self.assertIn("banner.jpg", html)
        self.assertIn("<svg", html)
        self.assertNotIn("style=", html)                                         # a CSP não deixa estilos em linha
        self.assertGreaterEqual(self.acessos("listagem"), 1)

    def test_rececao_check_in(self):
        self.entrar(self.t1)
        self.gravar(self.t1, "a", "emp_lidl", "Validado", "10:20")
        self.gravar(self.t1, "a", "emp_steam", "Validado")                     # lista de espera
        con = bd.ligar()
        rec = tokens.criar(con, "rececao", "Receção 1")
        con.close()
        self.entrar(rec)
        self.assertTrue(self.client.get("/").headers["Location"].endswith("/rececao"))
        html = self.client.get("/rececao").get_data(as_text=True)
        self.assertEqual(html.count("<b>Pessoa a</b>"), 1)
        self.assertIn("Pessoa b", html)                                         # por validar também consta (12 Out)
        self.assertIn("Pessoa d", html)
        self.assertIn('hora-ent pv">Por validar', html)
        self.assertNotIn("204813751", html)
        self.assertNotIn('href="/candidaturas"', html)
        for url in ("/candidaturas", "/c/a/emp_lidl", "/cv/a/emp_lidl", "/agenda/emp_lidl", "/rececao.csv",
                    "/listagens/rececao-candidatos", "/comunicacao", "/exportar.csv"):
            self.assertEqual(self.client.get(url).status_code, 403, url)
        dados = {"csrf": self.csrf(rec), "cand": ["a|LIDL", "a|STEAM"], "entrevistado:0": "1",
                 "empresa_dia:0": "LIDL", "horario_dia:0": "10:20", "empresa_dia:1": "LIDL", "horario_dia:1": "11:40"}
        r = self.client.post("/rececao", data=dados, headers={"X-Pedido": "fetch"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["resumo"], {"pessoas": 3, "presentes": 1, "entrevistas": 4, "feitas": 1})
        con = bd.ligar()
        linhas = {x["empresa"]: x for x in con.execute("SELECT * FROM rececao")}
        con.close()
        self.assertEqual((linhas["LIDL"]["presente"], linhas["LIDL"]["entrevistado"], linhas["LIDL"]["empresa_dia"]),
                         (1, 1, None))                                          # entrevista feita => presente
        self.assertEqual((linhas["STEAM"]["empresa_dia"], linhas["STEAM"]["horario_dia"]), ("LIDL", "11:40"))
        self.assertEqual(self.cand("a", "STEAM")["horario_entrevista"], None)  # a candidatura não muda
        r = self.client.post("/rececao", data={**dados, "horario_dia:0": "25:99"}, headers={"X-Pedido": "fetch"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/rececao", data={**dados, "cand": ["zz|LIDL"]})   # não existe
        self.assertIn("erro=", r.headers["Location"])
        r = self.client.post("/rececao", data={"csrf": self.csrf(rec), "cand": ["b|LIDL"], "presente": "1",
                                               "empresa_dia:0": "LIDL", "horario_dia:0": ""},
                             headers={"X-Pedido": "fetch"})                      # por validar: pode fazer check-in
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(self.acessos("rececao-alterar"), 2)
        # Nova entrevista no dia (a «a» tem duas: LIDL e STEAM)
        r = self.client.post("/rececao/extra", data={"csrf": self.csrf(rec), "id": "a", "empresa": "LIDL", "hora": "12:00"})
        self.assertIn("erro=", r.headers["Location"])                           # já tem entrevista aí
        r = self.client.post("/rececao/extra", data={"csrf": self.csrf(rec), "id": "a", "empresa": "NAO EXISTE"})
        self.assertIn("erro=", r.headers["Location"])
        con = bd.ligar()
        cols = [r[1] for r in con.execute("PRAGMA table_info(empresas)")]
        linha = dict(con.execute("SELECT * FROM empresas WHERE empresa = 'LIDL'").fetchone())
        for nome in ("OUTRA", "QUARTA"):
            linha.update(empresa=nome, choice_id=f"emp_{nome.lower()}")
            con.execute(f"INSERT INTO empresas ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        [linha[c] for c in cols])
        con.close()
        r = self.client.post("/rececao/extra", data={"csrf": self.csrf(rec), "id": "a", "empresa": "OUTRA", "hora": "12:00"})
        self.assertIn("ok=1", r.headers["Location"])
        html = self.client.get("/rececao").get_data(as_text=True)
        self.assertIn('hora-ent extra">Extra', html)
        r = self.client.post("/rececao/extra", data={"csrf": self.csrf(rec), "id": "a", "empresa": "QUARTA"})
        self.assertIn("ok=1", r.headers["Location"])                            # 4.ª entrevista: sem limite (13 Out)
        r = self.client.post("/rececao/extra", data={"csrf": self.csrf(rec), "id": "a", "empresa": "QUARTA"})
        self.assertIn("erro=", r.headers["Location"])                           # mas não repete empresa
        html = self.client.get("/rececao").get_data(as_text=True)
        self.assertNotIn('id="extra-a"', html)                                  # já não sobram empresas
        self.entrar(self.coord)
        self.assertIn("/rececao", self.client.get("/candidaturas").get_data(as_text=True))
        csv_txt = self.client.get("/rececao.csv").get_data(as_text=True)
        self.assertIn("Pessoa a;", csv_txt)
        self.assertIn(";STEAM;Espera 1;LIDL;11:40;Não", csv_txt)
        self.assertIn("Pessoa b;", csv_txt)
        self.assertIn(";LIDL;Por validar;LIDL;;Não", csv_txt)

    def test_email_ponto_de_situacao(self):
        """Um grupo por técnico da entidade, por candidato e depois por empresa; o
        e-mail leva o cabeçalho da Feira e os três logótipos embebidos."""
        import correio
        self.sincronizar_com_entidades()
        self.entrar(self.t1)
        self.gravar(self.t1, "b", "emp_lidl", "Validado", "10:00")
        self.gravar(self.t1, "a", "emp_lidl", "Anulado", observacoes="Sem documentos")   # c|STEAM: anulada como repetida
        con = bd.ligar()
        grupos = {g["tecnico"]: g for g in correio.pontos_de_situacao(con)}
        con.close()
        rui = grupos["Rui Lopes"]
        self.assertEqual([(l["candidato"], l["empresa"]) for l in rui["linhas"]],
                         [("Pessoa a", "LIDL"), ("Pessoa a", "STEAM"), ("Pessoa b", "LIDL"), ("Pessoa c", "STEAM")])
        self.assertEqual(rui["resumo"], {"pendente": 1, "marcado": 1, "espera": 0, "anulado": 2})
        self.assertEqual(rui["linhas"][2]["entrevista"], "13 Out, 10:00–10:20")
        msg = correio.compor("feira@exemplo.pt", rui["email"], "Assunto", "estado", nome_para="Rui Lopes",
                             g=rui, nome="Rui", data=datetime.now(bd.TZ), ate=correio.ESTADO_ATE, base_url="x")
        html = msg.get_body(("html",)).get_content()
        for trecho in ("Carnide 2026 · 13 Out · Lispolis", "#FFC629", "cid:gec@fe2026", "cid:redemprega@fe2026",
                       "cid:bipzip@fe2026", "Sem documentos", "Cloudkeep"):
            self.assertIn(trecho, html)
        self.assertEqual(len([p for p in msg.walk() if p.get_content_type() == "image/png"]), 3)
        self.assertIn("Pessoa b · LIDL · Validada · 13 Out, 10:00–10:20", msg.get_body(("plain",)).get_content())

    def test_email_para_no_limite_do_servidor(self):
        """Com uma recusa temporária (451, limite da Hostinger), a ronda pára e a seguinte retoma."""
        import smtplib
        import correio
        self.sincronizar_com_entidades()
        enviados = []

        class Falso:
            def __init__(self, cfg):
                pass

            def enviar(self, msg):
                if enviados:
                    raise smtplib.SMTPDataError(451, b"4.7.1 Ratelimit exceeded")
                enviados.append(msg["To"])

            def fechar(self):
                pass
        args = mock.Mock(simular=False, para=None, limite=None, forcar=False, so=None)
        cfg = {"MAIL_USER": "feira@exemplo.pt"}
        with mock.patch.object(correio, "Carteiro", Falso), mock.patch.object(correio, "ler_config", return_value=cfg), \
                mock.patch.object(correio, "PAUSA", 0), mock.patch.object(correio, "ESTADO_ATE", datetime(2099, 1, 1, tzinfo=bd.TZ)):
            self.assertEqual(correio.cmd_estado(args), 1)
            self.assertEqual(len(enviados), 1)
            enviados.clear()
            correio.cmd_estado(args)                             # o 1.º já foi enviado: é saltado
        con = bd.ligar()
        res = [r[0] for r in con.execute("SELECT resultado FROM envios ORDER BY id")]
        con.close()
        self.assertEqual(res[0], "enviado")
        self.assertTrue(res[1].startswith("erro: (451"))
        self.assertEqual(res[2], "enviado")                    # o 2.º técnico, na ronda seguinte

    # ── 8 Out 2026: candidatos, links novos e sem repetições ──
    def correio_falso(self, correio):
        """Carteiro que só guarda as mensagens; devolve a lista e os patches a aplicar."""
        caixa = []

        class Falso:
            def __init__(self, cfg):
                pass

            def enviar(self, msg):
                caixa.append(msg)

            def fechar(self):
                pass
        cfg = {"MAIL_USER": "feira@exemplo.pt"}
        return caixa, [mock.patch.object(correio, "Carteiro", Falso),
                       mock.patch.object(correio, "ler_config", return_value=cfg),
                       mock.patch.object(correio, "PAUSA", 0),
                       mock.patch.object(correio, "ESTADO_ATE", datetime(2099, 1, 1, tzinfo=bd.TZ)),
                       mock.patch.object(correio, "LINKS_NOVOS_ATE", datetime(2099, 1, 1, tzinfo=bd.TZ))]

    def correr(self, correio, funcao, **kw):
        caixa, patches = self.correio_falso(correio)
        kw = {"simular": False, "para": None, "limite": None, "forcar": False, "partilhados": False, "so": None, **kw}
        args = mock.Mock(**kw)
        for p in patches:
            p.start()
        try:
            funcao(args)
        finally:
            for p in patches:
                p.stop()
        return caixa

    def test_email_aos_candidatos_sem_repeticoes(self):
        import correio
        self.sincronizar_com_entidades()
        con = bd.ligar()
        con.execute("UPDATE submissoes SET cand_email = 'pessoa.' || id_submissao || '@exemplo.pt'")
        con.close()
        self.entrar(self.t1)
        self.gravar(self.t1, "a", "emp_lidl", "Validado", "10:20")
        self.gravar(self.t1, "a", "emp_steam", "Validado")                   # lista de espera
        con = bd.ligar()
        pessoas = {p["nome"]: p for p in correio.candidatos_a_contactar(con)}
        con.close()
        # a e c são a mesma pessoa (mesmo NIF): um só e-mail, com a LIDL marcada, a STEAM em espera e a
        # reinscrição na STEAM anulada como repetida; b e d continuam por validar e não recebem nada
        self.assertEqual(set(pessoas), {"Pessoa c"})
        p = pessoas["Pessoa c"]
        self.assertEqual(p["email"], "pessoa.c@exemplo.pt")                   # o da inscrição mais recente
        self.assertEqual([(i["empresa"], i["classe"]) for i in p["itens"]],
                         [("LIDL", "marcado"), ("STEAM", "espera"), ("STEAM", "anulado")])
        caixa = self.correr(correio, correio.cmd_candidatos)
        self.assertEqual(len(caixa), 1)
        msg = caixa[0]
        self.assertIn("a sua entrevista a 13 de Outubro", msg["Subject"])
        html = msg.get_body(("html",)).get_content()
        for trecho in ("13 Out, às 10:20", "terça-feira, 13 de Outubro", correio.ARTIGO_ENTREVISTAS,
                       "Chegue 15 minutos antes", "Lista de espera", "cid:redemprega@fe2026", "Rui Lopes"):
            self.assertIn(trecho, html)
        self.assertNotIn("Observações", html)                                 # as notas internas não vão
        # Sem alterações, não se repete, nem forçando o dia seguinte
        self.assertEqual(self.correr(correio, correio.cmd_candidatos), [])
        con = bd.ligar()
        con.execute("UPDATE envios SET em = '2020-01-01T00:00:00+00:00'")
        con.close()
        self.assertEqual(self.correr(correio, correio.cmd_candidatos), [])
        # Mudou a hora: volta a sair (noutro dia)
        self.gravar(self.t1, "a", "emp_lidl", "Validado", "10:40")
        self.assertEqual(len(self.correr(correio, correio.cmd_candidatos)), 1)
        # No mesmo dia, uma nova mudança espera pelo dia seguinte
        self.gravar(self.t1, "a", "emp_lidl", "Validado", "10:00")
        self.assertEqual(self.correr(correio, correio.cmd_candidatos), [])

    def test_estado_aos_tecnicos_so_com_alteracoes(self):
        import correio
        self.sincronizar_com_entidades()
        self.assertEqual(len(self.correr(correio, correio.cmd_estado)), 2)       # Rui e Bia
        con = bd.ligar()
        con.execute("UPDATE envios SET em = '2020-01-01T00:00:00+00:00'")
        con.close()
        self.assertEqual(self.correr(correio, correio.cmd_estado), [])          # nada mudou
        self.entrar(self.t1)
        self.gravar(self.t1, "b", "emp_lidl", "Validado", "10:00")               # muda um candidato do Rui
        caixa = self.correr(correio, correio.cmd_estado)
        self.assertEqual([m["To"].addresses[0].display_name for m in caixa], ["Rui Lopes"])
        self.assertIn("às 18:00", caixa[0].get_body(("plain",)).get_content())
        self.assertNotIn("13:00", caixa[0].get_body(("plain",)).get_content())

    def test_links_novos_para_tecnicos_sem_link(self):
        import correio
        self.sincronizar_com_entidades()                                         # o Rui já tem link; a Bia não
        caixa = self.correr(correio, correio.cmd_links_novos)
        self.assertEqual([m["To"].addresses[0].addr_spec for m in caixa], ["bia@outra.pt"])
        self.assertIn(f"{correio.BASE_URL}/?t=", caixa[0].get_body(("html",)).get_content())
        con = bd.ligar()
        self.assertTrue(all(t["tem_link"] for t in tokens.tecnicos_de_entidades(con)))
        con.close()
        self.assertEqual(self.correr(correio, correio.cmd_links_novos), [])     # não repete

    def test_links_novos_revoga_se_o_envio_falhar(self):
        import smtplib
        import correio
        self.sincronizar_com_entidades()
        caixa, patches = self.correio_falso(correio)
        for p in patches:
            p.start()
        try:
            with mock.patch.object(correio.Envio, "enviar", return_value=False):
                correio.cmd_links_novos(mock.Mock(simular=False, para=None, limite=None, forcar=False))
        finally:
            for p in patches:
                p.stop()
        con = bd.ligar()
        bia = [t for t in tokens.tecnicos_de_entidades(con) if t["tecnico_canon"] == "Bia"][0]
        con.close()
        self.assertFalse(bia["tem_link"])                                        # volta a tentar depois
        self.assertTrue(issubclass(smtplib.SMTPDataError, smtplib.SMTPResponseException))

    def test_lembrete_de_triagem(self):
        """8 Out 2026: só a quem ainda não tratou nenhuma candidatura; o link tem de ser o do próprio."""
        import correio
        base = correio.BASE_URL
        self.entrar(self.t2)
        self.gravar(self.t2, "d", "emp_lidl", "Validado", "")                   # o Técnico Dois já começou
        entrada = (f"Técnica Um\tum@x.pt\t{base}/?t={self.t1}\n"
                   f"Técnico Dois\tdois@x.pt\t{base}/?t={self.t2}\n"
                   f"Técnica Um\toutro@x.pt\t{base}/?t={self.t2}\n")            # link trocado: retido
        with mock.patch.object(sys, "stdin", io.StringIO(entrada)):
            caixa = self.correr(correio, correio.cmd_triagem)
        self.assertEqual([m["To"].addresses[0].addr_spec for m in caixa], ["um@x.pt"])
        texto = caixa[0].get_body(("plain",)).get_content()
        self.assertIn("encerram amanhã, 9 de Outubro, às 13:00", texto)
        self.assertIn("tem 3 candidaturas para LIDL, STEAM", texto)
        self.assertIn(f"?t={self.t1}", texto)
        with mock.patch.object(sys, "stdin", io.StringIO(entrada)):
            self.assertEqual(self.correr(correio, correio.cmd_triagem), [])        # não repete

    def test_email_corrigido_e_aviso_de_devolvidos(self):
        """8 Out 2026: um e-mail de candidato devolvido pode ser corrigido em entidades-aliases.json e reenviado
        só a essa pessoa (--so); se voltar a falhar, o técnico que inscreveu é avisado, uma só vez."""
        import correio
        self.sincronizar_com_entidades()
        con = bd.ligar()
        con.execute("UPDATE submissoes SET cand_email = 'pessoa.' || id_submissao || '@exemplo.pt'")
        con.close()
        self.entrar(self.t1)
        self.gravar(self.t1, "a", "emp_lidl", "Validado", "10:20")
        self.assertEqual(len(self.correr(correio, correio.cmd_candidatos)), 1)
        with mock.patch.object(correio, "correcoes_email", return_value={"pessoa.c@exemplo.pt": "pessoa.c@certo.pt"}):
            self.assertEqual(self.correr(correio, correio.cmd_candidatos, so=["outra@x.pt"], forcar=True), [])
            caixa = self.correr(correio, correio.cmd_candidatos, so=["pessoa.c@exemplo.pt"], forcar=True)
            self.assertEqual([m["To"].addresses[0].addr_spec for m in caixa], ["pessoa.c@certo.pt"])
            with mock.patch.object(sys, "stdin", io.StringIO("pessoa.c@certo.pt\npessoa.c@exemplo.pt\n")):
                caixa = self.correr(correio, correio.cmd_devolvidos)
            self.assertEqual(len(caixa), 1)
            self.assertIn("Rui Lopes", caixa[0]["To"].addresses[0].display_name)
            texto = caixa[0].get_body(("plain",)).get_content()
            for trecho in ("Pessoa c", "pessoa.c@certo.pt, pessoa.c@exemplo.pt", "ENTREVISTA a 13 Out, às 10:20"):
                self.assertIn(trecho, texto)
            with mock.patch.object(sys, "stdin", io.StringIO("pessoa.c@certo.pt\n")):
                self.assertEqual(self.correr(correio, correio.cmd_devolvidos), [])   # já avisado

    def test_tecto_diario_de_envios(self):
        import correio
        self.sincronizar_com_entidades()
        with mock.patch.object(correio, "LIMITE_DIA", 1):
            caixa = self.correr(correio, correio.cmd_estado)
        self.assertEqual(len(caixa), 1)                                          # o 2.º fica para depois
        caixa = self.correr(correio, correio.cmd_estado)
        self.assertEqual(len(caixa), 1)

    def test_email_links_retem_caixas_partilhadas(self):
        import correio
        base = correio.BASE_URL
        entrada = io.StringIO(
            f"Ana\tGab\tana@x.pt\t1\t2\t{base}/?t={'a' * 32}\n"
            f"Rui\tJ13\tgeral@j13.pt\t2\t1\t{base}/?t={'b' * 32}\n"
            f"Rita\tJ13\tgeral@j13.pt\t3\t1\t{base}/?t={'c' * 32}\n"
            f"Zé\tGab\tze@x.pt\t4\t1\thttps://outro.site/?t={'d' * 32}\n")
        saida = io.StringIO()
        args = mock.Mock(simular=True, para=None, limite=None, forcar=False, partilhados=False)
        with mock.patch.object(sys, "stdin", entrada), mock.patch.object(sys, "stdout", saida):
            correio.cmd_links(args)
        com_partilhados = io.StringIO()
        entrada.seek(0)
        with mock.patch.object(sys, "stdin", entrada), mock.patch.object(sys, "stdout", com_partilhados):
            correio.cmd_links(mock.Mock(simular=True, para=None, limite=None, forcar=False, partilhados=True))
        self.assertIn("enviaria a Rui <geral@j13.pt>", com_partilhados.getvalue())
        # Numa caixa partilhada, o envio ao Rui não conta como envio à Rita (7 Out 2026)
        con = bd.ligar()
        bd.garantir_schema(con)
        correio.registar_envio(con, "link", "geral@j13.pt", "geral@j13.pt|Rui", "x", None, "enviado")
        correio.registar_envio(con, "link", "ana@x.pt", "ana@x.pt", "x", None, "enviado")   # registo antigo
        con.close()
        outra = io.StringIO()
        entrada.seek(0)
        with mock.patch.object(sys, "stdin", entrada), mock.patch.object(sys, "stdout", outra):
            correio.cmd_links(mock.Mock(simular=True, para=None, limite=None, forcar=False, partilhados=True))
        self.assertIn("já enviado: linha 1: Ana", outra.getvalue())
        self.assertIn("já enviado: linha 2: Rui", outra.getvalue())
        self.assertIn("enviaria a Rita <geral@j13.pt>", outra.getvalue())
        txt = saida.getvalue()
        self.assertIn("enviaria a Ana <ana@x.pt>", txt)
        self.assertIn("RETIDO linha 2: Rui (J13): e-mail partilhado com Rita", txt)
        self.assertIn("RETIDO linha 4: Zé (Gab): link com formato inesperado", txt)
        self.assertNotIn("bbbb", txt)                                          # os links nunca aparecem
        msg = correio.compor("feira@exemplo.pt", "ana@x.pt", "Assunto", "link", nome="Ana",
                             l=correio.ler_links(io.StringIO(entrada.getvalue()))[0], validade="18/10",
                             ate=correio.ESTADO_ATE)
        self.assertIn(f"{base}/?t={'a' * 32}", msg.get_body(("html",)).get_content())
        self.assertNotIn("b" * 32, msg.as_string())


if __name__ == "__main__":
    unittest.main()
