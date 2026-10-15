"""Geração do relatório (Markdown e CSV) e dos modelos de aviso por entidade."""

import csv
import datetime as dt
import glob
import hashlib
import os
import re
import unicodedata
from collections import Counter, defaultdict

from .analise import ORDEM_GRAVIDADE

COLUNAS = ["gravidade", "situacao", "dias", "expiracao", "dominio", "relacao", "peso", "tags",
           "entidade", "tipo", "uf", "site", "fonte", "exemplos", "status", "dns_rcode", "consultado_em", "rdap_fonte"]
NOMES_GRAVIDADE = {"CRITICO": "Crítico", "ALTO": "Alto", "MEDIO": "Médio", "VERIFICAR": "Verificar",
                   "SEM DATA": "Sem data de vencimento", "SEM RDAP": "Sem RDAP", "ERRO": "Erro",
                   "NAO CONSULTADO": "Não consultado", "OK": "OK"}
ORDEM_PESO = {"alto": 0, "medio": 1, "baixo": 2}
SECOES_DETALHADAS = ("CRITICO", "ALTO", "MEDIO", "VERIFICAR", "SEM DATA", "SEM RDAP")


def descrever_relacao(r):
    rel, peso, tags = r.get("relacao"), r.get("peso"), ", ".join(r.get("tags") or [])
    if rel == "proprio":
        return "domínio do próprio site oficial"
    if rel == "redirecionamento":
        return "o site oficial redireciona para ele"
    if rel == "nameserver":
        return "servidor DNS do site oficial"
    if rel == "plataforma":
        return f"nome em serviço de hospedagem usado pelo site ({tags})"
    if peso == "alto":
        return f"o site carrega código ou conteúdo dele ({tags})"
    if peso == "medio":
        return f"o site exibe imagem ou mídia dele ({tags})"
    return "o site tem um link para ele"


def recomendar(r):
    """Recomendação específica para a relação e a situação de cada ocorrência."""
    rel, grav = r.get("relacao"), r.get("gravidade")
    livre = grav == "CRITICO"
    if rel == "proprio":
        texto = ("Se o domínio ainda é da entidade, renove-o no registrador (para .br, no Registro.br). "
                 "Se a entidade não o usa mais, ele continua aparecendo como site oficial")
        texto += " no Wikidata e, por consequência, na Wikipédia e em buscadores" if str(r.get("fonte", "")).startswith("wikidata") else ""
        return texto + ": corrija esses cadastros e avalie mantê-lo registrado para que ninguém se passe pela entidade."
    if rel == "redirecionamento":
        return "O site oficial depende desse domínio: renove-o ou troque o redirecionamento."
    if rel == "nameserver":
        return ("O endereço do site oficial depende dos servidores DNS hospedados nesse domínio: confirme a renovação "
                "com o provedor de DNS ou troque os servidores DNS do domínio.")
    if rel == "plataforma":
        return "Remova ou corrija a referência a esse nome; confirme com o provedor se ele pode ser recriado por terceiros."
    if livre:
        return "Remova ou substitua imediatamente a referência a esse domínio no site."
    return "Avise o fornecedor responsável pelo domínio ou substitua a referência no site."


def _ordem(r):
    return (ORDEM_GRAVIDADE.get(r["gravidade"], 99), ORDEM_PESO.get(r.get("peso") or "", 3), r["dominio"], r["entidade"])


