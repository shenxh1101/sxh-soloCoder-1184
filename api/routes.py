import datetime
from flask import Blueprint, request, jsonify
from models import db, Task, Execution, EnvVar
from scheduler_manager import scheduler_manager
from services.backup import export_tasks_to_dict, backup_to_file, restore_from_dict, restore_from_file, get_backup_files

api_bp = Blueprint('api', __name__, url_prefix='/api')


@api_bp.route('/tasks', methods=['GET'])
def list_tasks():
    tasks = Task.query.order_by(Task.created_at.desc()).all()
    return jsonify([t.to_dict() for t in tasks])


@api_bp.route('/tasks', methods=['POST'])
def create_task():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Task name is required'}), 400
    if Task.query.filter_by(name=name).first():
        return jsonify({'error': f'Task "{name}" already exists'}), 409

    task = Task(
        name=name,
        description=data.get('description', ''),
        trigger_type=data.get('trigger_type', 'cron'),
        trigger_value=data.get('trigger_value', ''),
        execute_type=data.get('execute_type', 'python'),
        execute_content=data.get('execute_content', ''),
        enabled=data.get('enabled', True),
        paused=data.get('paused', False),
        retry_count=data.get('retry_count', 0),
        retry_delay_seconds=data.get('retry_delay_seconds', 60),
        notify_email=data.get('notify_email', ''),
        notify_on_success=data.get('notify_on_success', False),
        notify_on_failure=data.get('notify_on_failure', True),
        depends_on=data.get('depends_on'),
    )
    task.set_env_vars(data.get('env_vars', {}))
    db.session.add(task)
    db.session.commit()

    if task.enabled:
        try:
            scheduler_manager.add_job(task)
        except Exception as e:
            return jsonify({'error': f'Task created but scheduling failed: {str(e)}', 'task': task.to_dict()}), 201

    return jsonify(task.to_dict()), 201


