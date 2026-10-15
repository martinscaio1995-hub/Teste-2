"""Consultas de rede: RDAP (situação e vencimento do registro) e DNS sobre HTTPS."""

import datetime as dt
import email.utils
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .dominio import USER_AGENT

BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
DOH_URLS = ("https://dns.google/resolve", "https://cloudflare-dns.com/dns-query")
# Garante o .br mesmo se o bootstrap da IANA falhar.
BOOTSTRAP_RESERVA = {"br": "https://rdap.registro.br/"}

# Intervalo mínimo entre consultas ao mesmo servidor RDAP, em segundos.
INTERVALO_RDAP = {"rdap.registro.br": 4.0}
INTERVALO_RDAP_PADRAO = 0.5
# Servidores que respondem 403 (e não 429) quando o limite de consultas é excedido.
LIMITE_COMO_403 = {"rdap.registro.br"}
PAUSA_MAXIMA = 900
ERROS_REDE = (OSError, http.client.HTTPException, ValueError)  # inclui URLError, timeout, SSL, JSON


def _agora_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _descrever(e):
    return f"{type(e).__name__}: {getattr(e, 'reason', e)}"


def _get_json(url, timeout=30, aceitar="application/rdap+json, application/json"):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": aceitar})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        dados = json.loads(resp.read().decode("utf-8"))
    if not isinstance(dados, dict):
        raise ValueError("resposta JSON não é um objeto")
    return dados


def salvar_json(caminho, dados):
    pasta = os.path.dirname(caminho) or "."
    os.makedirs(pasta, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=pasta, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=1)
    os.replace(tmp, caminho)


def segundos_retry_after(valor):
    """Interpreta Retry-After em segundos ou como data HTTP."""
    if not valor:
        return None
    valor = valor.strip()
    if valor.isdigit():
        return int(valor)
    try:
        quando = email.utils.parsedate_to_datetime(valor)
        return max(0, int((quando - dt.datetime.now(dt.timezone.utc)).total_seconds()))
    except (TypeError, ValueError):
        return None