def _cel(v):
    return (str(v if v is not None else "").replace("|", "\\|").replace("\n", " ")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _slug(texto, limite):
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    texto = re.sub(r"[^\w\s-]", "", texto).strip().lower()
    return re.sub(r"[\s_]+", "-", texto)[:limite].strip("-")


def _link_fonte(fonte):
    if str(fonte).startswith("wikidata:"):
        q = fonte.split(":", 1)[1]
        return f"[{q}](https://www.wikidata.org/wiki/{q})"
    return _cel(fonte)


def escrever_csv(caminho, linhas):
    with open(caminho, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUNAS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(linhas, key=_ordem):
            w.writerow({**r, "tags": " ".join(r.get("tags") or []), "exemplos": " ".join(r.get("exemplos") or []),
                        "status": " ".join(r.get("status") or [])})


def escrever_markdown(caminho, dados):
    linhas, resumo = dados["linhas"], dados["resumo"]
    dias_alto, dias_medio = resumo.get("dias_alto", 30), resumo.get("dias_medio", 90)
    cont = Counter(r["gravidade"] for r in linhas)
    distintos = defaultdict(set)
    for r in linhas:
        distintos[r["gravidade"]].add(r["dominio"])
    out = [f"# Domínios ligados a sites públicos — situação em {resumo['gerado_em'][:10]}", "", "## Resumo", "",
           f"- Entidades analisadas: {resumo['entidades']}",
           f"- Páginas iniciais lidas: {resumo['sites_lidos']} de {resumo['sites_tentados']}",
           f"- Domínios registráveis verificados: {resumo['dominios_verificados']} "
           f"({resumo.get('dominios_alvo', 0)} do próprio site, redirecionamento ou servidor DNS; "
           f"{resumo.get('dominios_externos', 0)} externos), dos quais {resumo.get('dominios_com_rdap', 0)} consultados no RDAP",
           f"- Nomes em serviços de hospedagem verificados no DNS: {resumo.get('plataformas', 0)}",
           f"- Domínios de site de registro restrito (.gov.br, .leg.br, .jus.br...): {resumo.get('dominios_restritos', 0)} "
           "(não podem ser registrados pelo público; foram verificados só pelos seus servidores DNS)", ""]
    if resumo.get("psl") == "mínima":
        out += ["> **Atenção:** a Public Suffix List não pôde ser obtida e foi usada uma lista mínima. "
                "Domínios fora de .com/.net/.org e dos .br comuns podem ter sido calculados errado.", ""]
    out += ["| Gravidade | Ocorrências | Domínios distintos |", "|---|---|---|"]
    for g in sorted(cont, key=lambda g: ORDEM_GRAVIDADE.get(g, 99)):
        out.append(f"| {NOMES_GRAVIDADE.get(g, g)} | {cont[g]} | {len(distintos[g])} |")
    out.append("")
    for g in SECOES_DETALHADAS:
        grupo = sorted((r for r in linhas if r["gravidade"] == g), key=_ordem)
        if not grupo:
            continue
        out += [f"## {NOMES_GRAVIDADE[g]}", "",
                "| Domínio | Situação | Vencimento | Relação com o site | Entidade | UF | Site | Fonte | Exemplo |",
                "|---|---|---|---|---|---|---|---|---|"]
        for r in grupo:
            ex = (r.get("exemplos") or [""])[0]
            out.append(f"| {_cel(r['dominio'])} | {_cel(r['situacao'])} | {_cel(r.get('expiracao'))} | "
                       f"{_cel(descrever_relacao(r))} | {_cel(r['entidade'])} | {_cel(r.get('uf'))} | {_cel(r['site'])} | "
                       f"{_link_fonte(r.get('fonte', ''))} | {_cel(ex)} |")
        out.append("")
    erros = defaultdict(list)
    for r in linhas:
        if r["gravidade"] == "ERRO":
            erros[(r["dominio"], r["situacao"])].append(r["entidade"])
    if erros:
        out += [f"## Consultas com erro ({len(erros)} domínio(s))", "",
                "Falhas temporárias (limite de consultas, rede): rode de novo para completar.", ""]
        for (d, sit), ents in sorted(erros.items()):
            out.append(f"- {_cel(d)}: {_cel(sit)} — {_cel(', '.join(sorted(set(ents))[:5]))}")
        out.append("")
    nao = cont.get("NAO CONSULTADO", 0)
    if nao:
        out += [f"## Não consultados no RDAP ({len(distintos['NAO CONSULTADO'])} domínio(s))", "",
                "Domínios que só aparecem como link nas páginas e resolvem normalmente no DNS. "
                "Para verificar o vencimento deles também, rode com `--completo`.", ""]
    for chave, titulo, explicacao in (
            ("sites_nao_lidos", "Sites cuja página inicial não pôde ser lida", "Os domínios externos destes sites não foram verificados:"),
            ("leituras_parciais", "Leituras parciais", "Só os primeiros 3 MB da página foram lidos:")):
        itens = dados.get(chave) or []
        if itens:
            out += [f"## {titulo} ({len(itens)})", "", explicacao, "", "| Site | Entidade(s) | Motivo |", "|---|---|---|"]
            for s in sorted(itens, key=lambda s: s["site"]):
                out.append(f"| {_cel(s['site'])} | {_cel(', '.join(s['entidades']))} | {_cel(s['erro'])} |")
            out.append("")
    if dados.get("sem_dominio"):
        out += [f"## Entidades sem domínio válido ({len(dados['sem_dominio'])})", ""]
        out += [f"- {_cel(s['entidade'])}: {_cel(s['site'])}" for s in dados["sem_dominio"]]
        out.append("")
    out += ["## Como ler", "",
            "- **Crítico:** o domínio está livre para registro, vencido ou em processo de exclusão. Qualquer pessoa pode conseguir registrá-lo.",
            f"- **Alto:** vence em até {dias_alto} dias, está suspenso ou acabou de vencer (renovação automática pendente).",
            f"- **Médio:** vence em até {dias_medio} dias.",
            "- **Verificar:** as fontes discordam ou o caso precisa de conferência manual (inclui nomes em serviços de hospedagem que deixaram de existir).",
            "- **Sem data de vencimento / Sem RDAP:** registrado, mas o vencimento não pôde ser obtido; confira no WHOIS do registro.",
            "- **Erro:** a consulta falhou nesta execução. **Não consultado:** só aparece como link e resolve no DNS. **OK:** vence depois do prazo médio.",
            "- **Fonte:** de onde veio o site oficial. Para itens do Wikidata, o link leva ao cadastro, que qualquer pessoa pode corrigir.", "",
            "Dados obtidos dos serviços públicos RDAP (registro de domínios) e DNS na data acima. "
            "A situação de um domínio muda rápido: confirme antes de notificar.", ""]
    with open(caminho, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


MODELO_AVISO = """Assunto: Aviso de segurança sobre domínio(s) ligado(s) a {entidade}

Prezados(as) responsáveis por {entidade},

Em uma verificação automatizada de dados públicos de registro de domínios (RDAP) e DNS, feita em {data}, encontramos o(s) ponto(s) abaixo relacionado(s) ao endereço {site}:

{itens}

Por que isso importa: quando um domínio vence e fica livre, qualquer pessoa pode registrá-lo e usá-lo para se passar pela entidade, alterar o que os cidadãos veem ou capturar dados.

Não registramos nenhum desses domínios e não pedimos nenhum pagamento. Os dados podem ser conferidos por qualquer pessoa no site do registro de domínios (para .br, registro.br). Podemos enviar os detalhes técnicos da verificação, se for útil.

Atenciosamente,
[seu nome e contato]
"""


def escrever_avisos(pasta, linhas, data):
    os.makedirs(pasta, exist_ok=True)
    for antigo in glob.glob(os.path.join(pasta, "*.txt")):
        os.remove(antigo)
    por_entidade = defaultdict(list)
    for r in linhas:
        if r["gravidade"] in ("CRITICO", "ALTO"):
            por_entidade[(r["entidade"], r.get("uf", ""), r["site"])].append(r)
    usados = set()
    for (entidade, uf, site), grupo in sorted(por_entidade.items()):
        itens, vistos = [], set()
        for r in sorted(grupo, key=_ordem):
            if (r["dominio"], r["relacao"]) in vistos:
                continue
            vistos.add((r["dominio"], r["relacao"]))
            venc = f" Vencimento registrado: {r['expiracao']}." if r.get("expiracao") else ""
            itens.append(f"- {r['dominio']} ({descrever_relacao(r)}): {r['situacao']}.{venc}\n  O que fazer: {recomendar(r)}")
        texto = MODELO_AVISO.format(site=site, entidade=entidade, data=data, itens="\n".join(itens))
        sufixo = hashlib.sha1(f"{entidade}|{uf}|{site}".encode("utf-8")).hexdigest()[:6]
        nome = f"{_slug(entidade, 50) or 'entidade'}-{_slug(site.split('//')[-1], 40)}-{sufixo}"
        while nome in usados:
            nome += "x"
        usados.add(nome)
        with open(os.path.join(pasta, nome + ".txt"), "w", encoding="utf-8") as f:
            f.write(texto)
    return len(usados)


def gerar(pasta_saida, dados):
    os.makedirs(pasta_saida, exist_ok=True)
    escrever_csv(os.path.join(pasta_saida, "achados.csv"), dados["linhas"])
    escrever_markdown(os.path.join(pasta_saida, "relatorio.md"), dados)
    return escrever_avisos(os.path.join(pasta_saida, "avisos"), dados["linhas"], dados["resumo"]["gerado_em"][:10])


def agora_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
