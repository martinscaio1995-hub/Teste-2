"""Geração do relatório (Markdown e CSV) e dos modelos de aviso por entidade."""

import csv
import datetime as dt
import os
import re
import unicodedata
from collections import Counter, defaultdict

from .analise import ORDEM_GRAVIDADE

COLUNAS = ["gravidade", "situacao", "dias", "expiracao", "dominio", "relacao", "peso", "tags",
           "entidade", "tipo", "uf", "site", "exemplos", "status", "dns_rcode", "consultado_em", "rdap_fonte"]
NOMES_GRAVIDADE = {"CRITICO": "Crítico", "ALTO": "Alto", "MEDIO": "Médio", "VERIFICAR": "Verificar",
                   "OK": "OK", "SEM DATA": "Sem data", "ERRO": "Erro"}
DESCRICAO_PESO = {"alto": "o site carrega código ou conteúdo dele", "medio": "o site exibe imagem ou mídia dele",
                  "baixo": "o site tem um link para ele", "": "domínio do próprio site"}


def _ordem(r):
    return (ORDEM_GRAVIDADE.get(r["gravidade"], 9), {"alto": 0, "medio": 1, "baixo": 2}.get(r.get("peso") or "", -1),
            r["dominio"], r["entidade"])


def _cel(v):
    return str(v if v is not None else "").replace("|", "\\|").replace("\n", " ")


def _slug(texto):
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    texto = re.sub(r"[^\w\s-]", "", texto).strip().lower()
    return re.sub(r"[\s_]+", "-", texto)[:80] or "entidade"


