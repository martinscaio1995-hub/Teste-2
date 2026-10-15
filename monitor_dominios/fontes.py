"""Lista de entidades públicas e seus sites oficiais."""

import csv
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .dominio import USER_AGENT, normalizar_url

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"

# Municípios do Brasil (Q3184121) e unidades federativas (Q485258) com site oficial (P856).
CONSULTAS = {
    "municipio": """
SELECT ?item ?itemLabel ?ufLabel ?ibge ?site WHERE {
  ?item wdt:P31 wd:Q3184121; wdt:P856 ?site .
  OPTIONAL { ?item rdfs:label ?itemLabel . FILTER(LANG(?itemLabel) = "pt") }
  OPTIONAL { ?item wdt:P131 ?uf . ?uf wdt:P31 wd:Q485258; rdfs:label ?ufLabel . FILTER(LANG(?ufLabel) = "pt") }
  OPTIONAL { ?item wdt:P1585 ?ibge . }
}""",
    "estado": """
SELECT ?item ?itemLabel ?ufLabel ?ibge ?site WHERE {
  ?item wdt:P31 wd:Q485258; wdt:P856 ?site .
  OPTIONAL { ?item rdfs:label ?itemLabel . FILTER(LANG(?itemLabel) = "pt") }
  OPTIONAL { ?item wdt:P1585 ?ibge . }
  BIND(?itemLabel AS ?ufLabel)
}""",
}

CAMPOS = ["entidade", "tipo", "uf", "codigo_ibge", "site", "fonte"]


def _sparql(consulta, timeout=180, tentativas=6):
    """Consulta o Wikidata respeitando o limite de taxa (HTTP 429 + Retry-After)."""
    url = WIKIDATA_SPARQL + "?" + urllib.parse.urlencode({"query": consulta})
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"})
    for tentativa in range(tentativas):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))["results"]["bindings"]
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or tentativa == tentativas - 1:
                raise
            espera = e.headers.get("Retry-After")
            espera = int(espera) if espera and espera.isdigit() else 65
            motivo = f"HTTP {e.code}"
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            if tentativa == tentativas - 1:
                raise
            espera, motivo = 65, f"resposta incompleta ou falha de conexão ({type(e).__name__})"
        print(f"Wikidata: {motivo}; nova tentativa em {espera}s...", file=sys.stderr, flush=True)
        time.sleep(min(espera, 300))


def coletar_wikidata(tipos=("municipio", "estado")):
    linhas = []
    for tipo in tipos:
        for b in _sparql(CONSULTAS[tipo]):
            v = lambda k: b.get(k, {}).get("value", "")
            linhas.append({
                "entidade": v("itemLabel"), "tipo": tipo, "uf": v("ufLabel"), "codigo_ibge": v("ibge"),
                "site": v("site"), "fonte": "wikidata:" + v("item").rsplit("/", 1)[-1],
            })
    return linhas


def ler_manual(caminho):
    """CSV com colunas entidade,tipo,uf,site (as demais são opcionais)."""
    if not os.path.exists(caminho):
        return []
    with open(caminho, encoding="utf-8-sig", newline="") as f:
        linhas = []
        for r in csv.DictReader(f):
            if (r.get("site") or "").strip():
                linha = {c: (r.get(c) or "").strip() for c in CAMPOS} | {"fonte": "manual"}
                linha["site"] = normalizar_url(linha["site"])
                linhas.append(linha)
        return linhas


def salvar_entidades(caminho, linhas):
    vistos, unicas = set(), []
    for r in linhas:
        chave = (r["entidade"], r["site"].rstrip("/").lower())
        if chave not in vistos:
            vistos.add(chave)
            unicas.append(r)
    os.makedirs(os.path.dirname(caminho) or ".", exist_ok=True)
    with open(caminho, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        w.writeheader()
        w.writerows(unicas)
    return unicas


def ler_entidades(caminho):
    with open(caminho, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))
