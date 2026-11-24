"""Notas automáticas das inscrições repetidas nas observações (7 Out 2026).

Quando uma candidatura anulada como repetida volta a «Por validar» ou é
validada, a nota que explicava a anulação deixa de fazer sentido e só
confunde quem lê as observações. Tira-se essa nota e mantém-se tudo o que as
pessoas escreveram, antes ou depois dela.

Usado pela sincronização (exportar_candidaturas.limpar_notas_automaticas,
para o que já está na BD) e pela gravação no dashboard (app.gravar).
"""
import re

# As três notas escritas por anular_duplicados(): a de 23 Set (R3 original),
# a de 1 Out (regra das 3 empresas) e a da revisão automática.
NOTAS_AUTOMATICAS = re.compile(
    r"Duplicado de \S+: anulada automaticamente \(mesmo NIF ou documento\)\.?"
    r"|Inscrição repetida: [^\n]*?\. Anulada automaticamente\."
    r"|Anulação automática revista: o candidato fica dentro das 3 empresas, sem repetir nenhuma\.")


def sem_notas_automaticas(texto):
    """O texto sem as notas automáticas, sem linhas vazias que elas deixem;
    None se não sobrar nada."""
    if not texto or not NOTAS_AUTOMATICAS.search(texto):
        return texto
    linhas = [re.sub(r"[ \t]{2,}", " ", NOTAS_AUTOMATICAS.sub("", l)).strip() for l in re.split(r"\r?\n", texto)]
    return "\n".join(l for l in linhas if l) or None
