import datetime
import io
from flask import Blueprint, request, jsonify, send_file
from models import db, Task, Execution, EnvVar
from scheduler_manager import scheduler_manager, validate_trigger, preview_trigger_times
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

    trigger_type = data.get('trigger_type', 'cron')
    trigger_value = data.get('trigger_value', '')
    try:
        validate_trigger(trigger_type, trigger_value)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    depends_on = data.get('depends_on')
    if depends_on:
        temp_task = Task()
        if temp_task.check_circular_dependency(int(depends_on)):
            return jsonify({'error': '不能选择自身或形成循环依赖'}), 400

    task = Task(
        name=name,
        description=data.get('description', ''),
        trigger_type=trigger_type,
        trigger_value=trigger_value,
        execute_type=data.get('execute_type', 'python'),
        execute_content=data.get('execute_content', ''),
        enabled=data.get('enabled', True),
        paused=data.get('paused', False),
        concurrency_mode=data.get('concurrency_mode', 'skip'),
        retry_count=data.get('retry_count', 0),
        retry_delay_seconds=data.get('retry_delay_seconds', 60),
        notify_email=data.get('notify_email', ''),
        notify_on_success=data.get('notify_on_success', False),
        notify_on_failure=data.get('notify_on_failure', True),
        depends_on=depends_on,
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
    result = task.to_dict()
    result['dependency_chain'] = task.get_dependency_chain()
    result['dependents'] = [{'id': d.id, 'name': d.name} for d in task.get_dependents()]
    result['stats'] = task.get_stats()
    return jsonify(result)


@api_bp.route('/tasks/<int:task_id>/stats', methods=['GET'])
def get_task_stats(task_id):
    task = db.session.get(Task, task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify(task.get_stats())


@api_bp.route('/tasks/<int:task_id>/dependency-view', methods=['GET'])
def get_dependency_view(task_id):
    task = db.session.get(Task, task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404

    tree = task.get_full_dependency_tree()
    for node in tree['upstream'] + tree['downstream']:
        t = db.session.get(Task, node['id'])
        if t:
            node['stats'] = t.get_stats()
            last = Execution.query.filter_by(task_id=node['id']).order_by(
                Execution.start_time.desc()).first()
            node['last_execution'] = last.to_dict() if last else None

    task_stats = task.get_stats()
    last = Execution.query.filter_by(task_id=task.id).order_by(Execution.start_time.desc()).first()

    return jsonify({
        'task': {**task.to_dict(), 'stats': task_stats, 'last_execution': last.to_dict() if last else None},
        'upstream': tree['upstream'],
        'downstream': tree['downstream'],
    })


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

    if 'trigger_type' in data or 'trigger_value' in data:
        tt = data.get('trigger_type', task.trigger_type)
        tv = data.get('trigger_value', task.trigger_value)
        try:
            validate_trigger(tt, tv)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

    if 'depends_on' in data:
        new_dep = data['depends_on']
        if new_dep:
            if task.check_circular_dependency(int(new_dep)):
                return jsonify({'error': '不能选择自身或形成循环依赖'}), 400

    scheduler_manager.remove_job(task_id, task.name)

    for field in ['description', 'trigger_type', 'trigger_value', 'execute_type',
                   'execute_content', 'enabled', 'paused', 'concurrency_mode',
                   'retry_count', 'retry_delay_seconds', 'notify_email',
                   'notify_on_success', 'notify_on_failure', 'depends_on']:
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


@api_bp.route('/tasks/preview-trigger', methods=['GET'])
def preview_trigger():
    trigger_type = request.args.get('type', 'cron')
    trigger_value = request.args.get('value', '').strip()
    count = request.args.get('count', 5, type=int)

    if not trigger_value:
        return jsonify({'error': 'trigger_value is required'}), 400
    if count > 20:
        count = 20

    try:
        validate_trigger(trigger_type, trigger_value)
        times = preview_trigger_times(trigger_type, trigger_value, count)
        return jsonify({'valid': True, 'times': times})
    except ValueError as e:
        return jsonify({'valid': False, 'error': str(e), 'times': []})


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
    status = request.args.get('status', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)

    query = Execution.query.order_by(Execution.start_time.desc())
    if task_id:
        query = query.filter_by(task_id=task_id)
    if status:
        query = query.filter_by(status=status)
    if date_from:
        try:
            dt_from = datetime.datetime.fromisoformat(date_from)
            query = query.filter(Execution.start_time >= dt_from)
        except ValueError:
            pass
    if date_to:
        try:
            dt_to = datetime.datetime.fromisoformat(date_to)
            query = query.filter(Execution.start_time <= dt_to)
        except ValueError:
            pass

    total = query.count()
    executions = query.offset(offset).limit(limit).all()
    return jsonify({
        'total': total,
        'limit': limit,
        'offset': offset,
        'data': [e.to_dict() for e in executions],
    })


@api_bp.route('/executions/<int:execution_id>', methods=['GET'])
def get_execution(execution_id):
    execution = db.session.get(Execution, execution_id)
    if not execution:
        return jsonify({'error': 'Execution not found'}), 404
    result = execution.to_dict()
    if execution.trigger_source_execution_id:
        source_exec = db.session.get(Execution, execution.trigger_source_execution_id)
        if source_exec:
            result['trigger_source'] = {
                'execution_id': source_exec.id,
                'task_name': source_exec.task.name if source_exec.task else '',
            }
    return jsonify(result)


@api_bp.route('/executions/<int:execution_id>/download', methods=['GET'])
def download_execution_log(execution_id):
    execution = db.session.get(Execution, execution_id)
    if not execution:
        return jsonify({'error': 'Execution not found'}), 404
    log_content = execution.output_log or '(empty)'
    task_name = execution.task.name if execution.task else 'unknown'
    filename = f'{task_name}_execution_{execution_id}.log'
    return send_file(
        io.BytesIO(log_content.encode('utf-8')),
        mimetype='text/plain',
        as_attachment=True,
        download_name=filename,
    )


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


@api_bp.route('/backup/preview', methods=['POST'])
def preview_backup():
    data = request.get_json()
    if not data or 'data' not in data:
        filename = (data or {}).get('filename') or request.args.get('filename')
        if filename:
            import os
            from config import Config
            filepath = os.path.join(Config.BACKUP_DIR, filename)
            if not os.path.exists(filepath):
                return jsonify({'error': 'Backup file not found'}), 404
            import json
            with open(filepath, 'r', encoding='utf-8') as f:
                backup_data = json.load(f)
        else:
            return jsonify({'error': 'No backup data or filename provided'}), 400
    else:
        backup_data = data['data']

    existing_tasks = {t.name: t for t in Task.query.all()}

    def _field_diff(existing, new_data):
        field_map = {
            'name': '名称', 'description': '描述', 'trigger_type': '触发类型',
            'trigger_value': '触发值', 'execute_type': '执行类型', 'execute_content': '执行内容',
            'enabled': '启用', 'paused': '暂停', 'retry_count': '重试次数',
            'retry_delay_seconds': '重试延迟', 'notify_email': '通知邮箱',
            'notify_on_success': '成功通知', 'notify_on_failure': '失败通知',
            'depends_on_name': '依赖任务',
        }
        diff_fields = []
        for key, label in field_map.items():
            old_val = None
            new_val = new_data.get(key)
            if key == 'depends_on_name':
                old_val = None
                if existing and existing.depends_on:
                    dep = db.session.get(Task, existing.depends_on)
                    old_val = dep.name if dep else str(existing.depends_on)
            else:
                old_val = getattr(existing, key, None) if existing else None

            old_str = str(old_val) if old_val is not None else '(无)'
            new_str = str(new_val) if new_val is not None else '(无)'
            if old_str != new_str:
                diff_fields.append({'field': label, 'old': old_str, 'new': new_str})
        return diff_fields

    tasks_preview = []
    for t in backup_data.get('tasks', []):
        existing = existing_tasks.get(t['name'])
        action = 'update' if existing else 'create'
        preview_item = {
            'name': t['name'],
            'trigger_type': t.get('trigger_type', 'cron'),
            'trigger_value': t.get('trigger_value', ''),
            'action': action,
            'existing_id': existing.id if existing else None,
        }
        if action == 'update':
            preview_item['diff_fields'] = _field_diff(existing, t)
            preview_item['has_dep_change'] = any(
                d['field'] == '依赖任务' for d in preview_item['diff_fields']
            )
        tasks_preview.append(preview_item)

    env_keys = {ev.key for ev in EnvVar.query.all()}
    env_preview = []
    for ev in backup_data.get('env_vars', []):
        action = 'update' if ev['key'] in env_keys else 'create'
        env_preview.append({'key': ev['key'], 'action': action})

    return jsonify({
        'tasks': tasks_preview,
        'env_vars': env_preview,
        'summary': {
            'tasks_create': sum(1 for t in tasks_preview if t['action'] == 'create'),
            'tasks_update': sum(1 for t in tasks_preview if t['action'] == 'update'),
            'tasks_with_dep_change': sum(1 for t in tasks_preview if t.get('has_dep_change')),
            'env_vars_create': sum(1 for e in env_preview if e['action'] == 'create'),
            'env_vars_update': sum(1 for e in env_preview if e['action'] == 'update'),
        }
    })


@api_bp.route('/backup/restore', methods=['POST'])
def restore_backup():
    data = request.get_json()
    affected_task_ids = []

    if data and 'data' in data:
        try:
            results = restore_from_dict(db, Task, EnvVar, data['data'])
            scheduler_manager.reload_all_jobs()
            affected_task_ids = _get_affected_ids(data['data'])
            return jsonify({
                'message': 'Restore completed',
                'results': results,
                'affected_task_ids': affected_task_ids,
            })
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
            return jsonify({
                'message': 'Restore completed',
                'results': results,
                'affected_task_ids': affected_task_ids,
            })
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 500

    return jsonify({'error': 'No backup data or filename provided'}), 400


def _get_affected_ids(data):
    ids = []
    existing = {t.name: t.id for t in Task.query.all()}
    for t in data.get('tasks', []):
        if t['name'] in existing:
            ids.append(existing[t['name']])
    return ids


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