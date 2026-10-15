import datetime as dt
import os
import tempfile
import unittest

from monitor_dominios import relatorio
from monitor_dominios.analise import classificar, classificar_plataforma
from monitor_dominios.dominio import PublicSuffixList, eh_restrito, host_de_url
from monitor_dominios.rede import ClienteRDAP, segundos_retry_after
from monitor_dominios.varredura import Varredor

PSL_TESTE = """
// ===BEGIN ICANN DOMAINS===
br
com.br
gov.br
sp.gov.br
leg.br
com
net
io
*.ck
!www.ck
// ===END ICANN DOMAINS===
// ===BEGIN PRIVATE DOMAINS===
googleapis.com
azurewebsites.net
*.elb.amazonaws.com
github.io
// ===END PRIVATE DOMAINS===
"""
HOJE = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)


class TesteDominio(unittest.TestCase):
    def setUp(self):
        self.psl = PublicSuffixList(PSL_TESTE)

    def test_registravel(self):
        self.assertEqual(self.psl.registravel("www.prefeitura.exemplo.com.br"), "exemplo.com.br")
        self.assertEqual(self.psl.registravel("cidade.sp.gov.br"), "cidade.sp.gov.br")
        self.assertEqual(self.psl.registravel("a.b.exemplo.com"), "exemplo.com")
        self.assertIsNone(self.psl.registravel("com.br"))

    def test_curinga_e_excecao(self):
        self.assertEqual(self.psl.registravel("a.b.c.ck"), "b.c.ck")
        self.assertEqual(self.psl.registravel("www.ck"), "www.ck")

    def test_secao_privada_nao_vira_dominio_registravel(self):
        self.assertTrue(self.psl.completa)
        self.assertEqual(self.psl.registravel("fonts.googleapis.com"), "googleapis.com")
        self.assertEqual(self.psl.registravel("lb-123.sa-east-1.elb.amazonaws.com"), "amazonaws.com")
        self.assertEqual(self.psl.registravel("prefeitura.github.io"), "github.io")
        self.assertEqual(self.psl.registravel("portal.azurewebsites.net", incluir_privados=True), "portal.azurewebsites.net")

    def test_host_de_url(self):
        self.assertEqual(host_de_url("https://WWW.Exemplo.com.br:8080/x?y"), "www.exemplo.com.br")
        self.assertEqual(host_de_url("http://são.com.br/"), "são.com.br".encode("idna").decode())
        self.assertEqual(host_de_url("www.semesquema.com.br"), "www.semesquema.com.br")
        self.assertIsNone(host_de_url("http://192.168.0.1/"))

    def test_restrito(self):
        self.assertTrue(eh_restrito("cidade.sp.gov.br"))
        self.assertTrue(eh_restrito("camara.leg.br"))
        self.assertFalse(eh_restrito("camaracidade.com.br"))
        self.assertFalse(eh_restrito("algov.br"))


class TesteClassificar(unittest.TestCase):
    def rdap(self, **kw):
        base = {"registrado": True, "expiracao": None, "status": ["active"]}
        base.update(kw)
        return base

    def g(self, rdap, rcode=0):
        return classificar(rdap, {"rcode": rcode}, HOJE)[0]

    def test_livre(self):
        self.assertEqual(self.g({"registrado": False}, 3), "CRITICO")
        self.assertEqual(self.g({"registrado": False}, 0), "VERIFICAR")

    def test_vencimentos(self):
        self.assertEqual(self.g(self.rdap(expiracao="2026-10-01T00:00:00Z")), "CRITICO")
        self.assertEqual(self.g(self.rdap(expiracao="2026-11-01T00:00:00Z")), "ALTO")
        self.assertEqual(self.g(self.rdap(expiracao="2026-12-20T00:00:00Z")), "MEDIO")
        self.assertEqual(self.g(self.rdap(expiracao="2028-01-01T00:00:00Z")), "OK")

    def test_data_sem_fuso(self):
        self.assertEqual(self.g(self.rdap(expiracao="2026-11-01T00:00:00")), "ALTO")

    def test_status(self):
        self.assertEqual(self.g(self.rdap(status=["pending delete"])), "CRITICO")
        self.assertEqual(self.g(self.rdap(status=["client hold"], expiracao="2028-01-01T00:00:00Z")), "ALTO")
        self.assertEqual(self.g(self.rdap(status=["auto renew period"], expiracao="2027-10-01T00:00:00Z")), "ALTO")
        self.assertEqual(self.g(self.rdap(status=["inactive"], expiracao="2028-01-01T00:00:00Z")), "OK")
        self.assertEqual(self.g(self.rdap(status=["inactive"], expiracao="2028-01-01T00:00:00Z"), 3), "VERIFICAR")

    def test_sem_data_sem_rdap_e_erro(self):
        self.assertEqual(self.g(self.rdap()), "SEM DATA")
        self.assertEqual(self.g({"registrado": None, "sem_rdap": True}), "SEM RDAP")
        self.assertEqual(self.g({"registrado": None, "sem_rdap": True}, 3), "VERIFICAR")
        self.assertEqual(self.g({"registrado": None, "erro": "HTTP 500"}), "ERRO")
        self.assertEqual(self.g({"registrado": None, "erro": "HTTP 500"}, 3), "VERIFICAR")

    def test_plataforma(self):
        self.assertEqual(classificar_plataforma({"rcode": 3})[0], "VERIFICAR")
        self.assertEqual(classificar_plataforma({"rcode": 0})[0], "OK")
        self.assertEqual(classificar_plataforma({"rcode": None, "erro": "x"})[0], "ERRO")


