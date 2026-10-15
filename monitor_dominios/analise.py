"""Classificação da situação de cada domínio a partir do RDAP e do DNS."""

import datetime as dt

ORDEM_GRAVIDADE = {"CRITICO": 0, "ALTO": 1, "MEDIO": 2, "VERIFICAR": 3, "SEM DATA": 4, "SEM RDAP": 5,
                   "ERRO": 6, "NAO CONSULTADO": 7, "OK": 8}

# Status RDAP (RFC 8056), comparados por igualdade exata.
STATUS_EXCLUSAO = {"pending delete", "redemption period"}
STATUS_RENOVACAO_AUTOMATICA = {"auto renew period", "pending restore"}
STATUS_SUSPENSO = {"client hold", "server hold", "on hold"}


def _data(iso):
    if not iso:
        return None
    try:
        data = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return data if data.tzinfo else data.replace(tzinfo=dt.timezone.utc)


def classificar(rdap, dns, hoje=None, dias_alto=30, dias_medio=90):
    """Devolve (gravidade, situação, dias_para_vencer) de um domínio registrável."""
    hoje = hoje or dt.datetime.now(dt.timezone.utc)
    dns = dns or {}
    rcode = dns.get("rcode")
    registrado = rdap.get("registrado")

    if rdap.get("sem_rdap"):
        if rcode == 3:
            return "VERIFICAR", ("o domínio não existe no DNS e o registro deste TLD não oferece RDAP: "
                                 "provavelmente livre para registro, confirme no WHOIS do registro"), None
        return "SEM RDAP", "o registro deste TLD não oferece RDAP: confira o vencimento no WHOIS do registro", None

    if registrado is None:
        erro = rdap.get("erro") or "sem resposta"
        if rcode == 3:
            return "VERIFICAR", f"o DNS diz que o domínio não existe; a consulta RDAP falhou ({erro})", None
        return "ERRO", f"falha na consulta RDAP: {erro}", None

    if registrado is False:
        if rcode == 3:
            return "CRITICO", "domínio NÃO registrado: está livre para qualquer pessoa registrar", None
        return "VERIFICAR", "o RDAP diz que não está registrado, mas o DNS não confirma", None

    status = set(rdap.get("status") or [])
    venc = _data(rdap.get("expiracao"))
    dias = (venc - hoje).days if venc else None

    if status & STATUS_EXCLUSAO:
        return "CRITICO", "domínio em processo de exclusão (" + ", ".join(sorted(status)) + ")", dias
    if status & STATUS_RENOVACAO_AUTOMATICA:
        return "ALTO", ("venceu recentemente: o registro renovou automaticamente, mas o pagamento "
                        "pode não ter sido feito (" + ", ".join(sorted(status)) + ")"), None
    if dias is not None and dias < 0:
        return "CRITICO", f"vencido há {-dias} dia(s)", dias
    if status & STATUS_SUSPENSO:
        return "ALTO", "domínio suspenso (" + ", ".join(sorted(status)) + ")", dias
    if dias is not None and dias <= dias_alto:
        return "ALTO", f"vence em {dias} dia(s)", dias
    if dias is not None and dias <= dias_medio:
        return "MEDIO", f"vence em {dias} dia(s)", dias
    if rcode == 3:
        return "VERIFICAR", "registrado, mas sem DNS (pode estar desativado)", dias
    if dias is None:
        return "SEM DATA", "registrado; o RDAP não informa vencimento", None
    return "OK", f"vence em {dias} dia(s)", dias


def classificar_plataforma(dns):
    """Nome dentro de um serviço de hospedagem (ex.: portal.azurewebsites.net)."""
    rcode = (dns or {}).get("rcode")
    if rcode == 3:
        return "VERIFICAR", ("o nome no serviço de hospedagem não existe mais (DNS NXDOMAIN): dependendo do "
                             "provedor, outra pessoa pode recriá-lo e assumir o endereço"), None
    if rcode == 0:
        return "OK", "nome ativo no serviço de hospedagem", None
    return "ERRO", f"falha na consulta DNS: {(dns or {}).get('erro') or f'rcode {rcode}'}", None
