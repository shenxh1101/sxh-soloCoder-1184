import os
from dotenv import load_dotenv

load_dotenv()

basedir = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', f'sqlite:///{os.path.join(basedir, "scheduler.db")}')
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    SCHEDULER_TIMEZONE = os.environ.get('SCHEDULER_TIMEZONE', 'Asia/Shanghai')

    SMTP_HOST = os.environ.get('SMTP_HOST', '')
    SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
    SMTP_USER = os.environ.get('SMTP_USER', '')
    SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '')
    SMTP_FROM = os.environ.get('SMTP_FROM', '')
    SMTP_USE_TLS = os.environ.get('SMTP_USE_TLS', 'true').lower() == 'true'

    MAX_EXECUTION_LOG_LENGTH = int(os.environ.get('MAX_EXECUTION_LOG_LENGTH', '5000'))
    TASK_HISTORY_RETENTION_DAYS = int(os.environ.get('TASK_HISTORY_RETENTION_DAYS', '30'))

    BACKUP_DIR = os.environ.get('BACKUP_DIR', os.path.join(basedir, 'backups'))