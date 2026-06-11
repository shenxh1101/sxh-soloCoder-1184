import os
import datetime
from datetime import UTC
from flask import Flask
from config import Config
from models import db, Task, Execution, EnvVar
from scheduler_manager import scheduler_manager
from api.routes import api_bp
from web.routes import web_bp


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)

    with app.app_context():
        db.create_all()
        _cleanup_old_history(app)

    app.register_blueprint(api_bp)
    app.register_blueprint(web_bp)

    scheduler_manager.init_app(app)

    with app.app_context():
        tasks = Task.query.filter_by(enabled=True).all()
        for task in tasks:
            try:
                scheduler_manager.add_job(task)
            except Exception:
                pass

    return app


def _cleanup_old_history(app):
    retention_days = app.config.get('TASK_HISTORY_RETENTION_DAYS', 30)
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=retention_days)
    Execution.query.filter(Execution.start_time < cutoff).delete()
    db.session.commit()


app = create_app()

if __name__ == '__main__':
    os.makedirs(Config.BACKUP_DIR, exist_ok=True)
    app.run(host='0.0.0.0', port=5000, debug=True)