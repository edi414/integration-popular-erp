import os
from dotenv import load_dotenv

# Carrega variáveis do arquivo .env se existir
load_dotenv()

class Config:
    # Redis (Suporte para variáveis do Railway)
    REDIS_HOST = os.getenv("REDISHOST", os.getenv("REDIS_HOST", "localhost"))
    REDIS_PORT = int(os.getenv("REDISPORT", os.getenv("REDIS_PORT", 6379)))
    REDIS_DB = int(os.getenv("REDIS_DB", 0))
    REDIS_PASSWORD = os.getenv("REDISPASSWORD", os.getenv("REDIS_PASSWORD", None))

    # API Uniplus
    UNIPLUS_API_BASE_URL = os.getenv("UNIPLUS_API_BASE_URL", "https://localhost:8443")
    UNIPLUS_API_CLIENT_ID = os.getenv("UNIPLUS_API_CLIENT_ID", "uniplus")
    UNIPLUS_API_CLIENT_SECRET = os.getenv("UNIPLUS_API_CLIENT_SECRET", "l4gtr1ck2rspr3ngcl3ent")

    # DB (MariaDB - para uso futuro nos handlers de alteração direta)
    MARIADB_HOST = os.getenv("G3_HOST", "localhost")
    MARIADB_PORT = int(os.getenv("G3_PORT", 3306))
    MARIADB_USER = os.getenv("G3_USER", "root")
    MARIADB_PASSWORD = os.getenv("G3_PASSWORD", "")
    MARIADB_DB = os.getenv("G3_DB", "gtech-gestao")

    # Webhook
    WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://n8n.supermercadopopular.org/webhook-test/e4eee32b-4e62-471b-858b-65cd58484143")

config = Config()
