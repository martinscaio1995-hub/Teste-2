import datetime as dt
import unittest

from monitor_dominios.analise import classificar
from monitor_dominios.dominio import PublicSuffixList, eh_restrito, host_de_url
from monitor_dominios.rede import ClienteRDAP
from monitor_dominios.varredura import Varredor

PSL_TESTE = """
// trecho da Public Suffix List
br
com.br
gov.br
sp.gov.br
leg.br
com
*.ck
!www.ck
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

    def test_host_de_url(self):
        self.assertEqual(host_de_url("https://WWW.Exemplo.com.br:8080/x?y"), "www.exemplo.com.br")
        self.assertEqual(host_de_url("http://são.com.br/"), "são.com.br".encode("idna").decode())
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

    def test_livre(self):
        self.assertEqual(classificar({"registrado": False}, {"rcode": 3}, HOJE)[0], "CRITICO")
        self.assertEqual(classificar({"registrado": False}, {"rcode": 0}, HOJE)[0], "VERIFICAR")

    def test_vencimentos(self):
        self.assertEqual(classificar(self.rdap(expiracao="2026-10-01T00:00:00Z"), {"rcode": 0}, HOJE)[0], "CRITICO")
        self.assertEqual(classificar(self.rdap(expiracao="2026-11-01T00:00:00Z"), {"rcode": 0}, HOJE)[0], "ALTO")
        self.assertEqual(classificar(self.rdap(expiracao="2026-12-20T00:00:00Z"), {"rcode": 0}, HOJE)[0], "MEDIO")
        self.assertEqual(classificar(self.rdap(expiracao="2028-01-01T00:00:00Z"), {"rcode": 0}, HOJE)[0], "OK")

    def test_status(self):
        self.assertEqual(classificar(self.rdap(status=["pending delete"]), {"rcode": 0}, HOJE)[0], "CRITICO")
        self.assertEqual(classificar(self.rdap(status=["client hold"], expiracao="2028-01-01T00:00:00Z"), {"rcode": 0}, HOJE)[0], "ALTO")

    def test_sem_data_e_erro(self):
        self.assertEqual(classificar(self.rdap(), {"rcode": 0}, HOJE)[0], "SEM DATA")
        self.assertEqual(classificar({"registrado": None, "erro": "HTTP 500"}, {"rcode": 0}, HOJE)[0], "ERRO")
        self.assertEqual(classificar({"registrado": None, "erro": "HTTP 500"}, {"rcode": 3}, HOJE)[0], "VERIFICAR")


class TesteRDAP(unittest.TestCase):
    def test_interpretar(self):
        dados = {"status": ["Active"], "events": [
            {"eventAction": "registration", "eventDate": "1996-04-24T12:00:00Z"},
            {"eventAction": "expiration", "eventDate": "2034-04-24T12:00:00Z"}]}
        r = ClienteRDAP._interpretar(dados)
        self.assertTrue(r["registrado"])
        self.assertEqual(r["expiracao"], "2034-04-24T12:00:00Z")
        self.assertEqual(r["status"], ["active"])


class TesteVarredura(unittest.TestCase):
    def test_coleta_externos(self):
        html = """<html><head>
        <script src="https://cdn.terceiro.com/lib.js"></script>
        <link rel="stylesheet" href="//estilos.outro.com.br/a.css">
        <link rel="canonical" href="https://canonico.com/">
        </head><body>
        <a href="/interna">interna</a><a href="https://www.cidade.sp.gov.br/x">gov</a>
        <a href="mailto:x@y.com">mail</a><img src="https://img.fotos.com.br/f.png">
        <iframe src="https://mapa.terceiro.com/embed"></iframe>
        <form action="https://formularios.exemplo.com/enviar"></form>
        </body></html>"""
        v = Varredor(PublicSuffixList(PSL_TESTE))
        v.baixar = lambda url: ("https://www.prefeitura.com.br/", html, None)
        res = v.dominios_externos("https://www.prefeitura.com.br/")["externos"]
        self.assertEqual(set(res), {"terceiro.com", "outro.com.br", "canonico.com", "fotos.com.br", "exemplo.com"})
        self.assertEqual(res["terceiro.com"]["peso"], "alto")
        self.assertEqual(res["terceiro.com"]["tags"], ["iframe", "script"])
        self.assertEqual(res["canonico.com"]["peso"], "baixo")
        self.assertEqual(res["fotos.com.br"]["peso"], "medio")
        self.assertEqual(res["exemplo.com"]["peso"], "alto")


if __name__ == "__main__":
    unittest.main()
