#!/usr/bin/env python3
"""Testes da unificação de entidades e técnicos (entidades.py), só com dados fictícios.

    python3 -m unittest test_entidades -v
"""
import os
import unittest

import entidades as en

ALIASES = en.carregar_aliases()


def sub(sid, minuto, entidade, tecnico, email, tel):
    return {"id_submissao": sid, "criada_em": f"2026-09-21T10:{minuto:02d}:00+00:00", "entidade_nome": entidade,
            "entidade_tecnico": tecnico, "entidade_email": email, "entidade_telefone": tel}


class UnificarTest(unittest.TestCase):
    def unificar(self, subs):
        en.unificar(subs, ALIASES)
        return {s["id_submissao"]: s for s in subs}

    def test_mesmo_tecnico_com_grafias_diferentes(self):
        """«Djamila» e «Djamila Silva» com o mesmo e-mail e telefone; telefone com espaços e indicativo."""
        r = self.unificar([
            sub("a", 1, "Assoc. Exemplo", "Joana", "joana@exemplo.pt", "910000001"),
            sub("b", 2, "Assoc. Exemplo", "Joana Lima", "Joana@Exemplo.pt ", "+351 910 000 001"),
            sub("c", 3, "Assoc. Exemplo", "Joana Lima", "joana@exemplo.pt", "910000001"),
        ])
        self.assertEqual({x["tecnico_id"] for x in r.values()}, {"a"})
        self.assertEqual(r["a"]["tecnico_canon"], "Joana Lima")
        self.assertEqual(r["a"]["tecnico_email_canon"], "joana@exemplo.pt")
        self.assertEqual(r["a"]["tecnico_tel_canon"], "910 000 001")

    def test_email_diferente_mesmo_telefone(self):
        r = self.unificar([
            sub("a", 1, "Junta X", "Rui Costa", "rui@junta-x.pt", "960000002"),
            sub("b", 2, "Junta X", "Rui Costa", "ruicosta@gmail.com", "960000002"),
        ])
        self.assertEqual(r["a"]["tecnico_id"], r["b"]["tecnico_id"])

    def test_email_partilhado_por_dois_tecnicos(self):
        """Um e-mail de serviço usado por duas pessoas: a entidade junta-se, os técnicos não."""
        r = self.unificar([
            sub("a", 1, "Associação Y - Núcleo Z", "Marta Reis", "geral@assoc-y.pt", "910000003"),
            sub("b", 2, "Associação Y", "Paulo Neves", "geral@assoc-y.pt", "910000003"),
        ])
        self.assertNotEqual(r["a"]["tecnico_id"], r["b"]["tecnico_id"])
        self.assertEqual(r["a"]["entidade_canon"], r["b"]["entidade_canon"])

    def test_entidade_com_nomes_diferentes_e_mesmo_tecnico(self):
        """«Associação de moradores» e «… per11»; sigla e nome por extenso."""
        r = self.unificar([
            sub("a", 1, "Associação de moradores", "Ivo Sá", "ivo@gmail.com", "920000004"),
            sub("b", 2, "Associação de moradores per11", "Ivo Sá", "ivo@gmail.com", "920000004"),
            sub("c", 3, "Associação de moradores per11", "Ivo Sá", "ivo@gmail.com", "920000004"),
        ])
        self.assertEqual({x["entidade_canon"] for x in r.values()}, {"Associação de moradores per11"})

    def test_alias_scml_e_dominio(self):
        """SCML por extenso, sigla, unidade e domínio de e-mail: uma só entidade, com o nome do ficheiro de aliases."""
        r = self.unificar([
            sub("a", 1, "SCML", "Ana Um", "ana.um@scml.pt", "930000001"),
            sub("b", 2, "Santa Casa da Misericórdia de Lisboa", "Bia Dois", "bia@hotmail.com", "930000002"),
            sub("c", 3, "SANTA CASA DA MISERICÓRDIA DE LISBOA (SCML)", "Caio Três", "caio@outro.pt", "930000003"),
            sub("d", 4, "Centro Social Qualquer", "Duda Quatro", "duda@scml.pt", "930000004"),
        ])
        self.assertEqual({x["entidade_canon"] for x in r.values()}, {"Santa Casa da Misericórdia de Lisboa (SCML)"})

    def test_gip_repetido(self):
        r = self.unificar([
            sub("a", 1, "GIP ANEME", "Lia Paz", "gip@aneme.pt", "960000005"),
            sub("b", 2, "GIP GIP ANEME", "Lia Paz", "gip@aneme.pt", "960000005"),
        ])
        self.assertEqual({x["entidade_canon"] for x in r.values()}, {"GIP ANEME"})

    def test_dominio_generico_nao_junta(self):
        r = self.unificar([
            sub("a", 1, "Entidade A", "Rita Um", "rita@gmail.com", "910000011"),
            sub("b", 2, "Entidade B", "Sara Dois", "sara@gmail.com", "910000012"),
        ])
        self.assertNotEqual(r["a"]["entidade_canon"], r["b"]["entidade_canon"])

    def test_empate_prefere_maiusculas_iniciais(self):
        r = self.unificar([
            sub("a", 1, "Ent", "rita castro", "rc@ent.pt", ""),
            sub("b", 2, "Ent", "Rita Castro", "rc@ent.pt", ""),
        ])
        self.assertEqual(r["a"]["tecnico_canon"], "Rita Castro")

    def test_tecnico_id_e_a_submissao_mais_antiga(self):
        """O id do técnico não depende da ordem de leitura: é a submissão mais antiga."""
        subs = [sub("z", 9, "E", "Tó", "to@e.pt", ""), sub("y", 1, "E", "Tó", "to@e.pt", "")]
        r = self.unificar(subs)
        self.assertEqual(r["z"]["tecnico_id"], "y")

    def test_campos_vazios(self):
        r = self.unificar([{"id_submissao": "a", "criada_em": "2026-09-21T10:00:00+00:00"}])
        self.assertIsNone(r["a"]["entidade_canon"])
        self.assertEqual(r["a"]["tecnico_id"], "a")

    def test_correcao_de_email(self):
        """Um e-mail com gralha corrige-se no ficheiro de aliases e junta-se ao certo (8 Out 2026)."""
        subs = [sub("a", 1, "APEA", "Inês", "ines.chavess@x.org", ""), sub("b", 2, "APEA", "Inês", "ines.chaves@x.org", "")]
        en.unificar(subs, ALIASES, {"ines.chavess@x.org": "ines.chaves@x.org"})
        r = {s["id_submissao"]: s for s in subs}
        self.assertEqual(r["a"]["tecnico_email_canon"], "ines.chaves@x.org")
        self.assertEqual(r["a"]["tecnico_id"], r["b"]["tecnico_id"])
        self.assertEqual(r["a"]["entidade_email"], "ines.chavess@x.org")       # o original fica
        self.assertIn("tecnica.errada@exemplo.org", en.carregar_correcoes_email())

    def test_ficheiro_de_aliases_valido(self):
        self.assertTrue(os.path.exists(en.ALIASES_FILE))
        self.assertGreater(len(ALIASES), 0)
        for nome, padroes in ALIASES:
            self.assertTrue(any(p.search(en.chave_nome(nome)) for p in padroes), nome)


if __name__ == "__main__":
    unittest.main()
