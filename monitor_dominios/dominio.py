"""Nomes de domínio: domínio registrável (Public Suffix List) e sufixos de registro restrito."""

import os
import time
import urllib.request
from urllib.parse import urlsplit

PSL_URL = "https://publicsuffix.org/list/public_suffix_list.dat"
USER_AGENT = "monitor-dominios-gov/0.1 (verificacao defensiva de dominios publicos)"

# Sufixos .br cujo registro é restrito a órgãos e instituições específicas. Um domínio
# nesses sufixos não pode ser registrado pelo público, então não corre o risco de ser
# tomado por terceiros quando o órgão deixa de usá-lo.
SUFIXOS_RESTRITOS = ("gov.br", "leg.br", "jus.br", "mp.br", "mil.br", "def.br", "tc.br", "b.br")

# Usado só quando a Public Suffix List não pode ser baixada nem está em cache.
_PSL_MINIMA = """
com
net
org
br
com.br
net.br
org.br
gov.br
leg.br
jus.br
mp.br
edu.br
art.br
blog.br
eco.br
emp.br
ind.br
inf.br
tur.br
app.br
dev.br
"""


def normalizar_host(host):
    """Minúsculas, sem ponto final, sem porta; nomes internacionalizados em punycode."""
    if not host:
        return None
    host = host.strip().lower().rstrip(".")
    if host.startswith("[") or host.replace(".", "").isdigit():
        return None  # endereço IP, não domínio
    host = host.split(":")[0]
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return host or None


def host_de_url(url):
    try:
        return normalizar_host(urlsplit(url.strip()).hostname)
    except ValueError:
        return None


def eh_restrito(dominio):
    return any(dominio == s or dominio.endswith("." + s) for s in SUFIXOS_RESTRITOS)


class PublicSuffixList:
    def __init__(self, texto):
        self.regras, self.excecoes, self.curingas = set(), set(), set()
        for linha in texto.splitlines():
            linha = linha.strip()
            if not linha or linha.startswith("//"):
                continue
            regra = linha.split()[0].lower()
            try:
                regra = ".".join(r.encode("idna").decode("ascii") if r != "*" else r for r in regra.lstrip("!").split("."))
                regra = ("!" if linha.startswith("!") else "") + regra
            except UnicodeError:
                continue
            if regra.startswith("!"):
                self.excecoes.add(regra[1:])
            elif regra.startswith("*."):
                self.curingas.add(regra[2:])
            else:
                self.regras.add(regra)

    def sufixo(self, host):
        """Sufixo público mais longo que casa com o host (algoritmo da PSL)."""
        rotulos = host.split(".")
        for i in range(len(rotulos)):
            candidato = ".".join(rotulos[i:])
            if candidato in self.excecoes:
                return ".".join(rotulos[i + 1:])
            if candidato in self.regras:
                return candidato
            if i + 1 < len(rotulos) and ".".join(rotulos[i + 1:]) in self.curingas:
                return candidato
        return rotulos[-1]

    def registravel(self, host):
        """Domínio que de fato se registra: 'www.prefeitura.exemplo.com.br' -> 'exemplo.com.br'."""
        host = normalizar_host(host)
        if not host or "." not in host:
            return None
        sufixo = self.sufixo(host)
        if host == sufixo:
            return None
        return host[: -len(sufixo) - 1].split(".")[-1] + "." + sufixo


def carregar_psl(caminho_cache, max_idade_dias=7):
    """Lê a Public Suffix List do cache, baixando de novo se estiver ausente ou velha."""
    velho = not os.path.exists(caminho_cache) or time.time() - os.path.getmtime(caminho_cache) > max_idade_dias * 86400
    if velho:
        try:
            req = urllib.request.Request(PSL_URL, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                texto = resp.read().decode("utf-8")
            os.makedirs(os.path.dirname(caminho_cache) or ".", exist_ok=True)
            with open(caminho_cache, "w", encoding="utf-8") as f:
                f.write(texto)
        except OSError:
            pass
    if os.path.exists(caminho_cache):
        with open(caminho_cache, encoding="utf-8") as f:
            return PublicSuffixList(f.read())
    return PublicSuffixList(_PSL_MINIMA)
