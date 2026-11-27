import pytest

from app.worker.services.gtin_service import GtinService
from app.worker.services.gtin_validation import validar_gtin_novo

GTIN_ATUAL = "7896327512233"
GTIN_NOVO = "7891048040065"


class FakeRepo:
    def __init__(self, produto=None, conflitos=None, update_ok=True, erro=None):
        self.produto = produto
        self.conflitos = conflitos or []
        self.update_ok = update_ok
        self.erro = erro
        self.updates = []

    def get_produto_by_gtin(self, gtin):
        if self.erro:
            raise self.erro
        return self.produto if self.produto and self.produto["GTIN"] == gtin else None

    def find_produtos_com_codigo(self, codigo, exceto_id):
        return self.conflitos

    def update_gtin_surgical(self, id_produto, gtin_atual, gtin_novo, id_usuario_edicao):
        self.updates.append((id_produto, gtin_atual, gtin_novo, id_usuario_edicao))
        return self.update_ok


def produto(**over):
    base = {"ID": 100, "GTIN": GTIN_ATUAL, "EAN": GTIN_ATUAL, "BALANCA": 0, "EXCLUIDO": 0}
    return {**base, **over}


def service(repo):
    return GtinService(db=repo, id_usuario_edicao=5)


@pytest.mark.parametrize("codigo", ["7896327512233", "78946347", "070330717527", "00012345678905"])
def test_ean_valido_passa(codigo):
    assert validar_gtin_novo(codigo) is None


@pytest.mark.parametrize("codigo", ["7896327512234", "352273", "O879998949420", "70330717527", "", None, 7896327512233])
def test_ean_invalido_rejeita(codigo):
    assert validar_gtin_novo(codigo) is not None


def test_troca_valida_atualiza_gtin_e_ean_com_usuario():
    repo = FakeRepo(produto())
    r = service(repo).process_gtin_update(GTIN_ATUAL, f" {GTIN_NOVO} ")
    assert r.status == "sucesso"
    assert r.ean_anterior == GTIN_ATUAL
    assert repo.updates == [(100, GTIN_ATUAL, GTIN_NOVO, 5)]


def test_codigo_novo_invalido_nao_consulta_banco():
    repo = FakeRepo(erro=AssertionError("não deveria consultar o banco"))
    r = service(repo).process_gtin_update(GTIN_ATUAL, "7896327512234")
    assert r.status == "rejeitado"
    assert repo.updates == []


@pytest.mark.parametrize(
    "repo, motivo",
    [
        (FakeRepo(None), "Nenhum produto"),
        (FakeRepo(produto(BALANCA=1)), "balança"),
        (FakeRepo(produto(EXCLUIDO=1)), "excluído"),
        (FakeRepo(produto(), conflitos=[{"ID": 200}]), "ID 200"),
    ],
)
def test_pre_checagens_rejeitam_sem_gravar(repo, motivo):
    r = service(repo).process_gtin_update(GTIN_ATUAL, GTIN_NOVO)
    assert r.status == "rejeitado"
    assert motivo in r.motivo
    assert repo.updates == []


def test_codigo_igual_ao_atual_rejeita():
    repo = FakeRepo(produto())
    r = service(repo).process_gtin_update(GTIN_ATUAL, GTIN_ATUAL)
    assert r.status == "rejeitado"
    assert repo.updates == []


def test_rollback_vira_erro():
    r = service(FakeRepo(produto(), update_ok=False)).process_gtin_update(GTIN_ATUAL, GTIN_NOVO)
    assert r.status == "erro"


def test_excecao_de_banco_vira_erro():
    r = service(FakeRepo(erro=ConnectionError("sem VPN"))).process_gtin_update(GTIN_ATUAL, GTIN_NOVO)
    assert r.status == "erro"
    assert "sem VPN" in r.motivo
