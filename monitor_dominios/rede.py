"""Consultas de rede: RDAP (situação e vencimento do registro) e DNS sobre HTTPS."""

import datetime as dt
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .dominio import USER_AGENT

BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
DOH_URLS = ("https://dns.google/resolve", "https://cloudflare-dns.com/dns-query")

# Intervalo mínimo entre consultas ao mesmo servidor RDAP, em segundos.
# O Registro.br limita a taxa de consultas; os demais costumam aceitar mais.
INTERVALO_RDAP = {"rdap.registro.br": 2.0}
INTERVALO_RDAP_PADRAO = 0.5


def _agora_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _get_json(url, timeout=30, aceitar="application/rdap+json, application/json"):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": aceitar})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _salvar_json(caminho, dados):
    os.makedirs(os.path.dirname(caminho) or ".", exist_ok=True)
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=1)
    os.replace(tmp, caminho)


class ClienteRDAP:
    """Consulta RDAP com cache em disco, intervalo por servidor e novas tentativas."""

    def __init__(self, pasta_cache, ttl_horas=24):
        self.caminho_cache = os.path.join(pasta_cache, "cache_rdap.json")
        self.caminho_bootstrap = os.path.join(pasta_cache, "rdap_bootstrap.json")
        self.ttl = ttl_horas * 3600
        self.cache = {}
        if os.path.exists(self.caminho_cache):
            with open(self.caminho_cache, encoding="utf-8") as f:
                self.cache = json.load(f)
        self._servicos = None
        self._ultimo = {}
        self._lock = threading.Lock()
        self._pendentes = 0

    def _bootstrap(self):
        if self._servicos is None:
            dados = None
            if os.path.exists(self.caminho_bootstrap) and time.time() - os.path.getmtime(self.caminho_bootstrap) < 7 * 86400:
                with open(self.caminho_bootstrap, encoding="utf-8") as f:
                    dados = json.load(f)
            if dados is None:
                _, dados = _get_json(BOOTSTRAP_URL, aceitar="application/json")
                _salvar_json(self.caminho_bootstrap, dados)
            self._servicos = {}
            for tlds, urls in dados["services"]:
                base = next((u for u in urls if u.startswith("https://")), urls[0])
                for tld in tlds:
                    self._servicos[tld.lower()] = base if base.endswith("/") else base + "/"
        return self._servicos

    def _esperar_vez(self, host):
        intervalo = INTERVALO_RDAP.get(host, INTERVALO_RDAP_PADRAO)
        with self._lock:
            agora = time.monotonic()
            proximo = max(agora, self._ultimo.get(host, 0) + intervalo)
            self._ultimo[host] = proximo
        if proximo > agora:
            time.sleep(proximo - agora)

    def salvar(self):
        with self._lock:
            _salvar_json(self.caminho_cache, self.cache)
            self._pendentes = 0

    def consultar(self, dominio):
        """Devolve {registrado, expiracao, status, eventos, erro, fonte, consultado_em}."""
        em_cache = self.cache.get(dominio)
        if em_cache and not em_cache.get("erro") and time.time() - em_cache.get("_t", 0) < self.ttl:
            return em_cache
        tld = dominio.rsplit(".", 1)[-1]
        try:
            base = self._bootstrap().get(tld)
        except (OSError, ValueError, KeyError) as e:
            return {"registrado": None, "erro": f"bootstrap RDAP indisponível: {e}", "consultado_em": _agora_iso()}
        if not base:
            return {"registrado": None, "erro": f"sem servidor RDAP conhecido para .{tld}", "consultado_em": _agora_iso()}
        url = base + "domain/" + urllib.parse.quote(dominio)
        host = urllib.parse.urlsplit(url).hostname
        resultado = None
        for tentativa in range(5):
            self._esperar_vez(host)
            try:
                status, dados = _get_json(url)
                resultado = self._interpretar(dados)
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    resultado = {"registrado": False, "expiracao": None, "status": [], "eventos": {}}
                    break
                if e.code in (429, 500, 502, 503, 504) and tentativa < 4:
                    espera = e.headers.get("Retry-After")
                    espera = int(espera) if espera and espera.isdigit() else 5 * 2 ** tentativa
                    time.sleep(min(espera, 120))
                    continue
                resultado = {"registrado": None, "erro": f"HTTP {e.code}"}
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError) as e:
                if tentativa < 4:
                    time.sleep(3 * 2 ** tentativa)
                    continue
                resultado = {"registrado": None, "erro": f"{type(e).__name__}: {e}"}
        resultado.update({"fonte": url, "consultado_em": _agora_iso(), "_t": time.time()})
        with self._lock:
            self.cache[dominio] = resultado
            self._pendentes += 1
            salvar = self._pendentes >= 25
        if salvar:
            self.salvar()
        return resultado

    @staticmethod
    def _interpretar(dados):
        eventos = {}
        for ev in dados.get("events", []):
            acao, data = ev.get("eventAction"), ev.get("eventDate")
            if acao and data:
                eventos[acao] = data
        return {
            "registrado": True,
            "expiracao": eventos.get("expiration"),
            "status": [s.lower() for s in dados.get("status", [])],
            "eventos": eventos,
        }


def consultar_dns(dominio, timeout=15):
    """Consulta NS por DNS sobre HTTPS. rcode 0 = existe; 3 = NXDOMAIN (não existe)."""
    ultimo_erro = None
    for base in DOH_URLS:
        url = f"{base}?name={urllib.parse.quote(dominio)}&type=NS"
        try:
            _, dados = _get_json(url, timeout=timeout, aceitar="application/dns-json")
            ns = [r.get("data", "").rstrip(".") for r in dados.get("Answer", []) if r.get("type") == 2]
            return {"rcode": dados.get("Status"), "ns": ns, "erro": None}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError, ValueError) as e:
            ultimo_erro = f"{type(e).__name__}: {e}"
    return {"rcode": None, "ns": [], "erro": ultimo_erro}
