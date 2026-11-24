"""CVs: descarregados do armazenamento privado do Formbricks (exige a chave
API, sem ela dá 401) e, para as empresas, com marca de água (§5.1.1).

O link directo do Formbricks nunca chega ao browser: o dashboard serve o
ficheiro depois de verificar o acesso e de o registar.
"""
import io
import urllib.parse
import urllib.request
from datetime import datetime

from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import Color
from reportlab.pdfgen import canvas

PUBLICO = "https://formularios.exemplo.org"


class CVIndisponivel(Exception):
    pass


def descarregar(cv_url, base_url, api_key, timeout=30):
    """O cv_url guardado aponta para o endereço público; pede-se o mesmo
    caminho ao Formbricks pela rede local."""
    parsed = urllib.parse.urlsplit(cv_url or "")
    if not parsed.path.startswith("/storage/"):
        raise CVIndisponivel("sem CV associado")
    req = urllib.request.Request(base_url.rstrip("/") + parsed.path)
    req.add_header("x-api-key", api_key)
    req.add_header("User-Agent", "gec-fe2026-dashboard")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            dados = resp.read()
    except Exception as e:  # rede, 401, 404…
        raise CVIndisponivel(f"não foi possível obter o CV ({e})") from e
    # O formulário aceita qualquer ficheiro com nome .pdf. A 29 Set havia 2 em
    # 105 que não eram PDF (um vazio, outro uma página web gravada).
    if b"%PDF" not in dados[:1024]:
        raise CVIndisponivel("o ficheiro enviado no formulário não é um PDF válido "
                             f"({'está vazio' if not dados else 'é de outro formato'}); "
                             "é preciso pedir um novo CV ao gabinete que fez a inscrição")
    return dados


def marca_de_agua(pdf, empresa, data=None):
    """Sobrepõe em cinzento claro 'FE2026 · Fornecido a EMPRESA · data' a
    cada página. Se o PDF não se deixar processar, recusa-se servi-lo (nunca
    se entrega a uma empresa um CV sem marca)."""
    texto = f"FE2026 · Fornecido a {empresa} · {data or datetime.now().strftime('%d/%m/%Y')}"
    try:
        reader = PdfReader(io.BytesIO(pdf))
        if reader.is_encrypted and not reader.decrypt(""):
            raise CVIndisponivel("PDF protegido")
        writer = PdfWriter()
        for page in reader.pages:
            w, h = float(page.mediabox.width), float(page.mediabox.height)
            buf = io.BytesIO()
            c = canvas.Canvas(buf, pagesize=(w, h))
            c.setFillColor(Color(0.35, 0.35, 0.35, alpha=0.16))
            size = max(12, min(w, h) / 24)
            c.setFont("Helvetica-Bold", size)
            c.saveState()
            c.translate(w / 2, h / 2)
            c.rotate(35)
            c.drawCentredString(0, 0, texto)
            c.restoreState()
            c.setFillColor(Color(0.35, 0.35, 0.35, alpha=0.55))
            c.setFont("Helvetica", 7)
            c.drawCentredString(w / 2, 10, texto)
            c.save()
            buf.seek(0)
            page.merge_page(PdfReader(buf).pages[0])
            writer.add_page(page)
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except CVIndisponivel:
        raise
    except Exception as e:
        raise CVIndisponivel(f"PDF não processável ({type(e).__name__})") from e
