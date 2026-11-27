import redis
import json
import time
import logging
import requests
from traceback import format_exc

from app.worker.config import config
from app.worker.utils.logger import get_logger, log_event_to_file
from app.worker.handlers.price_update_handler import handle_price_update
from app.worker.handlers.gtin_update_handler import handle_gtin_update

logger = get_logger()

QUEUE_NAME = "price_updates"

class RedisConsumer:
    def __init__(self):
        self.redis_client = None

    def connect(self):
        """Conecta no Redis."""
        while True:
            try:
                self.redis_client = redis.Redis(
                    host=config.REDIS_HOST,
                    port=config.REDIS_PORT,
                    db=config.REDIS_DB,
                    password=config.REDIS_PASSWORD,
                    decode_responses=True # Facilita pegando string direto
                )
                self.redis_client.ping()
                logger.info(f"Conectado ao Redis com sucesso (Host: {config.REDIS_HOST}:{config.REDIS_PORT}). Escutando fila: '{QUEUE_NAME}'.")
                break
            except redis.exceptions.ConnectionError as e:
                logger.error(f"Não foi possível conectar ao Redis. Retentando em 5 segundos... Erro: {str(e)}")
                time.sleep(5)

    def _notify_webhook(self, payload: dict):
        """Webhook de notificação após processamento."""
        try:
            logger.info(f"Enviando notificação para webhook: {config.WEBHOOK_URL}")
            response = requests.post(config.WEBHOOK_URL, json=payload, timeout=10)
            logger.info(f"Webhook enviado com sucesso. Status: {response.status_code}")
        except Exception as e:
            logger.error(f"Erro ao enviar webhook: {str(e)}")

    def start_consuming(self):
        """Bloqueia esperando eventos usando BRPOP."""
        if not self.redis_client:
            self.connect()

        logger.info("Worker pronto. Aguardando eventos de n8n...")

        while True:
            try:
                # O redis_client garante '0' como infinitivo p/ bloquear e esperar até chegar evento 
                # Retorna tupla: (nome_da_fila, msg) = brpop("price_updates", 0)
                result = self.redis_client.brpop(QUEUE_NAME, timeout=0)
                
                if result:
                    _, data = result
                    
                    try:
                        event = json.loads(data)
                        
                        # Se o n8n ou o script mandarem uma string JSON dentro de outra string, tratamos aqui
                        if isinstance(event, str):
                            event = json.loads(event)
                            
                        logger.info("Evento recebido enfileirado no Redis. Iniciando rotina.")

                        # Simples roteador:
                        event_type = event.get("type")
                        
                        if event_type == "update_price":
                            handle_price_update(event)
                            self._notify_webhook(event)
                        elif event_type == "update_gtin":
                            result = handle_gtin_update(event)
                            self._notify_webhook({**event, **result})
                        else:
                            msg = f"Tipo de evento '{event_type}' desconhecido. Mensagem ignorada."
                            logger.warning(msg)
                            log_event_to_file(msg, level=logging.WARNING)

                    except json.JSONDecodeError:
                        msg = f"Falha ao desserializar JSON recebido do Redis: {data}"
                        logger.error(msg)
                        log_event_to_file(msg, level=logging.ERROR)
            
            except Exception as e:
                logger.error(f"Erro fatal no loop do RedisConsumer:\n{format_exc()}")
                log_event_to_file(f"Erro fatal tentando ler a fila. Reconectando ao Redis... \n{str(e)}", level=logging.ERROR)
                # Pausa antes da reconexão
                time.sleep(3)
                self.connect()
