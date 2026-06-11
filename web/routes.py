import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash

from models import db, Task, Execution, EnvVar
from scheduler_manager import scheduler_manager
from services.backup import get_backup_files

web_bp = Blueprint('web', __name__)


@web_bp.route('/')
def index():
    all_tasks = Task.query.all()
    total_tasks = len(all_tasks)
    active_tasks = sum(1 for t in all_tasks if t.enabled and not t.paused)
    paused_tasks = sum(1 for t in all_tasks if t.paused)

    today = datetime.datetime.now(datetime.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    failed_today = Execution.query.filter(
        Execution.status == 'failed',
        Execution.start_time >= today,
    ).count()

    recent_executions = Execution.query.order_by(Execution.start_time.desc()).limit(10).all()

    return render_template(
        'index.html',
        total_tasks=total_tasks,
        active_tasks=active_tasks,
        paused_tasks=paused_tasks,
        failed_today=failed_today,
        recent_executions=recent_executions,
        tasks=all_tasks,
    )


@web_bp.route('/tasks')
def list_tasks():
    tasks = Task.query.order_by(Task.created_at.desc()).all()
    return render_template('tasks.html', tasks=tasks)


@web_bp.route('/tasks/create', methods=['GET', 'POST'])
def create_task():
    if request.method == 'POST':
        return handle_task_form(request.form)
    all_tasks = Task.query.all()
    return render_template('task_form.html', task=None, all_tasks=all_tasks)


@web_bp.route('/tasks/<int:task_id>/edit', methods=['GET', 'POST'])
def edit_task(task_id):
    task = db.session.get(Task, task_id)
    if not task:
        flash('任务不存在', 'error')
        return redirect(url_for('web.list_tasks'))

    if request.method == 'POST':
        return handle_task_form(request.form, task)

    all_tasks = Task.query.all()
    return render_template('task_form.html', task=task, all_tasks=all_tasks)


def handle_task_form(form, task=None):
    name = form.get('name', '').strip()
    if not name:
        flash('任务名称不能为空', 'error')
        all_tasks = Task.query.all()
        return render_template('task_form.html', task=task, all_tasks=all_tasks)

    existing = Task.query.filter_by(name=name).first()
    if existing and (task is None or existing.id != task.id):
        flash(f'任务 "{name}" 已存在', 'error')
        all_tasks = Task.query.all()
        return render_template('task_form.html', task=task, all_tasks=all_tasks)

    env_keys = request.form.getlist('env_key')
    env_values = request.form.getlist('env_value')
    env_vars = {}
    for k, v in zip(env_keys, env_values):
        if k.strip():
            env_vars[k.strip()] = v

    depends_on = form.get('depends_on')
    depends_on = int(depends_on) if depends_on else None

    if task is None:
        task = Task(
            name=name,
            description=form.get('description', ''),
            trigger_type=form.get('trigger_type', 'cron'),
            trigger_value=form.get('trigger_value', ''),
            execute_type=form.get('execute_type', 'python'),
            execute_content=form.get('execute_content', ''),
            enabled=True,
            paused=False,
            retry_count=int(form.get('retry_count', 0)),
            retry_delay_seconds=int(form.get('retry_delay_seconds', 60)),
            notify_email=form.get('notify_email', ''),
            notify_on_success=form.get('notify_on_success') == 'on',
            notify_on_failure=form.get('notify_on_failure', '') == 'on',
            depends_on=depends_on,
        )
        task.set_env_vars(env_vars)
        db.session.add(task)
        db.session.commit()

        if task.enabled:
            try:
                scheduler_manager.add_job(task)
            except Exception as e:
                flash(f'任务已创建但调度失败: {str(e)}', 'warning')

        flash('任务创建成功', 'success')
    else:
        scheduler_manager.remove_job(task.id, task.name)
        task.name = name
        task.description = form.get('description', '')
        task.trigger_type = form.get('trigger_type', 'cron')
        task.trigger_value = form.get('trigger_value', '')
        task.execute_type = form.get('execute_type', 'python')
        task.execute_content = form.get('execute_content', '')
        task.retry_count = int(form.get('retry_count', 0))
        task.retry_delay_seconds = int(form.get('retry_delay_seconds', 60))
        task.notify_email = form.get('notify_email', '')
        task.notify_on_success = form.get('notify_on_success') == 'on'
        task.notify_on_failure = form.get('notify_on_failure', '') == 'on'
        task.depends_on = depends_on
        task.set_env_vars(env_vars)
        task.updated_at = datetime.datetime.now(datetime.UTC)
        db.session.commit()

        if task.enabled:
            scheduler_manager.add_job(task)

        flash('任务更新成功', 'success')

    return redirect(url_for('web.list_tasks'))


@web_bp.route('/executions')
def execution_history():
    task_id = request.args.get('task_id', type=int)
    status = request.args.get('status', '')
    query = Execution.query.order_by(Execution.start_time.desc())
    if task_id:
        query = query.filter_by(task_id=task_id)
    if status:
        query = query.filter_by(status=status)
    executions = query.limit(200).all()
    all_tasks = Task.query.all()
    return render_template(
        'history.html',
        executions=executions,
        all_tasks=all_tasks,
        selected_task_id=task_id,
        selected_status=status,
    )


@web_bp.route('/executions/<int:execution_id>')
def execution_detail(execution_id):
    execution = db.session.get(Execution, execution_id)
    if not execution:
        flash('执行记录不存在', 'error')
        return redirect(url_for('web.execution_history'))
    return render_template('execution_detail.html', execution=execution)


@web_bp.route('/scheduler')
def scheduler_status():
    status = scheduler_manager.get_scheduler_status()
    return render_template('scheduler_status.html', status=status)


@web_bp.route('/env-vars')
def env_vars():
    env_vars = EnvVar.query.order_by(EnvVar.key).all()
    return render_template('env_vars.html', env_vars=env_vars)


@web_bp.route('/backup')
def backup():
    backup_files = get_backup_files()
    return render_template('backup.html', backup_files=backup_files)