def escrever_csv(caminho, linhas):
    with open(caminho, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUNAS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(linhas, key=_ordem):
            w.writerow({**r, "tags": " ".join(r.get("tags") or []), "exemplos": " ".join(r.get("exemplos") or []),
                        "status": " ".join(r.get("status") or [])})


def escrever_markdown(caminho, linhas, resumo, nao_lidos=()):
    data = resumo["gerado_em"][:10]
    cont = Counter(r["gravidade"] for r in linhas)
    dominios = defaultdict(set)
    for r in linhas:
        dominios[r["gravidade"]].add(r["dominio"])
    out = [f"# Domínios ligados a sites públicos — situação em {data}", ""]
    out += ["## Resumo", "",
            f"- Entidades analisadas: {resumo['entidades']}",
            f"- Sites lidos com sucesso (página inicial): {resumo['sites_lidos']} de {resumo['sites_tentados']}",
            f"- Domínios verificados: {resumo['dominios_verificados']} "
            f"({resumo['proprios_verificados']} próprios, {resumo['externos_verificados']} externos)",
            f"- Domínios próprios ignorados por serem de registro restrito (.gov.br, .leg.br, .jus.br...): {resumo['proprios_restritos']}",
            ""]
    out += ["| Gravidade | Ocorrências | Domínios distintos |", "|---|---|---|"]
    for g in sorted(cont, key=lambda g: ORDEM_GRAVIDADE.get(g, 9)):
        out.append(f"| {NOMES_GRAVIDADE.get(g, g)} | {cont[g]} | {len(dominios[g])} |")
    out.append("")
    for g in ("CRITICO", "ALTO", "MEDIO", "VERIFICAR"):
        grupo = sorted((r for r in linhas if r["gravidade"] == g), key=_ordem)
        if not grupo:
            continue
        out += [f"## {NOMES_GRAVIDADE[g]}", "",
                "| Domínio | Situação | Relação com o site | Entidade | UF | Site | Exemplo de uso |",
                "|---|---|---|---|---|---|---|"]
        for r in grupo:
            rel = "próprio" if r["relacao"] == "proprio" else f"externo ({r['peso']}: {', '.join(r.get('tags') or [])})"
            ex = (r.get("exemplos") or [""])[0]
            out.append(f"| {_cel(r['dominio'])} | {_cel(r['situacao'])} | {_cel(rel)} | {_cel(r['entidade'])} | "
                       f"{_cel(r['uf'])} | {_cel(r['site'])} | {_cel(ex)} |")
        out.append("")
    erros = [r for r in linhas if r["gravidade"] == "ERRO"]
    if erros:
        out += [f"## Consultas com erro ({len(erros)})", "",
                "Estes domínios não puderam ser verificados nesta execução e devem ser consultados de novo:", ""]
        vistos = []
        for r in sorted(erros, key=_ordem):
            if r["dominio"] not in vistos:
                vistos.append(r["dominio"])
                out.append(f"- {r['dominio']}: {r['situacao']}")
        out.append("")
    if nao_lidos:
        out += [f"## Sites cuja página inicial não pôde ser lida ({len(nao_lidos)})", "",
                "Os domínios externos destes sites não foram verificados nesta execução:", "",
                "| Site | Entidade(s) | Motivo |", "|---|---|---|"]
        for s in sorted(nao_lidos, key=lambda s: s["site"]):
            out.append(f"| {_cel(s['site'])} | {_cel(', '.join(s['entidades']))} | {_cel(s['erro'])} |")
        out.append("")
    out += ["## Como ler", "",
            "- **Crítico:** o domínio está livre para registro, vencido ou em exclusão. Qualquer pessoa pode conseguir registrá-lo.",
            "- **Alto:** vence em até 30 dias ou está suspenso.",
            "- **Médio:** vence em até 90 dias.",
            "- **Verificar:** RDAP e DNS discordam; confirme manualmente antes de avisar.",
            "- **Relação externo (alto):** o site oficial carrega script, estilo, iframe ou envia formulário para esse domínio. "
            "Quem registrar o domínio pode alterar o que o cidadão vê ou capturar dados.",
            "- **Relação externo (baixo):** é só um link; o risco é de golpe para quem clicar.", "",
            "Dados obtidos dos serviços públicos RDAP (registro de domínios) e DNS na data acima. "
            "Situação de domínio muda rápido: confirme antes de notificar.", ""]
    with open(caminho, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


MODELO_AVISO = """Assunto: Aviso de segurança sobre domínio(s) ligado(s) ao site {site}

Prezados(as) responsáveis por {entidade},

Em uma verificação automatizada de dados públicos de registro de domínios (RDAP) e DNS, feita em {data}, encontramos o(s) ponto(s) abaixo relacionados ao site {site}:

{itens}

Por que isso importa: quando um domínio vence e fica livre, qualquer pessoa pode registrá-lo. Se o site oficial carrega arquivos desse domínio (scripts, estilos, iframes) ou envia formulários para ele, quem o registrar pode alterar o que os cidadãos veem ou capturar dados. Se for apenas um link, quem clicar pode ser levado a uma página falsa.

Recomendação: renovar o domínio no registrador (para domínios .br, no Registro.br) ou, se ele não é mais usado, remover do site as referências a ele.

Não registramos nenhum desses domínios e não pedimos nenhum pagamento. Podemos enviar os detalhes técnicos da verificação, se for útil.

Atenciosamente,
[seu nome e contato]
"""


def escrever_avisos(pasta, linhas, data):
    os.makedirs(pasta, exist_ok=True)
    por_entidade = defaultdict(list)
    for r in linhas:
        if r["gravidade"] in ("CRITICO", "ALTO"):
            por_entidade[(r["entidade"], r["site"])].append(r)
    for (entidade, site), grupo in sorted(por_entidade.items()):
        itens = []
        for r in sorted(grupo, key=_ordem):
            onde = DESCRICAO_PESO[r.get("peso") or ""]
            ex = f" Exemplo: {r['exemplos'][0]}" if r.get("exemplos") else ""
            itens.append(f"- {r['dominio']}: {r['situacao']} ({onde}).{ex}")
        texto = MODELO_AVISO.format(site=site, entidade=entidade, data=data, itens="\n".join(itens))
        with open(os.path.join(pasta, _slug(f"{entidade}-{site.split('//')[-1]}") + ".txt"), "w", encoding="utf-8") as f:
            f.write(texto)
    return len(por_entidade)


def gerar(pasta_saida, linhas, resumo, nao_lidos=()):
    os.makedirs(pasta_saida, exist_ok=True)
    escrever_csv(os.path.join(pasta_saida, "achados.csv"), linhas)
    escrever_markdown(os.path.join(pasta_saida, "relatorio.md"), linhas, resumo, nao_lidos)
    n = escrever_avisos(os.path.join(pasta_saida, "avisos"), linhas, resumo["gerado_em"][:10])
    return n


def agora_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
