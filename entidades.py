"""Unificação das entidades e dos técnicos que inscrevem candidatos (6 Out 2026).

No formulário, o nome da entidade e do técnico são texto livre, e a mesma
entidade aparece escrita de várias maneiras («SCML», «Santa Casa Misericórdia
Lisboa», «GIP GIP ANEME»), tal como o mesmo técnico («Djamila» e «Djamila
Silva»). O e-mail e o telefone do técnico variam muito menos, e servem de
critério principal.

Técnico: as submissões com o mesmo e-mail ou o mesmo telefone são o mesmo
contacto (transitivamente). Dentro de um contacto separam-se as pessoas pelo
primeiro nome, porque há e-mails de serviço partilhados por dois técnicos.
O nome, o e-mail e o telefone mostrados são os mais usados no grupo.

Entidade: ficam juntas as submissões
  - do mesmo contacto de técnico;
  - com o mesmo nome normalizado (sem acentos, maiúsculas, pontuação nem
    «de/da/do…»);
  - com o mesmo domínio de e-mail institucional (não gmail, hotmail…);
  - que coincidem com o mesmo padrão de entidades-aliases.json.
O nome mostrado é o do ficheiro de aliases, se o grupo coincidir com um
padrão; senão, o nome mais usado no grupo.

Os campos originais não mudam: as colunas novas ficam ao lado. Um e-mail
escrito com erro no formulário corrige-se em "emails" no ficheiro de aliases
(8 Out 2026): vale para o agrupamento e para o e-mail mostrado e usado nos
envios.
"""
import json
import os
import re
import unicodedata
from collections import Counter

ALIASES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "entidades-aliases.json")

DOMINIOS_GENERICOS = {
    "gmail.com", "gmail.pt", "hotmail.com", "hotmail.pt", "outlook.com", "outlook.pt", "live.com", "live.com.pt",
    "msn.com", "yahoo.com", "yahoo.com.br", "yahoo.pt", "sapo.pt", "icloud.com", "me.com", "mail.com",
    "protonmail.com", "proton.me", "netcabo.pt", "clix.pt", "iol.pt", "aeiou.pt",
}
PALAVRAS_VAZIAS = {"de", "da", "do", "dos", "das", "e", "a", "o"}

# Colunas acrescentadas a cada submissão.
CAMPOS = ("entidade_canon", "tecnico_id", "tecnico_canon", "tecnico_email_canon", "tecnico_tel_canon")


def sem_acentos(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def chave_nome(s):
    """'Santa Casa da Misericórdia de Lisboa' → 'santa casa misericordia lisboa'."""
    palavras = re.sub(r"[^a-z0-9]+", " ", sem_acentos(s)).split()
    return " ".join(p for p in palavras if p not in PALAVRAS_VAZIAS)


def norm_email(s):
    s = (s or "").strip().lower()
    return s if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s) else ""


def norm_tel(s):
    d = re.sub(r"\D", "", s or "")
    if d.startswith("00351"):
        d = d[5:]
    elif d.startswith("351") and len(d) == 12:
        d = d[3:]
    return d if len(d) >= 6 else ""


def formatar_tel(d):
    return f"{d[:3]} {d[3:6]} {d[6:]}" if len(d) == 9 else d


def primeiro_nome(s):
    m = re.search(r"[a-z]+", sem_acentos(s))
    return m.group(0) if m else ""


def dominio_institucional(email):
    dom = email.rsplit("@", 1)[1] if "@" in email else ""
    return dom if dom and dom not in DOMINIOS_GENERICOS else ""


