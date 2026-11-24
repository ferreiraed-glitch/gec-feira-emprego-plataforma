#!/usr/bin/env python3
"""Testes da exportação Formbricks → SQLite, só com dados fictícios.

    python3 -m unittest test_exportar_candidaturas -v
"""
import os
import tempfile
import unittest

import exportar_candidaturas as ex
import r1_capacidade as r1

COMPANIES = {"LIDL": "emp_lidl", "STEAM": "emp_steam", "SCML Babysitting": "emp_scml_babysitting"}
OFFER_ELEMENT = {"ofertas_lidl": "LIDL", "ofertas_steam": "STEAM", "ofertas_scml_babysitting": "SCML Babysitting"}
NENHUMA = {ex.NENHUMA_FALLBACK}
N1, N3 = "204813751", "273608142"   # NIFs com dígito de controlo válido (inventados)


def resp(rid, created, master, nif="123456789", doc="12345678 9ZZ1", finished=True, **ofertas):
    data = {
        "entidade_nome": "Gabinete X", "entidade_tecnico": "Técnica Y",
        "cand_nome": f"Candidato {rid}", "cand_nif": nif, "cand_num_doc": doc,
        "cand_cv": [f"https://exemplo.invalid/{rid}.pdf"],
        "rgpd_1": "accepted", "rgpd_2": "accepted", "rgpd_3": "accepted",
        "empresas_master": master,
    }
    data.update(ofertas)
    return {"id": rid, "createdAt": created, "updatedAt": created, "finished": finished, "data": data}


class ExportarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        self.cap = dict(ex.CAPACITY)
        ex.CAPACITY.clear()
        ex.CAPACITY.update({"LIDL": 1, "STEAM": 10, "SCML Babysitting": 5})

    def tearDown(self):
        ex.CAPACITY.clear()
        ex.CAPACITY.update(self.cap)
        self.tmp.cleanup()

    def run_sync(self, responses):
        subs, cands, avisos = ex.transform(responses, list(COMPANIES), OFFER_ELEMENT, NENHUMA)
        ex.compute_flags(subs)
        con = ex.connect(self.db)
        ex.sync(con, subs, cands, COMPANIES, len(responses), avisos)
        self.addCleanup(con.close)
        return con

    def test_transform_rotulos_e_ofertas(self):
        responses = [
            resp("r1", "2026-09-21T15:00:00.000Z",
                 ["LIDL — Operador/a loja · INSCRIÇÕES ENCERRADAS", "STEAM — Assistente | Outro"],
                 ofertas_lidl=ex.NENHUMA_FALLBACK, ofertas_steam=["Assistente", "Outro"]),
            resp("r2", "2026-09-21T16:00:00.000Z", ["STEAM — x"], finished=False),
        ]
        # resposta antiga de uma empresa desmarcada depois no ecrã mestre
        responses[0]["data"]["ofertas_scml_babysitting"] = ["Babysitter"]
        subs, cands, avisos = ex.transform(responses, list(COMPANIES), OFFER_ELEMENT, NENHUMA)
        self.assertEqual([s["id_submissao"] for s in subs], ["r1"])  # parcial fica de fora
        self.assertEqual(avisos, [])
        by = {c["empresa"]: c for c in cands}
        self.assertEqual(sorted(by), ["LIDL", "STEAM"])
        self.assertEqual(by["LIDL"]["sem_oferta"], 1)
        self.assertEqual(by["LIDL"]["ofertas_seleccionadas"], "")
        self.assertEqual(by["STEAM"]["ofertas_seleccionadas"], "Assistente | Outro")
        self.assertEqual(subs[0]["criada_em_local"], "2026-09-21 16:00:00")  # WEST, UTC+1
        self.assertEqual(subs[0]["cand_num_doc_norm"], "123456789ZZ1")

    def test_resync_preserva_colunas_de_trabalho(self):
        r = [resp("r1", "2026-09-21T15:00:00.000Z", ["STEAM — x"], ofertas_steam=["A"])]
        con = self.run_sync(r)
        con.execute("UPDATE candidaturas SET validacao='Validado', id_tecnico='T1', horario_entrevista='09:20'")
        con.commit()
        con.close()
        r[0]["data"]["ofertas_steam"] = ["A", "B"]
        con = self.run_sync(r)
        row = con.execute("SELECT validacao, id_tecnico, horario_entrevista, ofertas_seleccionadas "
                          "FROM candidaturas").fetchone()
        self.assertEqual(row, ("Validado", "T1", "09:20", "A | B"))

    def test_removidas_ficam_marcadas_e_voltam(self):
        a = resp("r1", "2026-09-21T15:00:00.000Z", ["STEAM — x", "LIDL — y"], ofertas_steam=["A"])
        con = self.run_sync([a])
        con.close()
        a["data"]["empresas_master"] = ["STEAM — x"]
        con = self.run_sync([a])
        self.assertEqual(con.execute("SELECT empresa, removida FROM candidaturas ORDER BY empresa").fetchall(),
                         [("LIDL", 1), ("STEAM", 0)])
        self.assertEqual(con.execute("SELECT count(*) FROM v_candidaturas").fetchone()[0], 1)
        con.close()
        con = self.run_sync([])
        self.assertEqual(con.execute("SELECT removida FROM submissoes").fetchone()[0], 1)
        con.close()
        con = self.run_sync([a])
        self.assertEqual(con.execute("SELECT removida FROM submissoes").fetchone()[0], 0)

    def test_candidatura_manual_nao_e_removida(self):
        a = resp("r1", "2026-09-21T15:00:00.000Z", ["STEAM — x"], ofertas_steam=["A"])
        con = self.run_sync([a])
        con.execute("INSERT INTO candidaturas (id_submissao, empresa, ordem_empresa, criada_em, validacao, manual) "
                    "VALUES ('r1', 'LIDL', 0, '2026-10-16T10:00:00+00:00', 'Validado', 1)")
        con.commit()
        con.close()
        con = self.run_sync([a])
        self.assertEqual(con.execute("SELECT empresa, removida, validacao FROM candidaturas ORDER BY empresa").fetchall(),
                         [("LIDL", 0, "Validado"), ("STEAM", 0, "Por validar")])

    def test_duplicados_por_nif_ou_documento(self):
        con = self.run_sync([
            resp("r1", "2026-09-21T15:00:00.000Z", ["STEAM — x"], nif="204 813 751", doc="AB1111"),
            resp("r2", "2026-09-21T15:01:00.000Z", ["STEAM — x"], nif=N1, doc="BC2222"),
            resp("r3", "2026-09-21T15:02:00.000Z", ["STEAM — x"], nif="222", doc="bc-2222"),
            resp("r4", "2026-09-21T15:03:00.000Z", ["STEAM — x"], nif=N3, doc="CD3333"),
        ])
        got = dict(con.execute("SELECT id_submissao, duplicado_de FROM submissoes").fetchall())
        self.assertEqual(got, {"r1": "r2", "r2": "r1, r3", "r3": "r2", "r4": ""})

    def test_nif_e_documento_ficticios_nao_identificam(self):
        for nif in ("000000000", "123456789", "987654321", "00000000", "111111111", "204813752", ""):
            self.assertTrue(r1.nif_ficticio(ex.norm_nif(nif)), nif)
        self.assertFalse(r1.nif_ficticio(N1))
        for doc in ("000", "0000000", "XXXXXXXXXX", "0000000000", "123456789ZZ1", "12345678 9 ZZ1", "AAAA"):
            self.assertTrue(r1.doc_ficticio(ex.norm_doc(doc)), doc)
        self.assertFalse(r1.doc_ficticio(ex.norm_doc("15329870 4 ZX1")))
        con = self.run_sync([
            resp("r1", "2026-09-21T15:00:00.000Z", ["STEAM — x"], nif="000000000", doc="000"),
            resp("r2", "2026-09-21T15:01:00.000Z", ["STEAM — x"], nif="000000000", doc="XXXXXXXXXX"),
            resp("r3", "2026-09-21T15:02:00.000Z", ["STEAM — x"], nif="123456789", doc="000"),
        ])
        self.assertEqual(con.execute("SELECT count(*) FROM submissoes WHERE duplicado_de <> ''").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT count(*) FROM candidaturas WHERE validacao = 'Anulado'").fetchone()[0], 0)

    def test_reinscricao_ate_3_empresas_so_anula_a_repetida(self):
        rs = [
            resp("r1", "2026-09-21T15:00:00.000Z", ["STEAM — x", "LIDL — y"], nif=N1, doc="AB1111"),
            resp("r2", "2026-09-21T15:05:00.000Z", ["SCML Babysitting — z"], nif="000000000", doc="ab-1111"),
            resp("r3", "2026-09-21T15:06:00.000Z", ["STEAM — x"], nif=N1, doc="000"),
        ]
        con = self.run_sync(rs)
        q = "SELECT id_submissao, empresa, validacao, validacao_alterada_por, observacoes FROM candidaturas ORDER BY 1, 2"
        got = {(g[0], g[1]): g[2:] for g in con.execute(q)}
        self.assertEqual(got[("r2", "SCML Babysitting")][0], "Por validar")   # 3.ª empresa: fica
        self.assertEqual(got[("r3", "STEAM")][:2], ("Anulado", "Automático (duplicado)"))
        self.assertIn("repete STEAM, já pedida na inscrição r1", got[("r3", "STEAM")][2])
        # A coordenação reverte; a sincronização seguinte não volta a anular.
        con.execute("UPDATE candidaturas SET validacao = 'Por validar', validacao_alterada_por = 'Coord' "
                    "WHERE id_submissao = 'r3'")
        con.commit()
        con.close()
        con = self.run_sync(rs)
        self.assertEqual(con.execute("SELECT validacao FROM candidaturas WHERE id_submissao='r3'").fetchone()[0],
                         "Por validar")

    def test_anulacao_a_mao_conta_para_as_3_empresas(self):
        # Regra de princípio (1 Out 2026): a anulação liberta a vaga na empresa,
        # mas não abre uma 4.ª empresa ao candidato, nem permite repetir a anulada.
        rs = [
            resp("r1", "2026-09-21T15:00:00.000Z", ["STEAM — x", "LIDL — y"], nif=N1, doc="AB1111"),
            resp("r2", "2026-09-21T15:05:00.000Z", ["SCML Babysitting — z"], nif=N1, doc="AB1111"),
        ]
        con = self.run_sync(rs)
        con.execute("UPDATE candidaturas SET validacao = 'Anulado', validacao_alterada_por = 'Coord' "
                    "WHERE id_submissao = 'r1' AND empresa = 'LIDL'")
        con.commit()
        con.close()
        rs.append(resp("r3", "2026-09-21T15:09:00.000Z", ["LIDL — y"], nif=N1, doc="AB1111"))
        con = self.run_sync(rs)
        r3 = con.execute("SELECT validacao, observacoes FROM candidaturas WHERE id_submissao='r3'").fetchone()
        self.assertEqual(r3[0], "Anulado")
        self.assertIn("repete LIDL", r3[1])
        self.assertEqual(r1.count_selections(rs, "empresas_master", list(COMPANIES), r1.ler_estados(self.db)),
                         {"STEAM": 1, "SCML Babysitting": 1})

    def test_classificar_mais_de_3_empresas(self):
        ins = [{"id": "a", "criada_em": "1", "nif": N1, "doc": "", "empresas": ["A", "B"]},
               {"id": "b", "criada_em": "2", "nif": N1, "doc": "", "empresas": ["C", "D"]}]
        res = r1.classificar(ins)
        self.assertIsNone(res[("b", "C")])
        self.assertIn("já tem 3 empresas", res[("b", "D")])
        # uma anulada à mão continua a ser uma das 3 (só liberta a vaga na empresa)
        res = r1.classificar(ins, {("a", "A"): ("Anulado", "Coord")})
        self.assertEqual(res[("a", "A")], r1.ANULADA_A_MAO)
        self.assertIsNone(res[("b", "C")])
        self.assertIn("já tem 3 empresas", res[("b", "D")])
        # uma validada à mão ocupa sempre lugar, mesmo sendo a 4.ª
        res = r1.classificar(ins, {("b", "D"): ("Validado", "Coord")})
        self.assertEqual([res[k] for k in [("a", "A"), ("a", "B"), ("b", "C"), ("b", "D")]], [None] * 4)
        # anulada à mão quando já era a 4.ª não tira o lugar a nenhuma das outras
        ins.append({"id": "c", "criada_em": "3", "nif": N1, "doc": "", "empresas": ["E"]})
        res = r1.classificar(ins, {("b", "D"): ("Anulado", "Coord")})
        self.assertIsNone(res[("b", "C")])
        self.assertIn("já tem 3 empresas", res[("c", "E")])

    def test_empresa_que_desistiu_nao_conta_para_as_3(self):
        ins = [{"id": "a", "criada_em": "1", "nif": N1, "doc": "", "empresas": ["McDONALD'S", "A", "B"]},
               {"id": "b", "criada_em": "2", "nif": N1, "doc": "", "empresas": ["C"]}]
        res = r1.classificar(ins, {("a", "McDONALD'S"): ("Anulado", "Coordenação (desistência da empresa)")})
        self.assertEqual(res[("a", "McDONALD'S")], r1.ANULADA_A_MAO)
        self.assertIsNone(res[("b", "C")])                     # McDonald's não ocupa uma das 3
        ins.append({"id": "c", "criada_em": "3", "nif": N1, "doc": "", "empresas": ["D"]})
        self.assertIn("já tem 3 empresas", r1.classificar(ins)[("c", "D")])

    def test_duplicado_ja_validado_nao_e_mexido(self):
        rs = [resp("r1", "2026-09-21T15:00:00.000Z", ["STEAM — x"], nif=N1, doc="AB1111")]
        con = self.run_sync(rs)
        con.close()
        rs.append(resp("r2", "2026-09-21T15:05:00.000Z", ["STEAM — x"], nif=N1, doc="BC2222"))
        subs, cands, av = ex.transform(rs, list(COMPANIES), OFFER_ELEMENT, NENHUMA)
        ex.compute_flags(subs)
        con = ex.connect(self.db)
        self.addCleanup(con.close)
        # r2 entra e é validada antes de haver sincronização com o duplicado assinalado? Simula-se:
        ex.sync(con, subs[:1], cands[:1], COMPANIES, 1, av)
        con.execute("INSERT INTO submissoes (id_submissao, criada_em, criada_em_local, rgpd_aceite, n_empresas, "
                    f"sincronizada_em, cand_nif_norm) VALUES ('r2', '2026-09-21T15:05:00.000Z', 'x', 1, 1, 'x', '{N1}')")
        con.execute("INSERT INTO candidaturas (id_submissao, empresa, ordem_empresa, criada_em, validacao, "
                    "validacao_alterada_por) VALUES ('r2', 'STEAM', 0, 'x', 'Validado', 'T1')")
        con.commit()
        ex.sync(con, subs, cands, COMPANIES, 2, av)
        self.assertEqual(con.execute("SELECT validacao FROM candidaturas WHERE id_submissao='r2'").fetchone()[0],
                         "Validado")

    def test_r1_conta_como_a_bd(self):
        rs = [resp("r1", "2026-09-21T15:00:00.000Z", ["STEAM — x"], nif=N1, doc="AB1111"),
              resp("r2", "2026-09-21T15:05:00.000Z", ["STEAM — x"], nif=N1 + " ", doc="BC2222"),
              resp("r3", "2026-09-21T15:06:00.000Z", ["STEAM — x"], nif="000000000", doc="000"),
              resp("r4", "2026-09-21T15:07:00.000Z", ["STEAM — x"], nif="000000000", doc="000")]
        self.assertEqual(r1.count_selections(rs, "empresas_master", list(COMPANIES)), {"STEAM": 3})
        # anulada à mão no dashboard: liberta o lugar
        estados = {("r3", "STEAM"): ("Anulado", "Coord")}
        self.assertEqual(r1.count_selections(rs, "empresas_master", list(COMPANIES), estados), {"STEAM": 2})
        # e coincide com o que a BD deixa activo depois da sincronização
        con = self.run_sync(rs)
        con.execute("UPDATE candidaturas SET validacao = 'Anulado', validacao_alterada_por = 'Coord' "
                    "WHERE id_submissao = 'r3'")
        con.commit()
        self.assertEqual(r1.count_selections(rs, "empresas_master", list(COMPANIES), r1.ler_estados(self.db)),
                         {"STEAM": 2})
        self.assertEqual(con.execute("SELECT count(*) FROM candidaturas WHERE validacao <> 'Anulado'").fetchone()[0], 2)

    def test_capacidade_por_ordem_e_anulado_liberta(self):
        rs = [resp(f"r{i}", f"2026-09-21T15:0{i}:00.000Z", ["LIDL — y"], nif=str(i), doc=str(i))
              for i in range(1, 4)]
        con = self.run_sync(rs)
        q = "SELECT id_submissao, ordem_empresa, excede_capacidade FROM candidaturas ORDER BY id_submissao"
        self.assertEqual(con.execute(q).fetchall(), [("r1", 1, 0), ("r2", 2, 1), ("r3", 3, 1)])
        con.execute("UPDATE candidaturas SET validacao='Anulado' WHERE id_submissao='r1'")
        con.commit()
        con.close()
        con = self.run_sync(rs)
        self.assertEqual(con.execute(q).fetchall(), [("r1", 0, 0), ("r2", 1, 0), ("r3", 2, 1)])

    def test_capacidade_editada_nao_e_reposta(self):
        con = self.run_sync([])
        con.execute("UPDATE empresas SET capacidade_maxima=99, hora_inicio='09:00' WHERE empresa='LIDL'")
        con.commit()
        con.close()
        con = self.run_sync([])
        self.assertEqual(con.execute("SELECT capacidade_maxima, hora_inicio FROM empresas WHERE empresa='LIDL'")
                         .fetchone(), (99, "09:00"))

    def test_configuracao_de_empresas(self):
        cfg = {"hora_inicio": "10:00", "hora_fim": "13:00",
               "empresas": {"LIDL": {"n_entrevistadores": 1, "duracao_entrevista_min": 20, "capacidade_maxima": 12}}}
        subs, cands, avisos = ex.transform([], list(COMPANIES), OFFER_ELEMENT, NENHUMA)
        con = ex.connect(self.db)
        self.addCleanup(con.close)
        ex.sync(con, subs, cands, COMPANIES, 0, avisos, cfg)
        q = ("SELECT empresa, capacidade_maxima, hora_inicio, hora_fim, duracao_entrevista_min, "
             "n_entrevistadores FROM empresas ORDER BY empresa")
        rows = {r[0]: r[1:] for r in con.execute(q)}
        self.assertEqual(rows["LIDL"], (12, "10:00", "13:00", 20, 1))
        self.assertEqual(rows["STEAM"], (10, None, None, None, None))  # sem configuração: só a capacidade do R1
        cfg["empresas"]["LIDL"]["duracao_entrevista_min"] = 30
        ex.sync(con, subs, cands, COMPANIES, 0, avisos, cfg)
        self.assertEqual(con.execute(q).fetchall()[0][4], 30)

    def test_ficheiro_de_configuracao_real(self):
        """empresas-config.json cobre as 14 empresas, coincide com o R1 e as
        contas batem com a tabela enviada: entrevistas possíveis das 10:00 às
        13:00 (+30%, arredondado) = capacidade. O início passou para as 09:30
        a 30 Set 2026 sem mexer nas capacidades."""
        mapping = ex.read_json(ex.MAPPING_FILE)
        companies = list(mapping["company_choice_ids"])
        ex.CAPACITY.clear()
        ex.CAPACITY.update(self.cap)  # a capacidade real do R1, não a dos testes
        config, avisos = ex.load_config(ex.CONFIG_FILE, companies)
        self.assertEqual(avisos, [])
        self.assertEqual((config["hora_inicio"], config["hora_fim"]), ("09:30", "13:00"))
        reduzidas = {"LIDL": 2,               # 6 Out 2026: menos 2 vagas, a pedido da coordenação
                     "EL CORTE INGLÉS": -4}   # 8 Out 2026: mais 4 vagas, a pedido dos RH
        for company, c in config["empresas"].items():
            entrevistas = c["n_entrevistadores"] * 180 // c["duracao_entrevista_min"]
            self.assertEqual(round(entrevistas * 1.3) - reduzidas.get(company, 0), c["capacidade_maxima"], company)
            # Atribuição automática: todas as empresas têm técnico, menos as que desistiram.
            self.assertEqual(bool(c.get("tecnicos")), company not in ex.DESISTENCIAS, company)

    def test_tolerancia_e_estado_do_formulario(self):
        """29 Set 2026: horários de tolerância (global, com excepção por
        empresa) e, lidos do formulário, as ofertas e se a empresa lá está."""
        mapping = {"master_element_id": "empresas_master", "company_choice_ids": COMPANIES,
                   "company_blocks": {"LIDL": "b_lidl", "STEAM": "b_steam", "SCML Babysitting": "b_scml"}}
        lab = lambda t: {"pt-PT": t}  # noqa: E731
        survey = {"blocks": [
            {"id": "b_master", "elements": [{"id": "empresas_master", "choices": [
                {"id": "emp_lidl", "label": lab("LIDL — Operador")},
                {"id": "emp_scml_babysitting", "label": lab("SCML Babysitting — Babysitter")}]}]},
            {"id": "b_lidl", "elements": [{"id": "ofertas_lidl", "choices": [
                {"id": "o1", "label": lab("Operador de loja")}, {"id": "lidl_nenhuma", "label": lab("Não me candidato")}]}]},
            {"id": "b_steam", "elements": [{"id": "ofertas_steam", "choices": [
                {"id": "o2", "label": lab("Monitor")}, {"id": "o3", "label": lab("Coordenador")}]}]},
        ]}
        form = ex.estado_formulario(survey, mapping)
        self.assertEqual(form["LIDL"], {"ofertas": "Operador de loja", "no_formulario": 1})
        self.assertEqual(form["STEAM"], {"ofertas": "Monitor | Coordenador", "no_formulario": 0})
        self.assertEqual(form["SCML Babysitting"]["ofertas"], None)

        cfg = {"hora_inicio": "10:00", "hora_fim": "13:00", "slots_tolerancia": 2, "empresas": {
            "LIDL": {"duracao_entrevista_min": 20}, "STEAM": {"duracao_entrevista_min": 15, "slots_tolerancia": 0}}}
        subs, cands, avisos = ex.transform([], list(COMPANIES), OFFER_ELEMENT, NENHUMA)
        con = ex.connect(self.db)
        self.addCleanup(con.close)
        ex.sync(con, subs, cands, COMPANIES, 0, avisos, cfg, form)
        q = "SELECT empresa, slots_tolerancia, ofertas, no_formulario, desistiu FROM empresas"
        rows = {r[0]: r[1:] for r in con.execute(q)}
        self.assertEqual(rows["LIDL"], (2, "Operador de loja", 1, 0))
        self.assertEqual(rows["STEAM"], (0, "Monitor | Coordenador", 0, 0))
        self.assertEqual(rows["SCML Babysitting"], (0, None, 1, 0))   # sem configuração: sem tolerância
        # Sincronização sem leitura do formulário (ex. testes) não apaga o que lá está
        ex.sync(con, subs, cands, COMPANIES, 0, avisos, cfg)
        self.assertEqual({r[0]: r[1:] for r in con.execute(q)}["STEAM"], (0, "Monitor | Coordenador", 0, 0))

    def tokens(self, con, *ids, revogado=0, expira="2099-01-01T00:00:00+00:00"):
        con.execute("CREATE TABLE IF NOT EXISTS tokens (token_hash TEXT PRIMARY KEY, prefixo TEXT, perfil TEXT, "
                    "nome TEXT, id_tecnico TEXT, empresa TEXT, criado_em TEXT, expira_em TEXT, "
                    "revogado INTEGER NOT NULL DEFAULT 0, ultimo_acesso TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS acessos (id INTEGER PRIMARY KEY AUTOINCREMENT, em TEXT NOT NULL, "
                    "prefixo TEXT, perfil TEXT, nome TEXT, accao TEXT NOT NULL, id_submissao TEXT, empresa TEXT, "
                    "detalhe TEXT, ip TEXT)")
        for t in ids:
            con.execute("INSERT INTO tokens VALUES (?, 'x', 'tecnico', ?, ?, NULL, '', ?, ?, NULL)",
                        (f"h{t}{revogado}", f"Nome {t}", t, expira, revogado))
        con.commit()

    def sync_cfg(self, con, responses, cfg):
        subs, cands, avisos = ex.transform(responses, list(COMPANIES), OFFER_ELEMENT, NENHUMA)
        ex.compute_flags(subs)
        return ex.sync(con, subs, cands, COMPANIES, len(responses), avisos, cfg)

    def test_atribuicao_automatica_de_tecnicos(self):
        """2 Out 2026: a candidatura que chega sem técnico vai para o técnico
        da empresa; com dois, para o que tem menos; nunca muda uma atribuição
        feita, nem atribui a quem não tem link activo."""
        con = ex.connect(self.db)
        self.addCleanup(con.close)
        self.tokens(con, "T1", "T2", "T3")
        self.tokens(con, "TR", revogado=1)
        cfg = {"empresas": {"STEAM": {"tecnicos": ["T1", "T2"]}, "LIDL": {"tecnicos": ["TR"]},
                            "SCML Babysitting": {}}}
        n = [N1, N3, "501442600", "502011378"]
        r = [resp(f"r{i}", f"2026-10-0{i + 1}T10:00:00.000Z", ["STEAM — x"], nif=n[i], doc=f"DOC{i}XYZ",
                  ofertas_steam=["A"]) for i in range(3)]
        self.sync_cfg(con, r, cfg)
        q = "SELECT id_submissao, id_tecnico FROM candidaturas WHERE empresa = 'STEAM' ORDER BY id_submissao"
        self.assertEqual(con.execute(q).fetchall(), [("r0", "T1"), ("r1", "T2"), ("r2", "T1")])
        # A coordenação passa uma de T1 para T3: T1 e T2 ficam com 1 cada e,
        # no empate, a seguinte vai para o primeiro da lista.
        con.execute("UPDATE candidaturas SET id_tecnico = 'T3' WHERE id_submissao = 'r0'")
        con.commit()
        r.append(resp("r3", "2026-10-05T10:00:00.000Z", ["STEAM — x", "LIDL — y", "SCML Babysitting — z"],
                      nif=n[3], doc="DOC3XYZ", ofertas_steam=["A"]))
        _, _, _, _, atribuidas = self.sync_cfg(con, r, cfg)
        self.assertEqual(atribuidas, {"STEAM": {"T1": 1}})
        self.assertEqual(con.execute(q).fetchall(),
                         [("r0", "T3"), ("r1", "T2"), ("r2", "T1"), ("r3", "T1")])
        # LIDL: técnico com link revogado; SCML Babysitting: sem técnico configurado.
        self.assertEqual(con.execute("SELECT empresa, id_tecnico FROM candidaturas WHERE empresa <> 'STEAM' "
                                     "ORDER BY empresa").fetchall(), [("LIDL", None), ("SCML Babysitting", None)])
        aviso = con.execute("SELECT avisos FROM sincronizacoes ORDER BY id DESC").fetchone()[0]
        self.assertIn("LIDL: técnicos sem link activo", aviso)
        self.assertIn("1 candidaturas atribuídas automaticamente", aviso)
        self.assertEqual(con.execute("SELECT nome, empresa, detalhe FROM acessos ORDER BY id DESC").fetchone(),
                         (ex.ATRIBUIDO_AUTO, "STEAM", "1 candidaturas → T1"))
        # Uma 2.ª sincronização não muda nada.
        self.assertEqual(self.sync_cfg(con, r, cfg)[4], {})

    def test_atribuicao_sem_tokens_ou_com_link_expirado(self):
        con = ex.connect(self.db)
        self.addCleanup(con.close)
        cfg = {"empresas": {"STEAM": {"tecnicos": ["T1"]}}}
        r = [resp("r0", "2026-10-01T10:00:00.000Z", ["STEAM — x"], nif=N1, doc="DOC0XYZ", ofertas_steam=["A"])]
        self.assertEqual(self.sync_cfg(con, r, cfg)[4], {})   # BD sem dashboard: não atribui
        self.tokens(con, "T1", expira="2026-01-01T00:00:00+00:00")
        self.assertEqual(self.sync_cfg(con, r, cfg)[4], {})
        self.assertIsNone(con.execute("SELECT id_tecnico FROM candidaturas").fetchone()[0])

    def test_tecnicos_invalidos_na_configuracao(self):
        path = os.path.join(self.tmp.name, "c.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"empresas": {"STEAM": {"tecnicos": "T1"}, "LIDL": {}, "SCML Babysitting": {}}}')
        config, avisos = ex.load_config(path, list(COMPANIES))
        self.assertIn('STEAM: "tecnicos" tem de ser uma lista de id_tecnico', avisos)
        self.assertEqual(config["empresas"]["STEAM"]["tecnicos"], [])

    def test_csv(self):
        con = self.run_sync([resp("r1", "2026-09-21T15:00:00.000Z", ["STEAM — x"], ofertas_steam=["A"])])
        out = os.path.join(self.tmp.name, "o.csv")
        self.assertEqual(ex.export_csv(con, out), 1)
        with open(out, encoding="utf-8-sig") as f:
            self.assertTrue(f.readline().startswith("id_submissao;timestamp;"))


if __name__ == "__main__":
    unittest.main()