@api_bp.route('/tasks/<int:task_id>', methods=['GET'])
def get_task(task_id):
    task = db.session.get(Task, task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify(task.to_dict())


@api_bp.route('/tasks/<int:task_id>', methods=['PUT'])
def update_task(task_id):
    task = db.session.get(Task, task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    if 'name' in data and data['name'] != task.name:
        if Task.query.filter_by(name=data['name']).first():
            return jsonify({'error': f'Task name "{data["name"]}" already exists'}), 409
        task.name = data['name']

    scheduler_manager.remove_job(task_id, task.name)

    for field in ['description', 'trigger_type', 'trigger_value', 'execute_type',
                   'execute_content', 'enabled', 'paused', 'retry_count',
                   'retry_delay_seconds', 'notify_email', 'notify_on_success',
                   'notify_on_failure', 'depends_on']:
        if field in data:
            setattr(task, field, data[field])

    if 'env_vars' in data:
        task.set_env_vars(data['env_vars'])

    task.updated_at = datetime.datetime.now(datetime.UTC)
    db.session.commit()

    if task.enabled:
        scheduler_manager.add_job(task)

    return jsonify(task.to_dict())


@api_bp.route('/tasks/<int:task_id>', methods=['DELETE'])
def delete_task(task_id):
    task = db.session.get(Task, task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404

    scheduler_manager.remove_job(task.id, task.name)
    db.session.delete(task)
    db.session.commit()
    return jsonify({'message': 'Task deleted'})


@api_bp.route('/tasks/<int:task_id>/execute', methods=['POST'])
def execute_task_now(task_id):
    task = db.session.get(Task, task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    try:
        scheduler_manager.run_job_now(task_id)
        return jsonify({'message': f'Task "{task.name}" triggered for immediate execution'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@api_bp.route('/tasks/<int:task_id>/pause', methods=['POST'])
def pause_task(task_id):
    task = db.session.get(Task, task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    task.paused = True
    db.session.commit()
    scheduler_manager.pause_job(task)
    return jsonify({'message': f'Task "{task.name}" paused'})


@api_bp.route('/tasks/<int:task_id>/resume', methods=['POST'])
def resume_task(task_id):
    task = db.session.get(Task, task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    task.paused = False
    db.session.commit()
    scheduler_manager.resume_job(task)
    return jsonify({'message': f'Task "{task.name}" resumed'})


@api_bp.route('/executions', methods=['GET'])
def list_executions():
    task_id = request.args.get('task_id', type=int)
    limit = request.args.get('limit', 50, type=int)
    query = Execution.query.order_by(Execution.start_time.desc())
    if task_id:
        query = query.filter_by(task_id=task_id)
    executions = query.limit(limit).all()
    return jsonify([e.to_dict() for e in executions])


@api_bp.route('/executions/<int:execution_id>', methods=['GET'])
def get_execution(execution_id):
    execution = db.session.get(Execution, execution_id)
    if not execution:
        return jsonify({'error': 'Execution not found'}), 404
    return jsonify(execution.to_dict())


@api_bp.route('/env-vars', methods=['GET'])
def list_env_vars():
    env_vars = EnvVar.query.order_by(EnvVar.key).all()
    return jsonify([ev.to_dict() for ev in env_vars])


@api_bp.route('/env-vars', methods=['POST'])
def create_env_var():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    key = data.get('key', '').strip()
    if not key:
        return jsonify({'error': 'Key is required'}), 400
    if EnvVar.query.filter_by(key=key).first():
        return jsonify({'error': f'Env var "{key}" already exists'}), 409

    env_var = EnvVar(
        key=key,
        value=data.get('value', ''),
        description=data.get('description', ''),
    )
    db.session.add(env_var)
    db.session.commit()
    return jsonify(env_var.to_dict()), 201


@api_bp.route('/env-vars/<int:env_var_id>', methods=['PUT'])
def update_env_var(env_var_id):
    env_var = db.session.get(EnvVar, env_var_id)
    if not env_var:
        return jsonify({'error': 'Env var not found'}), 404

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    if 'key' in data and data['key'] != env_var.key:
        if EnvVar.query.filter_by(key=data['key']).first():
            return jsonify({'error': f'Env var "{data["key"]}" already exists'}), 409
        env_var.key = data['key']

    if 'value' in data:
        env_var.value = data['value']
    if 'description' in data:
        env_var.description = data['description']
    env_var.updated_at = datetime.datetime.now(datetime.UTC)
    db.session.commit()
    return jsonify(env_var.to_dict())


@api_bp.route('/env-vars/<int:env_var_id>', methods=['DELETE'])
def delete_env_var(env_var_id):
    env_var = db.session.get(EnvVar, env_var_id)
    if not env_var:
        return jsonify({'error': 'Env var not found'}), 404
    db.session.delete(env_var)
    db.session.commit()
    return jsonify({'message': 'Env var deleted'})


@api_bp.route('/backup', methods=['POST'])
def create_backup():
    try:
        filepath = backup_to_file(db, Task, EnvVar)
        return jsonify({'message': 'Backup created', 'filepath': filepath})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@api_bp.route('/backup/files', methods=['GET'])
def list_backup_files():
    files = get_backup_files()
    return jsonify(files)


@api_bp.route('/backup/restore', methods=['POST'])
def restore_backup():
    data = request.get_json()
    if data and 'data' in data:
        try:
            results = restore_from_dict(db, Task, EnvVar, data['data'])
            scheduler_manager.reload_all_jobs()
            return jsonify({'message': 'Restore completed', 'results': results})
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 500

    filename = (data or {}).get('filename') or request.args.get('filename')
    if filename:
        import os
        from config import Config
        filepath = os.path.join(Config.BACKUP_DIR, filename)
        if not os.path.exists(filepath):
            return jsonify({'error': 'Backup file not found'}), 404
        try:
            results = restore_from_file(db, Task, EnvVar, filepath)
            scheduler_manager.reload_all_jobs()
            return jsonify({'message': 'Restore completed', 'results': results})
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 500

    return jsonify({'error': 'No backup data or filename provided'}), 400


@api_bp.route('/backup/export', methods=['GET'])
def export_backup():
    data = export_tasks_to_dict(db, Task, EnvVar)
    return jsonify(data)


@api_bp.route('/scheduler/status', methods=['GET'])
def scheduler_status():
    status = scheduler_manager.get_scheduler_status()
    return jsonify(status)


@api_bp.route('/scheduler/reload', methods=['POST'])
def reload_scheduler():
    scheduler_manager.reload_all_jobs()
    return jsonify({'message': 'Scheduler reloaded'})