class ClienteRDAP:
    """Consulta RDAP com cache em disco, intervalo por servidor e pausa compartilhada entre threads."""

    def __init__(self, pasta_cache, ttl_horas=24):
        self.caminho_cache = os.path.join(pasta_cache, "cache_rdap.json")
        self.caminho_bootstrap = os.path.join(pasta_cache, "rdap_bootstrap.json")
        self.ttl = ttl_horas * 3600
        self.cache = {}
        if os.path.exists(self.caminho_cache):
            try:
                with open(self.caminho_cache, encoding="utf-8") as f:
                    self.cache = json.load(f)
                if not isinstance(self.cache, dict):
                    raise ValueError("formato inesperado")
            except (OSError, ValueError) as e:
                os.replace(self.caminho_cache, self.caminho_cache + ".corrompido")
                print(f"AVISO: cache RDAP ilegível ({e}); renomeado para .corrompido", file=sys.stderr, flush=True)
                self.cache = {}
        self._servicos = None
        self._bootstrap_falhou_em = 0.0
        self._lock = threading.Lock()
        self._lock_boot = threading.Lock()
        self._ultimo = {}
        self._pendentes = 0

    def _bootstrap(self):
        if self._servicos is not None:
            return self._servicos
        with self._lock_boot:
            if self._servicos is not None:
                return self._servicos
            if self._bootstrap_falhou_em and time.time() - self._bootstrap_falhou_em < 600:
                return BOOTSTRAP_RESERVA
            dados = None
            fresco = os.path.exists(self.caminho_bootstrap) and time.time() - os.path.getmtime(self.caminho_bootstrap) < 7 * 86400
            if not fresco:
                try:
                    dados = _get_json(BOOTSTRAP_URL, aceitar="application/json")
                    salvar_json(self.caminho_bootstrap, dados)
                except ERROS_REDE as e:
                    print(f"AVISO: bootstrap RDAP da IANA indisponível ({_descrever(e)})", file=sys.stderr, flush=True)
            if dados is None and os.path.exists(self.caminho_bootstrap):
                try:
                    with open(self.caminho_bootstrap, encoding="utf-8") as f:
                        dados = json.load(f)
                except (OSError, ValueError):
                    dados = None
            if dados is None:
                self._bootstrap_falhou_em = time.time()
                return BOOTSTRAP_RESERVA
            servicos = dict(BOOTSTRAP_RESERVA)
            for tlds, urls in dados.get("services", []):
                base = next((u for u in urls if u.startswith("https://")), urls[0])
                for tld in tlds:
                    servicos[tld.lower()] = base if base.endswith("/") else base + "/"
            self._servicos = servicos
            return servicos

    def _esperar_vez(self, host):
        intervalo = INTERVALO_RDAP.get(host, INTERVALO_RDAP_PADRAO)
        with self._lock:
            agora = time.monotonic()
            proximo = max(agora, self._ultimo.get(host, 0) + intervalo)
            self._ultimo[host] = proximo
        if proximo > agora:
            time.sleep(proximo - agora)

    def _pausar(self, host, segundos):
        """Adia todas as próximas consultas a este servidor, em todas as threads."""
        with self._lock:
            self._ultimo[host] = max(self._ultimo.get(host, 0), time.monotonic() + segundos)

    def salvar(self):
        with self._lock:
            salvar_json(self.caminho_cache, self.cache)
            self._pendentes = 0

    def consultar(self, dominio):
        """Devolve {registrado, expiracao, status, eventos, erro, sem_rdap, fonte, consultado_em}."""
        em_cache = self.cache.get(dominio)
        if em_cache and not em_cache.get("erro") and time.time() - em_cache.get("_t", 0) < self.ttl:
            return em_cache
        tld = dominio.rsplit(".", 1)[-1]
        base = self._bootstrap().get(tld)
        if not base:
            if self._servicos is None:  # a lista da IANA não pôde ser obtida: falha temporária
                return {"registrado": None, "erro": "lista de servidores RDAP da IANA indisponível",
                        "consultado_em": _agora_iso()}
            return {"registrado": None, "sem_rdap": True, "erro": f"o registro de .{tld} não oferece RDAP",
                    "consultado_em": _agora_iso()}
        url = base + "domain/" + urllib.parse.quote(dominio)
        host = urllib.parse.urlsplit(url).hostname
        resultado = None
        for tentativa in range(6):
            self._esperar_vez(host)
            try:
                resultado = self._interpretar(_get_json(url), dominio)
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    resultado = {"registrado": False, "expiracao": None, "status": [], "eventos": {}}
                    break
                limite = e.code in (429, 503) or (e.code == 403 and host in LIMITE_COMO_403)
                if (limite or e.code in (500, 502, 504)) and tentativa < 5:
                    espera = segundos_retry_after(e.headers.get("Retry-After") if e.headers else None)
                    if espera is None:
                        espera = 60 * 2 ** tentativa if limite else 5 * 2 ** tentativa
                    self._pausar(host, min(espera, PAUSA_MAXIMA))
                    continue
                motivo = " (limite de consultas do servidor)" if limite else ""
                resultado = {"registrado": None, "erro": f"HTTP {e.code}{motivo}"}
                break
            except ERROS_REDE as e:
                if tentativa < 5:
                    time.sleep(min(3 * 2 ** tentativa, 30))
                    continue
                resultado = {"registrado": None, "erro": _descrever(e)}
        resultado.update({"fonte": url, "consultado_em": _agora_iso(), "_t": time.time()})
        with self._lock:
            self.cache[dominio] = resultado
            self._pendentes += 1
            salvar = self._pendentes >= 25
        if salvar:
            self.salvar()
        return resultado

    @staticmethod
    def _interpretar(dados, dominio=None):
        nome = str(dados.get("ldhName") or "").lower().rstrip(".")
        if dominio and nome and nome != dominio.lower():
            return {"registrado": None, "erro": f"o RDAP respondeu sobre outro domínio ({nome})"}
        eventos = {}
        for ev in dados.get("events") or []:
            if isinstance(ev, dict) and ev.get("eventAction") and ev.get("eventDate"):
                eventos[str(ev["eventAction"])] = str(ev["eventDate"])
        return {
            "registrado": True,
            "expiracao": eventos.get("expiration"),
            "status": [str(s).lower() for s in dados.get("status") or []],
            "eventos": eventos,
        }


def consultar_dns(nome, tipo="NS", timeout=15):
    """DNS sobre HTTPS. rcode 0 = o nome existe; 3 = NXDOMAIN (não existe)."""
    ultimo_erro = None
    for base in DOH_URLS:
        url = f"{base}?name={urllib.parse.quote(nome)}&type={tipo}"
        try:
            dados = _get_json(url, timeout=timeout, aceitar="application/dns-json")
            respostas = [r for r in dados.get("Answer") or [] if isinstance(r, dict)]
            ns = [str(r.get("data", "")).rstrip(".").lower() for r in respostas if r.get("type") == 2]
            return {"rcode": dados.get("Status"), "ns": ns, "erro": None}
        except ERROS_REDE as e:
            ultimo_erro = _descrever(e)
    return {"rcode": None, "ns": [], "erro": ultimo_erro}
