import json
from app.worker.services.gtin_service import GtinService
from app.worker.utils.logger import get_logger, log_event_to_file

logger = get_logger()

# Inicializa o serviço uma vez para reutilizar a conexão com o MariaDB
gtin_service = GtinService()


def handle_gtin_update(event_data: dict) -> dict:
    """
    Interpreta o evento 'update_gtin' consumido do Redis.
    Payload esperado: { "type": "update_gtin", "gtin_atual": "...", "gtin_novo": "..." }
    Retorna o resultado (status, motivo, id_produto, ean_anterior) para o webhook.
    """
    logger.info(f"Troca de GTIN recebida na fila! Payload: {json.dumps(event_data, indent=2)}")
    log_event_to_file(f"Iniciando troca de GTIN: {event_data.get('gtin_atual')} -> {event_data.get('gtin_novo')}")

    result = gtin_service.process_gtin_update(event_data.get("gtin_atual"), event_data.get("gtin_novo"))
    return result.to_dict()
