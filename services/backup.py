import json
import os
import datetime
from config import Config


def export_tasks_to_dict(db, Task, EnvVar):
    tasks = Task.query.all()
    task_list = []
    for task in tasks:
        task_list.append({
            'name': task.name,
            'description': task.description,
            'trigger_type': task.trigger_type,
            'trigger_value': task.trigger_value,
            'execute_type': task.execute_type,
            'execute_content': task.execute_content,
            'enabled': task.enabled,
            'paused': task.paused,
            'retry_count': task.retry_count,
            'retry_delay_seconds': task.retry_delay_seconds,
            'notify_email': task.notify_email,
            'notify_on_success': task.notify_on_success,
            'notify_on_failure': task.notify_on_failure,
            'depends_on_name': _resolve_dependency_name(db, Task, task.depends_on),
            'env_vars': task.get_env_vars(),
        })

    env_vars = EnvVar.query.all()
    env_list = []
    for ev in env_vars:
        env_list.append({
            'key': ev.key,
            'value': ev.value,
            'description': ev.description,
        })

    return {
        'version': '1.0',
        'exported_at': datetime.datetime.now(datetime.UTC).isoformat(),
        'tasks': task_list,
        'env_vars': env_list,
    }


def _resolve_dependency_name(db, Task, depends_on_id):
    if not depends_on_id:
        return None
    dep_task = db.session.get(Task, depends_on_id)
    return dep_task.name if dep_task else None


def backup_to_file(db, Task, EnvVar, filename=None):
    backup_dir = Config.BACKUP_DIR
    os.makedirs(backup_dir, exist_ok=True)
    if filename is None:
        filename = f'backup_{datetime.datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    filepath = os.path.join(backup_dir, filename)
    data = export_tasks_to_dict(db, Task, EnvVar)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return filepath


def restore_from_dict(db, Task, EnvVar, data):
    results = {'tasks_created': 0, 'tasks_updated': 0, 'env_vars_created': 0, 'env_vars_updated': 0}

    for ev_data in data.get('env_vars', []):
        existing = EnvVar.query.filter_by(key=ev_data['key']).first()
        if existing:
            existing.value = ev_data.get('value', '')
            existing.description = ev_data.get('description', '')
            results['env_vars_updated'] += 1
        else:
            new_ev = EnvVar(
                key=ev_data['key'],
                value=ev_data.get('value', ''),
                description=ev_data.get('description', ''),
            )
            db.session.add(new_ev)
            results['env_vars_created'] += 1
    db.session.flush()

    task_name_to_id = {}

    for task_data in data.get('tasks', []):
        existing = Task.query.filter_by(name=task_data['name']).first()
        if existing:
            existing.description = task_data.get('description', '')
            existing.trigger_type = task_data.get('trigger_type', 'cron')
            existing.trigger_value = task_data.get('trigger_value', '')
            existing.execute_type = task_data.get('execute_type', 'python')
            existing.execute_content = task_data.get('execute_content', '')
            existing.enabled = task_data.get('enabled', True)
            existing.paused = task_data.get('paused', False)
            existing.retry_count = task_data.get('retry_count', 0)
            existing.retry_delay_seconds = task_data.get('retry_delay_seconds', 60)
            existing.notify_email = task_data.get('notify_email', '')
            existing.notify_on_success = task_data.get('notify_on_success', False)
            existing.notify_on_failure = task_data.get('notify_on_failure', True)
            existing.set_env_vars(task_data.get('env_vars', {}))
            results['tasks_updated'] += 1
            task_name_to_id[task_data['name']] = existing.id
        else:
            new_task = Task(
                name=task_data['name'],
                description=task_data.get('description', ''),
                trigger_type=task_data.get('trigger_type', 'cron'),
                trigger_value=task_data.get('trigger_value', ''),
                execute_type=task_data.get('execute_type', 'python'),
                execute_content=task_data.get('execute_content', ''),
                enabled=task_data.get('enabled', True),
                paused=task_data.get('paused', False),
                retry_count=task_data.get('retry_count', 0),
                retry_delay_seconds=task_data.get('retry_delay_seconds', 60),
                notify_email=task_data.get('notify_email', ''),
                notify_on_success=task_data.get('notify_on_success', False),
                notify_on_failure=task_data.get('notify_on_failure', True),
            )
            new_task.set_env_vars(task_data.get('env_vars', {}))
            db.session.add(new_task)
            db.session.flush()
            task_name_to_id[task_data['name']] = new_task.id
            results['tasks_created'] += 1

    for task_data in data.get('tasks', []):
        dep_name = task_data.get('depends_on_name')
        if dep_name and dep_name in task_name_to_id:
            task = Task.query.filter_by(name=task_data['name']).first()
            if task:
                task.depends_on = task_name_to_id[dep_name]

    db.session.commit()
    return results


def restore_from_file(db, Task, EnvVar, filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return restore_from_dict(db, Task, EnvVar, data)


def get_backup_files():
    backup_dir = Config.BACKUP_DIR
    if not os.path.exists(backup_dir):
        return []
    files = []
    for f in sorted(os.listdir(backup_dir), reverse=True):
        if f.endswith('.json'):
            full_path = os.path.join(backup_dir, f)
            files.append({
                'filename': f,
                'path': full_path,
                'size': os.path.getsize(full_path),
                'modified': datetime.datetime.fromtimestamp(os.path.getmtime(full_path)).isoformat(),
            })
    return files