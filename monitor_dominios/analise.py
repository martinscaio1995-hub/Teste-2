"""Classificação da situação de cada domínio a partir do RDAP e do DNS."""

import datetime as dt

ORDEM_GRAVIDADE = {"CRITICO": 0, "ALTO": 1, "MEDIO": 2, "VERIFICAR": 3, "OK": 4, "SEM DATA": 5, "ERRO": 6}

# Status RDAP (RFC 8056) que indicam que o domínio está a caminho de ser liberado.
STATUS_EXCLUSAO = ("pending delete", "redemption period")
STATUS_SUSPENSO = ("client hold", "server hold", "on hold", "inactive")


def _data(iso):
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def classificar(rdap, dns, hoje=None, dias_alto=30, dias_medio=90):
    """Devolve (gravidade, situação, dias_para_vencer)."""
    hoje = hoje or dt.datetime.now(dt.timezone.utc)
    registrado = rdap.get("registrado")
    rcode = dns.get("rcode") if dns else None

    if rdap.get("erro") and registrado is None:
        if rcode == 3:
            return "VERIFICAR", f"DNS diz que o domínio não existe; RDAP falhou ({rdap['erro']})", None
        return "ERRO", f"falha na consulta RDAP: {rdap['erro']}", None

    if registrado is False:
        if rcode == 3:
            return "CRITICO", "domínio NÃO registrado: está livre para qualquer pessoa registrar", None
        return "VERIFICAR", "RDAP diz não registrado, mas o DNS ainda responde", None

    status = rdap.get("status") or []
    venc = _data(rdap.get("expiracao"))
    dias = (venc - hoje).days if venc else None

    if any(s in st for st in status for s in STATUS_EXCLUSAO):
        return "CRITICO", "domínio em processo de exclusão/resgate (" + ", ".join(status) + ")", dias
    if dias is not None and dias < 0:
        return "CRITICO", f"vencido há {-dias} dia(s)", dias
    if any(s in st for st in status for s in STATUS_SUSPENSO):
        return "ALTO", "domínio suspenso (" + ", ".join(status) + ")", dias
    if dias is not None and dias <= dias_alto:
        return "ALTO", f"vence em {dias} dia(s)", dias
    if dias is not None and dias <= dias_medio:
        return "MEDIO", f"vence em {dias} dia(s)", dias
    if rcode == 3:
        return "VERIFICAR", "registrado, mas sem DNS (pode estar desativado)", dias
    if dias is None:
        return "SEM DATA", "registrado; o RDAP não informa vencimento", None
    return "OK", f"vence em {dias} dia(s)", dias
