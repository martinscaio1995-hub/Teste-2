"""Linha de comando: coletar entidades, verificar domínios e gerar o relatório."""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import fontes, relatorio
from .analise import classificar
from .dominio import carregar_psl, eh_restrito, host_de_url
from .rede import ClienteRDAP, consultar_dns
from .varredura import Varredor


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def cmd_coletar(a):
    linhas = []
    if not a.sem_wikidata:
        _log("Consultando o Wikidata (municípios e estados com site oficial)...")
        linhas += fontes.coletar_wikidata()
    linhas += fontes.ler_manual(os.path.join(a.dados, "entidades_manuais.csv"))
    unicas = fontes.salvar_entidades(os.path.join(a.dados, "entidades.csv"), linhas)
    _log(f"{len(unicas)} entidade(s)/site(s) salvos em {os.path.join(a.dados, 'entidades.csv')}")


def cmd_verificar(a):
    entidades = fontes.ler_entidades(os.path.join(a.dados, "entidades.csv"))
    if a.limite:
        entidades = entidades[: a.limite]
    psl = carregar_psl(os.path.join(a.dados, "public_suffix_list.dat"))
    rdap = ClienteRDAP(a.dados, ttl_horas=a.cache_horas)

    # 1. Domínios próprios (o domínio do próprio site oficial).
    alvos = {}  # dominio -> lista de ocorrências
    restritos = 0
    for e in entidades:
        dominio = psl.registravel(host_de_url(e["site"]) or "")
        if not dominio:
            continue
        if eh_restrito(dominio):
            restritos += 1
            continue
        alvos.setdefault(dominio, []).append({**e, "relacao": "proprio", "peso": "", "tags": [], "exemplos": [e["site"]]})
    proprios = len(alvos)
    _log(f"{len(entidades)} entidade(s); {proprios} domínio(s) próprio(s) não restrito(s); {restritos} restrito(s) ignorado(s)")

    # 2. Domínios externos referenciados na página inicial de cada site.
    sites_lidos = sites_tentados = 0
    externos = {}
    nao_lidos = []
    if not a.sem_externos:
        varredor = Varredor(psl)
        sites = {}
        for e in entidades:
            sites.setdefault(e["site"], []).append(e)
        sites_tentados = len(sites)
        _log(f"Lendo a página inicial de {sites_tentados} site(s) com {a.workers} conexões...")
        with ThreadPoolExecutor(a.workers) as ex:
            futuros = {ex.submit(varredor.dominios_externos, s): s for s in sites}
            for i, fut in enumerate(as_completed(futuros), 1):
                site = futuros[fut]
                try:
                    res = fut.result()
                except Exception as e:  # falha inesperada num site não interrompe os demais
                    res = {"url_final": site, "erro": f"falha inesperada: {type(e).__name__}: {e}", "externos": {}}
                if res["erro"] is None or res["externos"]:
                    sites_lidos += 1
                else:
                    nao_lidos.append({"site": site, "entidades": [e["entidade"] for e in sites[site]], "erro": res["erro"]})
                for dominio, info in res["externos"].items():
                    for e in sites[site]:
                        externos.setdefault(dominio, []).append(
                            {**e, "relacao": "externo", "peso": info["peso"], "tags": info["tags"], "exemplos": info["exemplos"]})
                if i % 100 == 0:
                    _log(f"  {i}/{sites_tentados} sites; {len(externos)} domínio(s) externo(s) até agora")
        _log(f"{sites_lidos}/{sites_tentados} sites lidos; {len(externos)} domínio(s) externo(s) distintos")

    # 3. DNS para todos; RDAP para próprios, externos de peso alto/médio e os que não resolvem.
    todos = sorted(set(alvos) | set(externos))
    _log(f"Consultando DNS de {len(todos)} domínio(s)...")
    dns = {}
    with ThreadPoolExecutor(a.workers) as ex:
        for d, r in zip(todos, ex.map(consultar_dns, todos)):
            dns[d] = r

    def precisa_rdap(d):
        if d in alvos or a.completo or dns[d].get("rcode") != 0:
            return True
        return any(o["peso"] in ("alto", "medio") for o in externos.get(d, []))

    para_rdap = [d for d in todos if precisa_rdap(d)]
    _log(f"Consultando RDAP de {len(para_rdap)} domínio(s) (com intervalo entre consultas a cada servidor)...")
    res_rdap = {}
    with ThreadPoolExecutor(min(a.workers, 6)) as ex:
        futuros = {ex.submit(rdap.consultar, d): d for d in para_rdap}
        for i, fut in enumerate(as_completed(futuros), 1):
            res_rdap[futuros[fut]] = fut.result()
            if i % 100 == 0:
                _log(f"  RDAP {i}/{len(para_rdap)}")
    rdap.salvar()

    # 4. Classificação.
    linhas = []
    for d in todos:
        r = res_rdap.get(d)
        if r is None:  # só DNS: externo de link simples que resolve normalmente
            gravidade, situacao, dias = "OK", "resolve no DNS (RDAP não consultado: só link)", None
            r = {}
        else:
            gravidade, situacao, dias = classificar(r, dns[d], dias_alto=a.dias_alto, dias_medio=a.dias_medio)
        for o in alvos.get(d, []) + externos.get(d, []):
            linhas.append({**o, "dominio": d, "gravidade": gravidade, "situacao": situacao, "dias": dias,
                           "expiracao": (r.get("expiracao") or "")[:10], "status": r.get("status") or [],
                           "dns_rcode": dns[d].get("rcode"), "consultado_em": r.get("consultado_em", ""),
                           "rdap_fonte": r.get("fonte", "")})

    resumo = {"gerado_em": relatorio.agora_iso(), "entidades": len(entidades), "sites_tentados": sites_tentados,
              "sites_lidos": sites_lidos, "dominios_verificados": len(todos), "proprios_verificados": proprios,
              "externos_verificados": len(set(externos) - set(alvos)), "proprios_restritos": restritos}
    os.makedirs(a.saida, exist_ok=True)
    with open(os.path.join(a.saida, "resultados.json"), "w", encoding="utf-8") as f:
        json.dump({"resumo": resumo, "linhas": linhas, "sites_nao_lidos": nao_lidos}, f, ensure_ascii=False, indent=1)
    _log(f"Resultados salvos em {os.path.join(a.saida, 'resultados.json')}")
    cmd_relatorio(a)