class TesteRede(unittest.TestCase):
    def test_interpretar(self):
        dados = {"ldhName": "UOL.COM.BR", "status": ["Active"], "events": [
            {"eventAction": "registration", "eventDate": "1996-04-24T12:00:00Z"},
            {"eventAction": "expiration", "eventDate": "2034-04-24T12:00:00Z"}, "lixo"]}
        r = ClienteRDAP._interpretar(dados, "uol.com.br")
        self.assertTrue(r["registrado"])
        self.assertEqual(r["expiracao"], "2034-04-24T12:00:00Z")
        self.assertEqual(r["status"], ["active"])

    def test_interpretar_outro_dominio(self):
        r = ClienteRDAP._interpretar({"ldhName": "pai.com.br"}, "filho.pai.com.br")
        self.assertIsNone(r["registrado"])

    def test_retry_after(self):
        self.assertEqual(segundos_retry_after("120"), 120)
        self.assertIsNone(segundos_retry_after("abc"))
        self.assertEqual(segundos_retry_after("Wed, 21 Oct 2015 07:28:00 GMT"), 0)

    def test_cache_corrompido(self):
        with tempfile.TemporaryDirectory() as pasta:
            with open(os.path.join(pasta, "cache_rdap.json"), "w") as f:
                f.write("{quebrado")
            c = ClienteRDAP(pasta)
            self.assertEqual(c.cache, {})
            self.assertTrue(os.path.exists(os.path.join(pasta, "cache_rdap.json.corrompido")))


class TesteVarredura(unittest.TestCase):
    HTML = """<html><head>
    <meta http-equiv="refresh" content="5; url=https://novo-portal.com.br/">
    <script src="https://cdn.terceiro.com/lib.js"></script>
    <link rel="stylesheet" href="//estilos.outro.com.br/a.css">
    <link rel="stylesheet" href="https://fonts.googleapis.com/css?family=X">
    <link rel="canonical" href="https://canonico.com/">
    </head><body>
    <a href="/interna">interna</a><a href="https://www.cidade.sp.gov.br/x">gov</a>
    <a href="mailto:x@y.com">mail</a><a href="http://[malformado/">x</a><img src="https://img.fotos.com.br/f.png">
    <iframe src="https://mapa.terceiro.com/embed"></iframe>
    <script src="https://prefeitura.azurewebsites.net/app.js"></script>
    <form action="https://formularios.exemplo.com/enviar"></form>
    </body></html>"""

    def setUp(self):
        self.v = Varredor(PublicSuffixList(PSL_TESTE))
        self.v.baixar = lambda url: ("https://www.prefeitura-nova.com.br/", self.HTML, None, False)
        self.res = self.v.dominios_externos("https://www.prefeitura.com.br/")

    def test_externos(self):
        ext = self.res["externos"]
        self.assertEqual(set(ext), {"terceiro.com", "outro.com.br", "googleapis.com", "canonico.com", "fotos.com.br",
                                    "exemplo.com", "azurewebsites.net", "novo-portal.com.br"})
        self.assertEqual(ext["terceiro.com"]["peso"], "alto")
        self.assertEqual(ext["terceiro.com"]["tags"], ["iframe", "script"])
        self.assertEqual(ext["canonico.com"]["peso"], "baixo")
        self.assertEqual(ext["fotos.com.br"]["peso"], "medio")
        self.assertEqual(ext["novo-portal.com.br"]["tags"], ["meta"])

    def test_plataformas_e_dominio_final(self):
        self.assertEqual(set(self.res["plataformas"]), {"fonts.googleapis.com", "prefeitura.azurewebsites.net"})
        self.assertEqual(self.res["dominio_final"], "prefeitura-nova.com.br")


class TesteRelatorio(unittest.TestCase):
    def linha(self, **kw):
        base = {"gravidade": "CRITICO", "situacao": "x", "dominio": "a.com.br", "relacao": "externo", "peso": "alto",
                "tags": ["script"], "entidade": "Cidade", "uf": "SP", "site": "https://cidade.sp.gov.br/",
                "fonte": "manual", "exemplos": [], "expiracao": ""}
        base.update(kw)
        return base

    def test_recomendacao_por_relacao(self):
        self.assertIn("Remova", relatorio.recomendar(self.linha()))
        self.assertIn("renove", relatorio.recomendar(self.linha(relacao="proprio", fonte="wikidata:Q1")))
        self.assertIn("Wikidata", relatorio.recomendar(self.linha(relacao="proprio", fonte="wikidata:Q1")))
        self.assertIn("servidores DNS", relatorio.recomendar(self.linha(relacao="nameserver")))

    def test_avisos_limpos_e_sem_colisao(self):
        with tempfile.TemporaryDirectory() as pasta:
            with open(os.path.join(pasta, "antigo.txt"), "w") as f:
                f.write("velho")
            linhas = [self.linha(entidade="A" * 80, site="https://um.sp.gov.br/"),
                      self.linha(entidade="A" * 80, site="https://dois.sp.gov.br/"),
                      self.linha(entidade="São Paulo", uf="SP"), self.linha(entidade="São Paulo", uf="RJ")]
            n = relatorio.escrever_avisos(pasta, linhas, "2026-10-15")
            arquivos = os.listdir(pasta)
            self.assertEqual(n, 4)
            self.assertEqual(len(arquivos), 4)
            self.assertNotIn("antigo.txt", arquivos)


if __name__ == "__main__":
    unittest.main()
