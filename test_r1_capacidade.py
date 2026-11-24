"""Testes do fecho/reabertura de empresas no R1 (só dados fictícios).

    python3 -m unittest test_r1_capacidade
"""
import copy
import itertools
import json
import unittest

import r1_capacidade as r1

EMPRESAS = ["A", "B", "C", "D"]
CHOICE = {e: f"emp_{e.lower()}" for e in EMPRESAS}
BLOCO = {e: f"blk_{e.lower()}" for e in EMPRESAS}
FIM = "blk_rgpd"


def regra(empresa):
    return {
        "id": f"r_{empresa}",
        "actions": [{"id": f"a_{empresa}", "target": BLOCO[empresa], "objective": "jumpToBlock"}],
        "conditions": {"id": "c", "connector": "and", "conditions": [{
            "id": "k", "operator": "includesOneOf",
            "leftOperand": {"type": "element", "value": "mestre"},
            "rightOperand": {"type": "static", "value": [CHOICE[empresa]]},
        }]},
    }


def formulario():
    """Mestre + um bloco por empresa, cada um a saltar para a próxima escolhida."""
    mestre = {
        "id": "blk_mestre",
        "elements": [{"id": "mestre", "choices": [
            {"id": CHOICE[e], "label": {"pt-PT": f"{e} — oferta"}} for e in EMPRESAS
        ]}],
        "logic": [regra(e) for e in EMPRESAS],
        "logicFallback": FIM,
    }
    blocos = [mestre]
    for i, e in enumerate(EMPRESAS):
        blocos.append({"id": BLOCO[e], "elements": [], "logic": [regra(x) for x in EMPRESAS[i + 1:]],
                       "logicFallback": FIM})
    blocos.append({"id": FIM, "elements": []})
    return blocos


def percurso(blocos, escolhidas):
    by_id = {b["id"]: b for b in blocos}
    sel = {CHOICE[e] for e in escolhidas}
    atual, vistos = "blk_mestre", []
    while atual != FIM:
        bloco = by_id[atual]
        vistos.append(atual)
        atual = next((reg["actions"][0]["target"] for reg in bloco.get("logic", [])
                      if sel & set(reg["conditions"]["conditions"][0]["rightOperand"]["value"])),
                     bloco["logicFallback"])
    inv = {v: k for k, v in BLOCO.items()}
    return [inv[b] for b in vistos if b in inv]


def mestre(blocos):
    return r1.find_master_element(blocos, "blk_mestre", "mestre")


class FechoReabertura(unittest.TestCase):
    def test_fecho_retira_opcao_e_regras(self):
        blocos = formulario()
        reg = r1.close_choice(blocos, mestre(blocos), CHOICE["B"])
        self.assertNotIn(CHOICE["B"], json.dumps(blocos))
        self.assertEqual(len(reg["rules"]), 2)  # regra no mestre e no bloco de A
        restantes = [e for e in EMPRESAS if e != "B"]
        for k in (1, 2, 3):
            for comb in itertools.combinations(restantes, k):
                self.assertEqual(percurso(blocos, comb), list(comb))

    def test_fecho_tira_sufixo_antigo(self):
        blocos = formulario()
        mestre(blocos)["choices"][2]["label"]["pt-PT"] += r1.CLOSED_SUFFIX
        reg = r1.close_choice(blocos, mestre(blocos), CHOICE["C"])
        self.assertEqual(reg["choice"]["label"]["pt-PT"], "C — oferta")

    def test_reabrir_repoe_original_em_qualquer_ordem(self):
        original = formulario()
        ordem = [CHOICE[e] for e in EMPRESAS]
        for fechadas in itertools.permutations(EMPRESAS, 3):
            for reabertura in itertools.permutations(fechadas):
                blocos = copy.deepcopy(original)
                regs = {e: r1.close_choice(blocos, mestre(blocos), CHOICE[e]) for e in fechadas}
                for e in reabertura:
                    # como vem do ficheiro JSON
                    r1.reopen_choice(blocos, mestre(blocos), json.loads(json.dumps(regs[e])), ordem)
                self.assertEqual(blocos, original, (fechadas, reabertura))


if __name__ == "__main__":
    unittest.main()
