"""Leitura da página inicial de cada site oficial e coleta dos domínios externos que ela usa."""

import codecs
import http.client
import re
import threading
import urllib.error
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .dominio import USER_AGENT, eh_restrito, host_de_url, normalizar_url

# Atributos que apontam para outros endereços, por tag.
ATRIBUTOS = {
    "script": ("src",), "iframe": ("src",), "frame": ("src",), "link": ("href",),
    "img": ("src",), "source": ("src",), "embed": ("src",), "object": ("data",),
    "form": ("action",), "a": ("href",),
}
# Risco se o domínio cair em mãos erradas:
#  alto  = o site do governo carrega código ou conteúdo dele (script, iframe, CSS, formulário, redirecionamento)
#  medio = o site exibe uma imagem ou mídia dele
#  baixo = o site só tem um link para ele (risco de golpe para quem clicar)
PESO_POR_TAG = {
    "script": "alto", "iframe": "alto", "frame": "alto", "link": "alto", "embed": "alto",
    "object": "alto", "form": "alto", "meta": "alto", "img": "medio", "source": "medio", "a": "baixo",
}
ORDEM_PESO = {"alto": 0, "medio": 1, "baixo": 2}
MAX_BYTES = 3_000_000
_URL_META_REFRESH = re.compile(r"url\s*=\s*['\"]?([^'\";]+)", re.I)


class _Bloqueado(Exception):
    pass


