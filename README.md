# Monitor de domínios de sites públicos

Ferramenta defensiva que encontra **domínios ligados a sites de órgãos públicos que estão vencendo, vencidos ou livres para registro**, para que os responsáveis possam ser avisados antes que alguém de má-fé registre esses domínios.

## O que ela verifica

1. **Domínios próprios.** O domínio do site oficial de cada entidade, quando ele não é de registro restrito. Domínios `.gov.br`, `.leg.br`, `.jus.br`, `.mp.br`, `.mil.br`, `.def.br`, `.tc.br` e `.b.br` só podem ser registrados por órgãos e instituições específicas, então ficam de fora. Já um site de prefeitura ou câmara em `.com.br`, `.org.br` ou `.net` pode vencer e ser registrado por qualquer pessoa.
2. **Domínios externos.** A ferramenta lê a página inicial de cada site oficial e lista os domínios de terceiros que ela usa:
   - **alto:** scripts, folhas de estilo, iframes e destino de formulários. Quem registrar um desses domínios pode alterar o que o cidadão vê ou capturar dados.
   - **médio:** imagens e mídia.
   - **baixo:** links. Quem clicar pode cair numa página falsa.

Para cada domínio, ela consulta:
- o **RDAP**, serviço público dos registros de domínio, que informa se o domínio está registrado, quando vence e em que situação está;
- o **DNS**, para confirmar.

## Gravidade

| Gravidade | Significado |
|---|---|
| Crítico | livre para registro, vencido ou em processo de exclusão |
| Alto | vence em até 30 dias ou está suspenso |
| Médio | vence em até 90 dias |
| Verificar | RDAP e DNS discordam; confirmar manualmente |

## Como usar

Requer só Python 3.10+ (biblioteca padrão).

```bash
# 1. Monta dados/entidades.csv: municípios e estados com site oficial no Wikidata,
#    mais o que estiver em dados/entidades_manuais.csv
python3 -m monitor_dominios coletar

# 2. Verifica tudo e gera o relatório em saida/
python3 -m monitor_dominios verificar

# Teste rápido com as 50 primeiras entidades
python3 -m monitor_dominios verificar --limite 50

# Só os domínios próprios, sem ler as páginas
python3 -m monitor_dominios verificar --sem-externos

# Regerar o relatório a partir de saida/resultados.json
python3 -m monitor_dominios relatorio
```

### Saídas

- `saida/relatorio.md`: resumo e tabelas por gravidade, sites que não puderam ser lidos e consultas com erro.
- `saida/achados.csv`: todas as ocorrências, para filtrar numa planilha.
- `saida/avisos/*.txt`: um modelo de aviso por entidade com achados críticos ou altos.

### Lista manual

Câmaras municipais, autarquias e outros órgãos que não estão no Wikidata entram em `dados/entidades_manuais.csv`, com as colunas `entidade,tipo,uf,site` (há um exemplo em `dados/entidades_manuais.exemplo.csv`).

## Uso responsável

- **Confirme antes de avisar.** A situação de um domínio muda rápido, e o próprio dono pode estar renovando.
- **Avise pelos canais oficiais do órgão:** ouvidoria, setor de TI ou e-mail institucional. Para órgãos do governo federal, existe também o canal de notificação de incidentes do CTIR Gov.
- **Não registre domínios de terceiros para revender a eles.** As regras de disputa (SACI-Adm no `.br`, UDRP nos genéricos) tratam isso como má-fé, e o domínio pode ser retirado de você.
- O aviso nunca deve pedir pagamento. Golpistas mandam mensagens parecidas, e a transparência é o que diferencia.
- A ferramenta é gentil com os servidores: respeita o `robots.txt`, faz uma única requisição à página inicial de cada site, espera entre consultas RDAP (2 s no Registro.br) e guarda cache de 24 h.

## Testes

```bash
python3 -m unittest discover -s tests
```
