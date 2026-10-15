"""Linha de comando: coletar entidades, verificar domínios e gerar o relatório."""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import fontes, relatorio
from .analise import classificar, classificar_plataforma
from .dominio import carregar_psl, eh_restrito, host_de_url, normalizar_url
from .rede import ClienteRDAP, consultar_dns, salvar_json
from .varredura import Varredor


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _em_paralelo(funcao, itens, workers, rotulo=None):
    """Aplica funcao a cada item; uma falha num item vira {'erro': ...} sem derrubar os demais."""
    itens = list(itens)
    resultados = {}
    ex = ThreadPoolExecutor(max(1, workers))
    try:
        futuros = {ex.submit(funcao, it): it for it in itens}
        for i, fut in enumerate(as_completed(futuros), 1):
            item = futuros[fut]
            try:
                resultados[item] = fut.result()
            except Exception as e:
                resultados[item] = {"erro": f"falha inesperada: {type(e).__name__}: {e}"}
            if rotulo and i % 200 == 0:
                _log(f"  {rotulo}: {i}/{len(itens)}")
    except KeyboardInterrupt:
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown(wait=True)
    return resultados


def cmd_coletar(a):
    linhas = []
    if not a.sem_wikidata:
        _log("Consultando o Wikidata (municípios e estados com site oficial)...")
        linhas += fontes.coletar_wikidata()
    linhas += fontes.ler_manual(os.path.join(a.dados, "entidades_manuais.csv"))
    unicas = fontes.salvar_entidades(os.path.join(a.dados, "entidades.csv"), linhas)
    _log(f"{len(unicas)} entidade(s)/site(s) salvos em {os.path.join(a.dados, 'entidades.csv')}")


def _ocorrencia(e, relacao, peso, tags, exemplos):
    return {**{k: e.get(k, "") for k in ("entidade", "tipo", "uf", "codigo_ibge", "site", "fonte")},
            "relacao": relacao, "peso": peso, "tags": list(tags), "exemplos": list(exemplos)}


def cmd_verificar(a):
    rdap = ClienteRDAP(a.dados, ttl_horas=a.cache_horas)
    try:
        _verificar(a, rdap)
    except KeyboardInterrupt:
        rdap.salvar()
        _log("Interrompido. O cache RDAP foi salvo; rode de novo para continuar de onde parou.")
        sys.exit(130)


