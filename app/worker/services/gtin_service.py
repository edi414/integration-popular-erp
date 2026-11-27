import logging
from dataclasses import dataclass, asdict

from app.worker.config import config
from app.worker.repositories.mariadb_repository import MariaDBRepository
from app.worker.services.gtin_validation import validar_gtin_novo
from app.worker.utils.logger import get_logger, log_event_to_file

logger = get_logger()


@dataclass
class GtinUpdateResult:
    status: str  # "sucesso" | "rejeitado" | "erro"
    motivo: str
    id_produto: int | None = None
    ean_anterior: str | None = None

    @property
    def success(self) -> bool:
        return self.status == "sucesso"

    def to_dict(self) -> dict:
        return asdict(self)


class GtinService:
    """
    Troca de código de barras no G3, seguindo as regras de docs/g3_troca_gtin.md:
    valida EAN -> pré-checagens no banco -> UPDATE cirúrgico de GTIN + EAN.
    """

    def __init__(self, db=None, id_usuario_edicao: int | None = None):
        self.db = db or MariaDBRepository()
        self.id_usuario_edicao = id_usuario_edicao or config.G3_ID_USUARIO_EDICAO

    def process_gtin_update(self, gtin_atual, gtin_novo) -> GtinUpdateResult:
        result = self._process(gtin_atual, gtin_novo)
        msg = f"Troca de GTIN {gtin_atual} -> {gtin_novo}: {result.status.upper()} ({result.motivo})"
        if result.success:
            logger.info(msg)
            log_event_to_file(msg, level=logging.INFO)
        else:
            logger.warning(msg)
            log_event_to_file(msg, level=logging.WARNING if result.status == "rejeitado" else logging.ERROR)
        return result

    def _process(self, gtin_atual, gtin_novo) -> GtinUpdateResult:
        # 1. Validação de formato (obrigatória, antes de tocar no banco)
        if not isinstance(gtin_atual, str) or not gtin_atual.strip():
            return GtinUpdateResult("rejeitado", "Código atual ausente ou vazio")
        motivo = validar_gtin_novo(gtin_novo)
        if motivo:
            return GtinUpdateResult("rejeitado", motivo)
        gtin_atual, gtin_novo = gtin_atual.strip(), gtin_novo.strip()

        try:
            # 2. Pré-checagens (somente leitura)
            produto = self.db.get_produto_by_gtin(gtin_atual)
            if not produto:
                return GtinUpdateResult("rejeitado", f"Nenhum produto com GTIN {gtin_atual}")

            id_produto, ean_anterior = produto["ID"], produto["EAN"]
            if produto["BALANCA"]:
                return GtinUpdateResult("rejeitado", "Produto de balança fica fora do fluxo", id_produto, ean_anterior)
            if produto["EXCLUIDO"]:
                return GtinUpdateResult("rejeitado", "Produto excluído", id_produto, ean_anterior)
            if gtin_atual == gtin_novo:
                return GtinUpdateResult("rejeitado", "Código novo igual ao atual", id_produto, ean_anterior)

            conflitos = self.db.find_produtos_com_codigo(gtin_novo, exceto_id=id_produto)
            if conflitos:
                ids = ", ".join(str(c["ID"]) for c in conflitos)
                return GtinUpdateResult("rejeitado", f"Código novo já usado pelo(s) produto(s) ID {ids}", id_produto, ean_anterior)

            # 3. Gravação
            ok = self.db.update_gtin_surgical(id_produto, gtin_atual, gtin_novo, self.id_usuario_edicao)
            if not ok:
                return GtinUpdateResult("erro", "UPDATE não afetou exatamente 1 linha (ROLLBACK)", id_produto, ean_anterior)
            return GtinUpdateResult("sucesso", "GTIN e EAN atualizados", id_produto, ean_anterior)

        except Exception as e:
            logger.exception("Erro ao trocar GTIN")
            return GtinUpdateResult("erro", f"Erro de banco: {e}")