def cmd_relatorio(a):
    with open(os.path.join(a.saida, "resultados.json"), encoding="utf-8") as f:
        dados = json.load(f)
    n = relatorio.gerar(a.saida, dados["linhas"], dados["resumo"], dados.get("sites_nao_lidos", []))
    _log(f"Relatório: {os.path.join(a.saida, 'relatorio.md')} | tabela: achados.csv | {n} modelo(s) de aviso em avisos/")


def main(argv=None):
    p = argparse.ArgumentParser(prog="monitor_dominios", description="Monitora domínios ligados a sites de órgãos públicos.")
    p.add_argument("--dados", default="dados", help="pasta de dados e cache (padrão: dados)")
    p.add_argument("--saida", default="saida", help="pasta do relatório (padrão: saida)")
    sub = p.add_subparsers(dest="comando", required=True)

    c = sub.add_parser("coletar", help="monta dados/entidades.csv a partir do Wikidata e da lista manual")
    c.add_argument("--sem-wikidata", action="store_true", help="usa só dados/entidades_manuais.csv")
    c.set_defaults(func=cmd_coletar)

    for nome, ajuda in (("verificar", "verifica os domínios e gera o relatório"), ("tudo", "coletar + verificar")):
        v = sub.add_parser(nome, help=ajuda)
        v.add_argument("--limite", type=int, default=0, help="verifica só as N primeiras entidades (teste)")
        v.add_argument("--sem-externos", action="store_true", help="não lê as páginas; só os domínios próprios")
        v.add_argument("--completo", action="store_true", help="consulta RDAP também dos domínios só linkados")
        v.add_argument("--workers", type=int, default=8)
        v.add_argument("--dias-alto", type=int, default=30)
        v.add_argument("--dias-medio", type=int, default=90)
        v.add_argument("--cache-horas", type=int, default=24)
        if nome == "tudo":
            v.add_argument("--sem-wikidata", action="store_true")
            v.set_defaults(func=lambda a: (cmd_coletar(a), cmd_verificar(a)))
        else:
            v.set_defaults(func=cmd_verificar)

    r = sub.add_parser("relatorio", help="regera o relatório a partir de saida/resultados.json")
    r.set_defaults(func=cmd_relatorio)

    a = p.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