def limpar(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def carregar_aliases(path=ALIASES_FILE):
    """[(nome a mostrar, [regex compilada, …]), …]; os padrões aplicam-se a chave_nome()."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        dados = json.load(f)
    return [(a["nome"], [re.compile(p) for p in a["padroes"]]) for a in dados.get("entidades", [])]


def carregar_correcoes_email(path=ALIASES_FILE):
    """{e-mail errado: e-mail certo}, já normalizados."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        dados = json.load(f)
    return {norm_email(k): norm_email(v) for k, v in dados.get("emails", {}).items() if norm_email(v)}


class _Conjuntos:
    def __init__(self):
        self.pai = {}

    def raiz(self, x):
        self.pai.setdefault(x, x)
        while self.pai[x] != x:
            self.pai[x] = self.pai[self.pai[x]]
            x = self.pai[x]
        return x

    def juntar(self, a, b):
        ra, rb = self.raiz(a), self.raiz(b)
        if ra != rb:
            self.pai[max(ra, rb)] = min(ra, rb)


def _mais_usado(valores):
    """O valor mais frequente; em empate, o que não está todo em maiúsculas
    ou minúsculas, depois o que tem mais palavras com maiúscula inicial
    («Rita Castro» e não «Rita castro»), depois o mais comprido, depois o
    que apareceu primeiro."""
    valores = [v for v in valores if v]
    if not valores:
        return ""
    n = Counter(valores)
    primeiro = {}
    for i, v in enumerate(valores):
        primeiro.setdefault(v, i)
    return max(n, key=lambda v: (n[v], not (v.isupper() or v.islower()), sum(p[:1].isupper() for p in v.split()),
                                 len(v), -primeiro[v]))


def unificar(subs, aliases=(), correcoes_email=None):
    """Acrescenta a cada submissão (dict com id_submissao, criada_em e os
    campos entidade_*) as colunas de CAMPOS. tecnico_id é o id da submissão
    mais antiga do técnico: não muda enquanto as submissões existirem, e serve
    de âmbito aos links das entidades."""
    ordem = sorted(subs, key=lambda s: (s.get("criada_em") or "", s["id_submissao"]))
    corr = carregar_correcoes_email() if correcoes_email is None else correcoes_email

    def email_de(s):
        e = norm_email(s.get("entidade_email"))
        return corr.get(e, e)

    # Contactos: mesmo e-mail ou mesmo telefone.
    contactos = _Conjuntos()
    for s in ordem:
        sid = "s:" + s["id_submissao"]
        contactos.raiz(sid)
        email, tel = email_de(s), norm_tel(s.get("entidade_telefone"))
        if email:
            contactos.juntar(sid, "e:" + email)
        if tel:
            contactos.juntar(sid, "t:" + tel)

    # Técnicos: contacto + primeiro nome. Um nome vazio junta-se à única pessoa do contacto, se só houver uma.
    nomes_do_contacto = {}
    for s in ordem:
        nomes_do_contacto.setdefault(contactos.raiz("s:" + s["id_submissao"]), set()).add(
            primeiro_nome(s.get("entidade_tecnico")))
    tecnicos = {}
    for s in ordem:
        c = contactos.raiz("s:" + s["id_submissao"])
        nome = primeiro_nome(s.get("entidade_tecnico"))
        outros = nomes_do_contacto[c] - {""}
        if not nome and len(outros) == 1:
            nome = next(iter(outros))
        tecnicos.setdefault((c, nome), []).append(s)

    # Entidades.
    entidades = _Conjuntos()
    alias_de = {}
    for s in ordem:
        sid = "s:" + s["id_submissao"]
        entidades.raiz(sid)
        entidades.juntar(sid, "c:" + contactos.raiz(sid))
        chave = chave_nome(s.get("entidade_nome"))
        if chave:
            entidades.juntar(sid, "n:" + chave)
            for nome, padroes in aliases:
                if any(p.search(chave) for p in padroes):
                    entidades.juntar(sid, "a:" + nome)
                    alias_de[s["id_submissao"]] = nome
                    break
        dom = dominio_institucional(email_de(s))
        if dom:
            entidades.juntar(sid, "d:" + dom)
    grupos = {}
    for s in ordem:
        grupos.setdefault(entidades.raiz("s:" + s["id_submissao"]), []).append(s)
    for membros in grupos.values():
        nomes_alias = [alias_de[s["id_submissao"]] for s in membros if s["id_submissao"] in alias_de]
        nome = _mais_usado(nomes_alias) or _mais_usado([limpar(s.get("entidade_nome")) for s in membros])
        for s in membros:
            s["entidade_canon"] = nome or None

    for membros in tecnicos.values():
        nome = _mais_usado([limpar(s.get("entidade_tecnico")) for s in membros])
        email = _mais_usado([email_de(s) for s in membros])
        tel = _mais_usado([norm_tel(s.get("entidade_telefone")) for s in membros])
        for s in membros:
            s["tecnico_id"] = membros[0]["id_submissao"]
            s["tecnico_canon"] = nome or None
            s["tecnico_email_canon"] = email or None
            s["tecnico_tel_canon"] = formatar_tel(tel) or None
    return subs
