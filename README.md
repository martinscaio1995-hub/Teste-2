# Monitor de domínios de sites públicos

Ferramenta defensiva que encontra **domínios ligados a sites de órgãos públicos que estão vencendo, vencidos ou livres para registro**, para que os responsáveis possam ser avisados antes que alguém de má-fé registre esses domínios.

## O que ela verifica

Para cada site oficial (municípios e estados do Wikidata, mais uma lista manual):

| Relação | O que é | Por que importa |
|---|---|---|
| Próprio | o domínio do site oficial, quando não é de registro restrito | se vencer, alguém pode registrá-lo e se passar pela entidade |
| Servidor DNS | o domínio dos servidores DNS do site, **inclusive de sites .gov.br** | se vencer, quem registrá-lo controla para onde o endereço oficial aponta |
| Redirecionamento | o domínio para onde o site oficial redireciona | é ele que de fato serve o site |
| Externo (alto) | domínio de scripts, estilos, iframes, formulários ou redirecionamento por `<meta refresh>` na página inicial | quem registrá-lo pode alterar o que o cidadão vê ou capturar dados |
| Externo (médio) | domínio de imagens e mídia | pode trocar o conteúdo exibido |
| Externo (baixo) | domínio só linkado | quem clicar pode cair numa página falsa |
| Plataforma | nome dentro de um serviço de hospedagem (ex.: `portal.azurewebsites.net`) | se deixar de existir, dependendo do provedor, outra pessoa pode recriá-lo |

Os domínios `.gov.br`, `.leg.br`, `.jus.br`, `.mp.br`, `.mil.br`, `.def.br`, `.tc.br` e `.b.br` só podem ser registrados por órgãos e instituições específicas. Por isso, para eles, só os servidores DNS são verificados.

Cada domínio é consultado no **RDAP**, o serviço público dos registros de domínio, que informa se está registrado, quando vence e em que situação está. O resultado é confirmado no **DNS**. Para domínios que só aparecem como link e resolvem normalmente, o RDAP só é consultado com `--completo`.

## Gravidade

| Gravidade | Significado |
|---|---|
| Crítico | livre para registro, vencido ou em processo de exclusão |
| Alto | vence em até 30 dias (`--dias-alto`), está suspenso ou acabou de vencer com renovação automática pendente |
| Médio | vence em até 90 dias (`--dias-medio`) |
| Verificar | as fontes discordam, ou é um nome em plataforma de hospedagem que deixou de existir |
| Sem data / Sem RDAP | registrado, mas sem data de vencimento disponível; conferir no WHOIS do registro |
| Erro | a consulta falhou nesta execução (rode de novo) |
| Não consultado | só aparece como link e resolve no DNS (use `--completo`) |
| OK | vence depois do prazo médio |

## Como usar

Requer só Python 3.10+ (biblioteca padrão).

```bash
# 1. Monta dados/entidades.csv: municípios e estados com site oficial no Wikidata,
#    mais o que estiver em dados/entidades_manuais.csv.
#    O repositório já traz um dados/entidades.csv coletado; rode de novo para atualizar.
python3 -m monitor_dominios coletar

# 2. Verifica tudo e gera o relatório em saida/
python3 -m monitor_dominios verificar

# Teste rápido com as 50 primeiras entidades
python3 -m monitor_dominios verificar --limite 50

# Sem ler as páginas: só domínios próprios e servidores DNS
python3 -m monitor_dominios verificar --sem-externos

# Consultar o vencimento também dos domínios só linkados
python3 -m monitor_dominios verificar --completo

# Regerar o relatório a partir de saida/resultados.json
python3 -m monitor_dominios relatorio
```

A execução pode ser interrompida com Ctrl+C: o cache RDAP é salvo, e a próxima execução reaproveita as consultas das últimas 24 horas (`--cache-horas`).

### Saídas

- `saida/relatorio.md`: resumo, tabelas por gravidade (com a fonte de cada site oficial), consultas com erro, sites que não puderam ser lidos.
- `saida/achados.csv`: todas as ocorrências, para filtrar numa planilha.
- `saida/avisos/*.txt`: um modelo de aviso por entidade com achados críticos ou altos, com a recomendação específica de cada item. A pasta é recriada a cada execução.

### Lista manual

Câmaras municipais, autarquias e outros órgãos que não estão no Wikidata entram em `dados/entidades_manuais.csv`, com as colunas `entidade,tipo,uf,site` (veja `dados/entidades_manuais.exemplo.csv`).

## Sobre os sites oficiais do Wikidata

Muitas prefeituras migraram para `.gov.br`, mas o Wikidata ainda lista o endereço antigo como site oficial. Quando esse endereço antigo está livre ou vencido, o risco é real: Wikidata, Wikipédia e buscadores continuam mandando pessoas para um domínio que qualquer um pode registrar. Além de avisar a entidade, vale corrigir o cadastro: o relatório traz o link de cada item, e qualquer pessoa pode editar o Wikidata.

## Uso responsável

- **Confirme antes de avisar.** A situação de um domínio muda rápido, e o próprio dono pode estar renovando.
- **Avise pelos canais oficiais do órgão:** ouvidoria, setor de TI ou e-mail institucional. Para órgãos do governo federal, existe também o canal de notificação de incidentes do CTIR Gov.
- **Não registre domínios de terceiros para revender a eles.** As regras de disputa (SACI-Adm no `.br`, UDRP nos genéricos) tratam isso como má-fé, e o domínio pode ser retirado de você.
- **O aviso nunca deve pedir pagamento.** Golpistas mandam mensagens parecidas, e a transparência é o que diferencia.
- **A ferramenta é gentil com os servidores:**
  - respeita o `robots.txt`, inclusive nos redirecionamentos;
  - faz uma única leitura da página inicial de cada site;
  - espera entre consultas RDAP (4 s no Registro.br) e pausa todas as consultas a um servidor quando ele pede (HTTP 429, ou 403 no Registro.br);
  - guarda cache de 24 h.

## Testes

```bash
python3 -m unittest discover -s tests
```