def _verificar(a, rdap):
    entidades = fontes.ler_entidades(os.path.join(a.dados, "entidades.csv"))
    if a.limite:
        entidades = entidades[: a.limite]
    psl, origem_psl = carregar_psl(os.path.join(a.dados, "public_suffix_list.dat"))
    for e in entidades:
        e["site"] = normalizar_url(e["site"])
        e["_dominio"] = psl.registravel(host_de_url(e["site"]) or "")
    sem_dominio = [{"entidade": e["entidade"], "site": e["site"]} for e in entidades if not e["_dominio"]]
    sites = {}
    for e in entidades:
        if e["_dominio"]:
            sites.setdefault(e["site"], []).append(e)

    alvos = {}       # domínio registrável -> ocorrências (próprio, redirecionamento, nameserver)
    externos = {}    # domínio registrável externo -> ocorrências
    plataformas = {}  # nome em serviço de hospedagem -> ocorrências

    # 1. Domínio do próprio site oficial.
    restritos = set()
    for e in entidades:
        d = e["_dominio"]
        if not d:
            continue
        if eh_restrito(d):
            restritos.add(d)
        else:
            alvos.setdefault(d, []).append(_ocorrencia(e, "proprio", "alto", [], [e["site"]]))
    _log(f"{len(entidades)} entidade(s); {len(alvos)} domínio(s) próprio(s) não restrito(s); "
         f"{len(restritos)} domínio(s) restrito(s) (verificados só pelos servidores DNS)")
    if sem_dominio:
        _log(f"AVISO: {len(sem_dominio)} entidade(s) sem domínio válido no campo site")

    # 2. Servidores DNS (NS) de todos os sites, inclusive .gov.br: se o domínio de um
    #    servidor DNS vencer, quem registrá-lo passa a controlar o endereço do site oficial.
    dominios_sites = sorted({e["_dominio"] for e in entidades if e["_dominio"]})
    _log(f"Consultando os servidores DNS de {len(dominios_sites)} domínio(s) de site...")
    ns_sites = _em_paralelo(consultar_dns, dominios_sites, a.workers, "NS")
    for e in entidades:
        d = e["_dominio"]
        for ns in (ns_sites.get(d) or {}).get("ns") or []:
            d_ns = psl.registravel(ns)
            if d_ns and d_ns != d and not eh_restrito(d_ns):
                lista = alvos.setdefault(d_ns, [])
                if not any(o["relacao"] == "nameserver" and o["entidade"] == e["entidade"] and o["site"] == e["site"] for o in lista):
                    lista.append(_ocorrencia(e, "nameserver", "alto", [], [ns]))

    # 3. Página inicial: redirecionamentos, domínios externos e nomes em plataformas.
    sites_tentados = len(sites)
    nao_lidos, parciais = [], []
    sites_lidos = 0
    if not a.sem_externos:
        varredor = Varredor(psl)
        _log(f"Lendo a página inicial de {sites_tentados} site(s) com {a.workers} conexões...")
        res_sites = _em_paralelo(varredor.dominios_externos, sites, a.workers, "páginas")
        for site, res in res_sites.items():
            ents = sites[site]
            lida = res.get("erro") is None or bool(res.get("externos"))
            if lida:
                sites_lidos += 1
            else:
                nao_lidos.append({"site": site, "entidades": [e["entidade"] for e in ents], "erro": res.get("erro")})
            if res.get("parcial"):
                parciais.append({"site": site, "entidades": [e["entidade"] for e in ents], "erro": "página truncada em 3 MB"})
            d_final = res.get("dominio_final")
            for e in ents:
                if d_final and d_final != e["_dominio"] and not eh_restrito(d_final):
                    alvos.setdefault(d_final, []).append(
                        _ocorrencia(e, "redirecionamento", "alto", [], [res.get("url_final", "")]))
                for d, info in (res.get("externos") or {}).items():
                    externos.setdefault(d, []).append(_ocorrencia(e, "externo", info["peso"], info["tags"], info["exemplos"]))
                for p, info in (res.get("plataformas") or {}).items():
                    plataformas.setdefault(p, []).append(_ocorrencia(e, "plataforma", info["peso"], info["tags"], info["exemplos"]))
        _log(f"{sites_lidos}/{sites_tentados} sites lidos; {len(externos)} domínio(s) externo(s); "
             f"{len(plataformas)} nome(s) em plataformas de hospedagem")
    else:
        sites_tentados = 0

    # 4. DNS de todos os domínios; RDAP dos domínios que podem afetar o site.
    todos = sorted(set(alvos) | set(externos))
    _log(f"Consultando DNS de {len(todos)} domínio(s) e {len(plataformas)} nome(s) em plataformas...")
    dns = _em_paralelo(consultar_dns, [d for d in todos if d not in ns_sites], a.workers, "DNS")
    dns.update({d: ns_sites[d] for d in todos if d in ns_sites})
    dns_plat = _em_paralelo(consultar_dns, plataformas, a.workers, "DNS plataformas")

    def precisa_rdap(d):
        if d in alvos or a.completo or (dns.get(d) or {}).get("rcode") != 0:
            return True
        return any(o["peso"] in ("alto", "medio") for o in externos.get(d, []))

    para_rdap = [d for d in todos if precisa_rdap(d)]
    _log(f"Consultando RDAP de {len(para_rdap)} domínio(s) (com pausa entre consultas a cada servidor)...")
    res_rdap = _em_paralelo(rdap.consultar, para_rdap, min(a.workers, 6), "RDAP")
    rdap.salvar()

    # 5. Classificação.
    linhas = []
    for d in todos:
        r = res_rdap.get(d)
        if r is None:
            gravidade, situacao, dias = "NAO CONSULTADO", "só aparece como link e resolve no DNS; use --completo para consultar o RDAP", None
            r = {}
        else:
            try:
                gravidade, situacao, dias = classificar(r, dns.get(d), dias_alto=a.dias_alto, dias_medio=a.dias_medio)
            except Exception as e:
                gravidade, situacao, dias = "ERRO", f"dado RDAP inválido: {type(e).__name__}: {e}", None
        for o in alvos.get(d, []) + externos.get(d, []):
            linhas.append({**o, "dominio": d, "gravidade": gravidade, "situacao": situacao, "dias": dias,
                           "expiracao": (r.get("expiracao") or "")[:10], "status": r.get("status") or [],
                           "dns_rcode": (dns.get(d) or {}).get("rcode"), "consultado_em": r.get("consultado_em", ""),
                           "rdap_fonte": r.get("fonte", "")})
    for p, ocorrencias in plataformas.items():
        gravidade, situacao, dias = classificar_plataforma(dns_plat.get(p))
        for o in ocorrencias:
            linhas.append({**o, "dominio": p, "gravidade": gravidade, "situacao": situacao, "dias": None,
                           "expiracao": "", "status": [], "dns_rcode": (dns_plat.get(p) or {}).get("rcode"),
                           "consultado_em": "", "rdap_fonte": ""})

    resumo = {"gerado_em": relatorio.agora_iso(), "entidades": len(entidades), "sites_tentados": sites_tentados,
              "sites_lidos": sites_lidos, "dominios_verificados": len(todos),
              "dominios_com_rdap": len(para_rdap), "dominios_alvo": len(alvos),
              "dominios_externos": len(set(externos) - set(alvos)), "plataformas": len(plataformas),
              "dominios_restritos": len(restritos), "psl": origem_psl,
              "dias_alto": a.dias_alto, "dias_medio": a.dias_medio}
    salvar_json(os.path.join(a.saida, "resultados.json"),
                {"resumo": resumo, "linhas": linhas, "sites_nao_lidos": nao_lidos,
                 "leituras_parciais": parciais, "sem_dominio": sem_dominio})
    _log(f"Resultados salvos em {os.path.join(a.saida, 'resultados.json')}")
    cmd_relatorio(a)


def cmd_relatorio(a):
    with open(os.path.join(a.saida, "resultados.json"), encoding="utf-8") as f:
        dados = json.load(f)
    n = relatorio.gerar(a.saida, dados)
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
        v.add_argument("--sem-externos", action="store_true", help="não lê as páginas; só domínios próprios e servidores DNS")
        v.add_argument("--completo", action="store_true", help="consulta o RDAP também dos domínios que só aparecem como link")
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