class _ColetorReferencias(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.referencias = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and (attrs.get("http-equiv") or "").lower() == "refresh":
            m = _URL_META_REFRESH.search(attrs.get("content") or "")
            if m:
                self.referencias.append(("meta", m.group(1).strip()))
            return
        nomes = ATRIBUTOS.get(tag)
        if not nomes:
            return
        rel = (attrs.get("rel") or "").lower()
        # <link rel=canonical/alternate...> funciona como link comum; só estilo e pré-carga são carregados.
        tag_efetiva = "a" if tag == "link" and "stylesheet" not in rel and "preload" not in rel else tag
        for nome in nomes:
            valor = attrs.get(nome)
            if valor:
                self.referencias.append((tag_efetiva, valor.strip()))


class _Redirecionador(urllib.request.HTTPRedirectHandler):
    """Segue no máximo 5 redirecionamentos e respeita o robots.txt de cada destino."""
    max_redirections = 5

    def __init__(self, varredor):
        super().__init__()
        self.varredor = varredor

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        permitido, motivo = self.varredor.permitido(newurl)
        if not permitido:
            raise _Bloqueado(f"redirecionamento para {newurl} bloqueado: {motivo}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Varredor:
    def __init__(self, psl, timeout=20):
        self.psl = psl
        self.timeout = timeout
        self._robots = {}
        self._lock = threading.Lock()
        self._abridor = urllib.request.build_opener(_Redirecionador(self))

    def permitido(self, url):
        """(permitido, motivo). Segue a RFC 9309: 4xx = sem restrição; 5xx ou inacessível = bloqueio total."""
        partes = urlsplit(url)
        base = f"{partes.scheme}://{partes.netloc}"
        with self._lock:
            em_cache = self._robots.get(base)
        if em_cache is None:
            try:
                req = urllib.request.Request(base + "/robots.txt", headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    rp = urllib.robotparser.RobotFileParser()
                    rp.parse(resp.read(500_000).decode("utf-8", "replace").splitlines())
                em_cache = ("regras", rp)
            except urllib.error.HTTPError as e:
                em_cache = ("livre", None) if 400 <= e.code < 500 else ("bloqueado", f"robots.txt indisponível (HTTP {e.code})")
            except (OSError, http.client.HTTPException, ValueError) as e:
                em_cache = ("bloqueado", f"site inacessível ({type(e).__name__}: {getattr(e, 'reason', e)})")
            with self._lock:
                self._robots[base] = em_cache
        tipo, valor = em_cache
        if tipo == "livre":
            return True, None
        if tipo == "bloqueado":
            return False, valor
        return (True, None) if valor.can_fetch(USER_AGENT, url) else (False, "bloqueado pelo robots.txt")

    def baixar(self, url):
        """Devolve (url_final, html, erro, parcial)."""
        url = normalizar_url(url)
        try:
            permitido, motivo = self.permitido(url)
            if not permitido:
                return url, None, motivo, False
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"})
            with self._abridor.open(req, timeout=self.timeout) as resp:
                tipo = resp.headers.get("Content-Type", "")
                if "html" not in tipo.lower():
                    return resp.geturl(), None, f"conteúdo não é HTML ({tipo})", False
                corpo = resp.read(MAX_BYTES + 1)
                parcial = len(corpo) > MAX_BYTES
                charset = resp.headers.get_content_charset() or "utf-8"
                try:
                    codecs.lookup(charset)
                except LookupError:
                    charset = "utf-8"
                return resp.geturl(), corpo[:MAX_BYTES].decode(charset, "replace"), None, parcial
        except _Bloqueado as e:
            return url, None, str(e), False
        except urllib.error.HTTPError as e:
            return url, None, f"HTTP {e.code}", False
        except (OSError, http.client.HTTPException, ValueError) as e:
            return url, None, f"{type(e).__name__}: {getattr(e, 'reason', e)}", False

    def dominios_externos(self, url_site):
        """Lê a página inicial e agrupa tudo o que aponta para fora do site.

        externos: domínios registráveis (seção ICANN da PSL), que podem vencer num registro.
        plataformas: nomes em serviços de hospedagem (seção privada da PSL), que podem deixar
        de existir e ser recriados por outra pessoa no provedor.
        """
        url_final, html, erro, parcial = self.baixar(url_site)
        dominio_final = self.psl.registravel(host_de_url(url_final) or "")
        resultado = {"url_final": url_final, "dominio_final": dominio_final, "erro": erro, "parcial": parcial,
                     "externos": {}, "plataformas": {}}
        if html is None:
            return resultado
        hosts_proprios = {h for h in (host_de_url(url_site), host_de_url(url_final)) if h}
        proprios = {self.psl.registravel(h) for h in hosts_proprios}
        coletor = _ColetorReferencias()
        try:
            coletor.feed(html)
        except Exception as e:  # HTML muito quebrado: usa o que já foi coletado
            resultado["erro"] = f"HTML com erro de leitura: {type(e).__name__}"
        for tag, valor in coletor.referencias:
            if valor.lower().startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
                continue
            try:
                absoluto = urljoin(url_final, valor)
            except ValueError:  # endereço malformado na página (ex.: IPv6 inválido)
                continue
            if not absoluto.lower().startswith(("http://", "https://")):
                continue
            host = host_de_url(absoluto)
            if not host:
                continue
            dominio = self.psl.registravel(host)
            if not dominio or dominio in proprios or eh_restrito(dominio):
                continue
            peso = PESO_POR_TAG[tag]
            self._acumular(resultado["externos"], dominio, tag, peso, absoluto)
            plataforma = self.psl.registravel(host, incluir_privados=True)
            if plataforma and plataforma != dominio and host not in hosts_proprios:
                self._acumular(resultado["plataformas"], plataforma, tag, peso, absoluto)
        for grupo in (resultado["externos"], resultado["plataformas"]):
            for item in grupo.values():
                item["tags"] = sorted(item["tags"])
        return resultado

    @staticmethod
    def _acumular(grupo, chave, tag, peso, url):
        item = grupo.setdefault(chave, {"peso": peso, "tags": set(), "exemplos": []})
        item["tags"].add(tag)
        if ORDEM_PESO[peso] < ORDEM_PESO[item["peso"]]:
            item["peso"] = peso
        if len(item["exemplos"]) < 3 and url not in item["exemplos"]:
            item["exemplos"].append(url)
