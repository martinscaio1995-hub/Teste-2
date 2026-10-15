"""Leitura da página inicial de cada site oficial e coleta dos domínios externos que ela usa."""

import urllib.error
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .dominio import USER_AGENT, eh_restrito, host_de_url

# Atributos que apontam para outros endereços, por tag.
ATRIBUTOS = {
    "script": ("src",), "iframe": ("src",), "frame": ("src",), "link": ("href",),
    "img": ("src",), "source": ("src",), "embed": ("src",), "object": ("data",),
    "form": ("action",), "a": ("href",),
}
# Risco se o domínio cair em mãos erradas:
#  alto  = o site do governo carrega código ou conteúdo dele (script, iframe, CSS, formulário)
#  medio = o site exibe uma imagem ou mídia dele
#  baixo = o site só tem um link para ele (risco de golpe para quem clicar)
PESO_POR_TAG = {
    "script": "alto", "iframe": "alto", "frame": "alto", "link": "alto", "embed": "alto",
    "object": "alto", "form": "alto", "img": "medio", "source": "medio", "a": "baixo",
}
ORDEM_PESO = {"alto": 0, "medio": 1, "baixo": 2}
MAX_BYTES = 3_000_000


class _ColetorReferencias(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.referencias = []

    def handle_starttag(self, tag, attrs):
        nomes = ATRIBUTOS.get(tag)
        if not nomes:
            return
        attrs = dict(attrs)
        if tag == "link" and "stylesheet" not in (attrs.get("rel") or "").lower() and "preload" not in (attrs.get("rel") or "").lower():
            tag_efetiva = "a"  # <link rel=canonical/alternate...> funciona como link comum
        else:
            tag_efetiva = tag
        for nome in nomes:
            valor = attrs.get(nome)
            if valor:
                self.referencias.append((tag_efetiva, valor.strip()))


class Varredor:
    def __init__(self, psl, timeout=20):
        self.psl = psl
        self.timeout = timeout
        self._robots = {}

    def _permitido(self, url):
        partes = urlsplit(url)
        base = f"{partes.scheme}://{partes.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                req = urllib.request.Request(base + "/robots.txt", headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    rp.parse(resp.read(200_000).decode("utf-8", "replace").splitlines())
            except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
                rp = None  # sem robots.txt acessível: permitido
            self._robots[base] = rp
        rp = self._robots[base]
        return rp is None or rp.can_fetch(USER_AGENT, url)

    def baixar(self, url):
        """Devolve (url_final, html, erro)."""
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        try:
            if not self._permitido(url):
                return url, None, "bloqueado pelo robots.txt"
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                tipo = resp.headers.get("Content-Type", "")
                if "html" not in tipo.lower():
                    return resp.geturl(), None, f"conteúdo não é HTML ({tipo})"
                corpo = resp.read(MAX_BYTES)
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.geturl(), corpo.decode(charset, "replace"), None
        except urllib.error.HTTPError as e:
            return url, None, f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError, LookupError) as e:
            return url, None, f"{type(e).__name__}: {getattr(e, 'reason', e)}"

    def dominios_externos(self, url_site):
        """Lê a página inicial e agrupa por domínio registrável tudo o que aponta para fora do site."""
        url_final, html, erro = self.baixar(url_site)
        if html is None:
            return {"url_final": url_final, "erro": erro, "externos": {}}
        proprios = {self.psl.registravel(h) for h in (host_de_url(url_site), host_de_url(url_final)) if h}
        coletor = _ColetorReferencias()
        try:
            coletor.feed(html)
        except Exception as e:  # HTML muito quebrado: usa o que já foi coletado
            erro = f"HTML com erro de leitura: {type(e).__name__}"
        externos = {}
        for tag, valor in coletor.referencias:
            if valor.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
                continue
            try:
                absoluto = urljoin(url_final, valor)
            except ValueError:  # endereço malformado na página (ex.: IPv6 inválido)
                continue
            if not absoluto.startswith(("http://", "https://")):
                continue
            host = host_de_url(absoluto)
            dominio = self.psl.registravel(host) if host else None
            if not dominio or dominio in proprios or eh_restrito(dominio):
                continue
            peso = PESO_POR_TAG[tag]
            item = externos.setdefault(dominio, {"peso": peso, "tags": set(), "exemplos": []})
            item["tags"].add(tag)
            if ORDEM_PESO[peso] < ORDEM_PESO[item["peso"]]:
                item["peso"] = peso
            if len(item["exemplos"]) < 3 and absoluto not in item["exemplos"]:
                item["exemplos"].append(absoluto)
        for item in externos.values():
            item["tags"] = sorted(item["tags"])
        return {"url_final": url_final, "erro": erro, "externos": externos}
