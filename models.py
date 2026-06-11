import datetime
import json
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

UTC = datetime.timezone.utc


def _now():
    return datetime.datetime.now(UTC)


class Task(db.Model):
    __tablename__ = 'tasks'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False, unique=True)
    description = db.Column(db.Text, default='')

    trigger_type = db.Column(db.String(20), nullable=False, default='cron')
    trigger_value = db.Column(db.String(200), nullable=False)

    execute_type = db.Column(db.String(20), nullable=False, default='python')
    execute_content = db.Column(db.Text, nullable=False)

    enabled = db.Column(db.Boolean, default=True)
    paused = db.Column(db.Boolean, default=False)

    retry_count = db.Column(db.Integer, default=0)
    retry_delay_seconds = db.Column(db.Integer, default=60)

    notify_email = db.Column(db.String(500), default='')
    notify_on_success = db.Column(db.Boolean, default=False)
    notify_on_failure = db.Column(db.Boolean, default=True)

    depends_on = db.Column(db.Integer, nullable=True)

    env_vars = db.Column(db.Text, default='{}')

    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    executions = db.relationship('Execution', backref='task', lazy='dynamic', cascade='all, delete-orphan')

    def get_env_vars(self):
        try:
            return json.loads(self.env_vars) if self.env_vars else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def set_env_vars(self, env_dict):
        self.env_vars = json.dumps(env_dict or {})

    def get_dependency_chain(self):
        chain = []
        visited = set()
        current = self
        while current and current.depends_on and current.depends_on not in visited:
            visited.add(current.id)
            dep = db.session.get(Task, current.depends_on)
            if dep:
                chain.insert(0, {'id': dep.id, 'name': dep.name})
                current = dep
            else:
                break
        return chain

    def get_dependents(self):
        return Task.query.filter_by(depends_on=self.id).all()

    def check_circular_dependency(self, target_depends_on):
        if not target_depends_on:
            return False
        if self.id and target_depends_on == self.id:
            return True
        if not self.id:
            return False
        visited = {self.id}
        current_id = target_depends_on
        while current_id:
            if current_id in visited:
                return True
            visited.add(current_id)
            dep_task = db.session.get(Task, current_id)
            if dep_task and dep_task.depends_on:
                current_id = dep_task.depends_on
            else:
                break
        return False

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'trigger_type': self.trigger_type,
            'trigger_value': self.trigger_value,
            'execute_type': self.execute_type,
            'execute_content': self.execute_content,
            'enabled': self.enabled,
            'paused': self.paused,
            'retry_count': self.retry_count,
            'retry_delay_seconds': self.retry_delay_seconds,
            'notify_email': self.notify_email,
            'notify_on_success': self.notify_on_success,
            'notify_on_failure': self.notify_on_failure,
            'depends_on': self.depends_on,
            'env_vars': self.get_env_vars(),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class Execution(db.Model):
    __tablename__ = 'executions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    task_id = db.Column(db.Integer, db.ForeignKey('tasks.id'), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), nullable=False, default='running')
    trigger_type = db.Column(db.String(20), default='scheduled')
    trigger_source_execution_id = db.Column(db.Integer, nullable=True)
    output_log = db.Column(db.Text, default='')
    retry_attempt = db.Column(db.Integer, default=0)

    def to_dict(self):
        return {
            'id': self.id,
            'task_id': self.task_id,
            'task_name': self.task.name if self.task else '',
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'status': self.status,
            'trigger_type': self.trigger_type,
            'trigger_source_execution_id': self.trigger_source_execution_id,
            'output_log': self.output_log,
            'retry_attempt': self.retry_attempt,
        }


class EnvVar(db.Model):
    __tablename__ = 'env_vars'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    key = db.Column(db.String(200), nullable=False, unique=True)
    value = db.Column(db.Text, default='')
    description = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    def to_dict(self):
        return {
            'id': self.id,
            'key': self.key,
            'value': self.value,
            'description': self.description,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }