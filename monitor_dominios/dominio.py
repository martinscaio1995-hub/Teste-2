"""Nomes de domínio: domínio registrável (Public Suffix List) e sufixos de registro restrito."""

import http.client
import os
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit

PSL_URL = "https://publicsuffix.org/list/public_suffix_list.dat"
USER_AGENT = "monitor-dominios-gov/0.2 (verificacao defensiva de dominios publicos)"
MARCA_ICANN = "===BEGIN ICANN DOMAINS==="
MARCA_PRIVADOS = "===BEGIN PRIVATE DOMAINS==="

# Sufixos .br cujo registro é restrito a órgãos e instituições específicas. Um domínio
# nesses sufixos não pode ser registrado pelo público, então não corre o risco de ser
# tomado por terceiros quando o órgão deixa de usá-lo.
SUFIXOS_RESTRITOS = ("gov.br", "leg.br", "jus.br", "mp.br", "mil.br", "def.br", "tc.br", "b.br")

# Usado só quando a Public Suffix List não pode ser baixada nem existe em cache.
_PSL_MINIMA = """
// ===BEGIN ICANN DOMAINS===
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


def normalizar_url(url):
    url = (url or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        url = "http://" + url
    return url


def host_de_url(url):
    try:
        return normalizar_host(urlsplit(normalizar_url(url)).hostname)
    except ValueError:
        return None


def eh_restrito(dominio):
    return any(dominio == s or dominio.endswith("." + s) for s in SUFIXOS_RESTRITOS)


class PublicSuffixList:
    """Public Suffix List com as seções ICANN e privada separadas.

    A seção ICANN define o que de fato se registra num registro de domínios (com.br,
    com, net...). A seção privada lista plataformas que distribuem subdomínios
    (googleapis.com, github.io, azurewebsites.net...): um nome nessas plataformas não
    "vence" num registro, mas pode deixar de existir e ser recriado por outra pessoa.
    """

    def __init__(self, texto):
        self.regras, self.excecoes, self.curingas = {}, {}, {}  # regra -> é da seção privada?
        privada = False
        for linha in texto.splitlines():
            linha = linha.strip()
            if MARCA_PRIVADOS in linha:
                privada = True
            if not linha or linha.startswith("//"):
                continue
            bruta = linha.split()[0].lower()
            excecao = bruta.startswith("!")
            try:
                regra = ".".join(r if r == "*" else r.encode("idna").decode("ascii") for r in bruta.lstrip("!").split("."))
            except UnicodeError:
                continue
            if excecao:
                self.excecoes[regra] = privada
            elif regra.startswith("*."):
                self.curingas[regra[2:]] = privada
            else:
                self.regras[regra] = privada
        self.completa = MARCA_ICANN in texto and MARCA_PRIVADOS in texto

    def sufixo(self, host, incluir_privados=False):
        """Sufixo público mais longo que casa com o host (algoritmo da PSL)."""
        aceita = lambda privada: incluir_privados or not privada
        rotulos = host.split(".")
        for i in range(len(rotulos)):
            candidato = ".".join(rotulos[i:])
            if candidato in self.excecoes and aceita(self.excecoes[candidato]):
                return ".".join(rotulos[i + 1:])
            if candidato in self.regras and aceita(self.regras[candidato]):
                return candidato
            pai = ".".join(rotulos[i + 1:])
            if i + 1 < len(rotulos) and pai in self.curingas and aceita(self.curingas[pai]):
                return candidato
        return rotulos[-1]

    def registravel(self, host, incluir_privados=False):
        """Domínio registrável: 'www.prefeitura.exemplo.com.br' -> 'exemplo.com.br'.

        Com incluir_privados=True, nomes em plataformas ficam no nível da plataforma:
        'portal.azurewebsites.net' -> 'portal.azurewebsites.net' (em vez de 'azurewebsites.net').
        """
        host = normalizar_host(host)
        if not host or "." not in host:
            return None
        sufixo = self.sufixo(host, incluir_privados)
        if host == sufixo:
            return None
        return host[: -len(sufixo) - 1].split(".")[-1] + "." + sufixo


def _gravar_atomico(caminho, texto):
    pasta = os.path.dirname(caminho) or "."
    os.makedirs(pasta, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=pasta, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(texto)
    os.replace(tmp, caminho)


def carregar_psl(caminho_cache, max_idade_dias=7):
    """Devolve (PublicSuffixList, origem). origem: 'cache', 'baixada', 'cache antigo' ou 'mínima'."""
    def ler():
        with open(caminho_cache, encoding="utf-8") as f:
            texto = f.read()
        return texto if MARCA_ICANN in texto else None

    existe = os.path.exists(caminho_cache)
    if existe and time.time() - os.path.getmtime(caminho_cache) <= max_idade_dias * 86400:
        texto = ler()
        if texto:
            return PublicSuffixList(texto), "cache"
    try:
        req = urllib.request.Request(PSL_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            texto = resp.read().decode("utf-8")
        if MARCA_ICANN in texto and MARCA_PRIVADOS in texto:
            _gravar_atomico(caminho_cache, texto)
            return PublicSuffixList(texto), "baixada"
    except (OSError, http.client.HTTPException, ValueError):
        pass
    if existe:
        texto = ler()
        if texto:
            return PublicSuffixList(texto), "cache antigo"
    print("AVISO: Public Suffix List indisponível; usando uma lista mínima. Domínios fora de "
          ".com/.net/.org/.br comuns podem ser calculados errado.", file=sys.stderr, flush=True)
    return PublicSuffixList(_PSL_MINIMA), "mínima"
