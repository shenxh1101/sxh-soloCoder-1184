import os
import sys
import io
import subprocess
import datetime
import traceback
import smtplib
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
from croniter import croniter

from config import Config

_running_tasks = set()
_running_lock = threading.Lock()


def validate_trigger(trigger_type, trigger_value, timezone=None):
    if trigger_type == 'cron':
        parts = trigger_value.strip().split()
        if len(parts) != 5:
            raise ValueError(f'Cron 表达式必须是 5 个字段 (分 时 日 月 周)，当前是 {len(parts)} 个字段')
        try:
            croniter(trigger_value)
        except (ValueError, KeyError) as e:
            raise ValueError(f'Cron 表达式不合法: {e}')
        return True
    elif trigger_type == 'interval':
        try:
            seconds = int(trigger_value)
            if seconds <= 0:
                raise ValueError('间隔时间必须大于 0')
        except ValueError:
            raise ValueError(f'间隔时间必须是正整数')
        return True
    else:
        raise ValueError(f'未知触发类型: {trigger_type}')


def preview_trigger_times(trigger_type, trigger_value, count=5, timezone=None):
    if timezone is None:
        timezone = datetime.timezone(datetime.timedelta(hours=8))
    if isinstance(timezone, str):
        import pytz
        timezone = pytz.timezone(timezone)

    times = []
    now = datetime.datetime.now(timezone)

    if trigger_type == 'cron':
        try:
            cron = croniter(trigger_value, now)
            for _ in range(count):
                next_dt = cron.get_next(datetime.datetime)
                times.append(next_dt.isoformat())
        except Exception:
            pass
    elif trigger_type == 'interval':
        try:
            seconds = int(trigger_value)
            for i in range(1, count + 1):
                times.append((now + datetime.timedelta(seconds=seconds * i)).isoformat())
        except Exception:
            pass

    return times


class SchedulerManager:
    def __init__(self, app=None):
        self.app = app
        self.scheduler = None

    def init_app(self, app):
        self.app = app
        self.scheduler = BackgroundScheduler(
            timezone=app.config.get('SCHEDULER_TIMEZONE', 'Asia/Shanghai'),
            job_defaults={
                'coalesce': False,
                'max_instances': 3,
            }
        )
        self.scheduler.add_listener(self._job_event_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
        self.scheduler.start()

    def _job_event_listener(self, event):
        if not self.app:
            return
        with self.app.app_context():
            from models import db, Execution, Task
            execution = Execution.query.filter_by(
                id=getattr(event, 'execution_db_id', None)
            ).first()
            if not execution:
                return

            if event.exception:
                execution.status = 'failed'
                error_msg = ''.join(traceback.format_exception_only(type(event.exception), event.exception))
                execution.output_log = (execution.output_log or '') + '\n' + error_msg
            else:
                execution.status = 'success'
                if event.retval:
                    execution.output_log = (execution.output_log or '') + '\n' + str(event.retval)

            execution.end_time = datetime.datetime.now(datetime.UTC)
            db.session.commit()

            with _running_lock:
                _running_tasks.discard(execution.task_id)

            task = Task.query.get(execution.task_id)
            if task and execution.status in ('success', 'failed'):
                self._send_notification(task, execution)
                if execution.status == 'success':
                    self._trigger_dependent_tasks(task, execution)

    def _build_trigger(self, task):
        validate_trigger(task.trigger_type, task.trigger_value,
                         self.app.config.get('SCHEDULER_TIMEZONE', 'Asia/Shanghai'))
        if task.trigger_type == 'cron':
            parts = task.trigger_value.strip().split()
            return CronTrigger(
                minute=parts[0], hour=parts[1], day=parts[2],
                month=parts[3], day_of_week=parts[4],
                timezone=self.app.config.get('SCHEDULER_TIMEZONE', 'Asia/Shanghai')
            )
        elif task.trigger_type == 'interval':
            return IntervalTrigger(seconds=int(task.trigger_value))
        else:
            raise ValueError(f'Unknown trigger type: {task.trigger_type}')

    def add_job(self, task):
        job_id = f'{task.id}_{task.name}'
        trigger = self._build_trigger(task)
        self.scheduler.add_job(
            func=self._execute_task,
            trigger=trigger,
            args=[task.id],
            kwargs={'_trigger_type': 'scheduled'},
            id=job_id,
            name=task.name,
            replace_existing=True,
        )
        if task.paused:
            self.scheduler.pause_job(job_id)

    def remove_job(self, task_id, task_name):
        job_id = f'{task_id}_{task_name}'
        try:
            self.scheduler.remove_job(job_id)
        except Exception:
            pass

    def pause_job(self, task):
        job_id = f'{task.id}_{task.name}'
        try:
            self.scheduler.pause_job(job_id)
        except Exception:
            pass

    def resume_job(self, task):
        job_id = f'{task.id}_{task.name}'
        try:
            self.scheduler.resume_job(job_id)
        except Exception:
            pass

    def run_job_now(self, task_id, trigger_source_execution_id=None):
        with self.app.app_context():
            from models import db, Task
            task = db.session.get(Task, task_id)
            if not task:
                raise ValueError(f'Task {task_id} not found')
            trigger_type_label = 'dependency' if trigger_source_execution_id else 'manual'
            self.scheduler.add_job(
                func=self._execute_task,
                args=[task.id],
                kwargs={
                    '_trigger_type': trigger_type_label,
                    'trigger_source_execution_id': trigger_source_execution_id,
                },
                id=f'manual_{task.id}_{datetime.datetime.now(datetime.UTC).timestamp()}',
                name=f'Manual: {task.name}',
            )

    def _execute_task(self, task_id, retry_attempt=0, trigger_source_execution_id=None, _trigger_type=None):
        with self.app.app_context():
            from models import db, Task, Execution
            task = db.session.get(Task, task_id)
            if not task:
                return

            try:
                task.name
            except Exception:
                return

            if _trigger_type:
                trigger_type = _trigger_type
            elif trigger_source_execution_id:
                trigger_type = 'dependency'
            elif retry_attempt > 0:
                trigger_type = 'retry'
            else:
                trigger_type = 'scheduled'

            mode = task.concurrency_mode or 'skip'

            with _running_lock:
                if task_id in _running_tasks:
                    if mode == 'skip':
                        skip_execution = Execution(
                            task_id=task.id,
                            start_time=datetime.datetime.now(datetime.UTC),
                            end_time=datetime.datetime.now(datetime.UTC),
                            status='skipped',
                            trigger_type=trigger_type,
                            trigger_source_execution_id=trigger_source_execution_id,
                            retry_attempt=retry_attempt,
                            skip_reason='任务正在运行中，并发模式为跳过',
                        )
                        db.session.add(skip_execution)
                        db.session.commit()
                        return
                    elif mode == 'replace':
                        from models import Execution
                        running = Execution.query.filter_by(task_id=task_id, status='running').all()
                        for rex in running:
                            rex.status = 'cancelled'
                            rex.end_time = datetime.datetime.now(datetime.UTC)
                            rex.output_log = (rex.output_log or '') + '\n[已取消：被新触发替换]'
                        db.session.commit()
                        with _running_lock:
                            _running_tasks.discard(task_id)
                _running_tasks.add(task_id)

            execution = Execution(
                task_id=task.id,
                start_time=datetime.datetime.now(datetime.UTC),
                status='running',
                trigger_type=trigger_type,
                trigger_source_execution_id=trigger_source_execution_id,
                retry_attempt=retry_attempt,
            )
            db.session.add(execution)
            db.session.commit()

            env = os.environ.copy()
            env.update(task.get_env_vars())
            from models import EnvVar
            global_vars = EnvVar.query.all()
            for gv in global_vars:
                env[gv.key] = gv.value

            for key, value in env.items():
                if not isinstance(value, str):
                    env[key] = str(value)

            output_buffer = io.StringIO()

            try:
                if task.execute_type == 'python':
                    old_stdout = sys.stdout
                    old_stderr = sys.stderr
                    sys.stdout = output_buffer
                    sys.stderr = output_buffer
                    try:
                        exec_globals = {'__builtins__': __builtins__}
                        exec(task.execute_content, exec_globals)
                    finally:
                        sys.stdout = old_stdout
                        sys.stderr = old_stderr
                elif task.execute_type == 'command':
                    result = subprocess.run(
                        task.execute_content,
                        shell=True,
                        capture_output=True,
                        text=True,
                        timeout=3600,
                        env=env,
                    )
                    output_buffer.write(result.stdout)
                    if result.stderr:
                        output_buffer.write('\n[STDERR]\n')
                        output_buffer.write(result.stderr)
                    if result.returncode != 0:
                        raise subprocess.CalledProcessError(
                            result.returncode, task.execute_content,
                            output=result.stdout, stderr=result.stderr
                        )

                output = output_buffer.getvalue()
                execution.output_log = output
                execution.status = 'success'
                execution.end_time = datetime.datetime.now(datetime.UTC)
                db.session.commit()

            except Exception as e:
                output = output_buffer.getvalue()
                output += f'\n[ERROR] {traceback.format_exc()}'
                execution.output_log = output

                if task.retry_count > 0 and retry_attempt < task.retry_count:
                    execution.status = 'retrying'
                    execution.end_time = datetime.datetime.now(datetime.UTC)
                    db.session.commit()
                    with _running_lock:
                        _running_tasks.discard(task_id)
                    import time
                    time.sleep(task.retry_delay_seconds)
                    self._execute_task(task_id, retry_attempt + 1, trigger_source_execution_id, _trigger_type='retry')
                else:
                    execution.status = 'failed'
                    execution.end_time = datetime.datetime.now(datetime.UTC)
                    db.session.commit()

            finally:
                output_buffer.close()
                with _running_lock:
                    _running_tasks.discard(task_id)

            if execution.status in ('success', 'failed'):
                self._send_notification(task, execution)
                if execution.status == 'success':
                    self._trigger_dependent_tasks(task, execution)

    def _send_notification(self, task, execution):
        if not task.notify_email:
            return
        if execution.status == 'success' and not task.notify_on_success:
            return
        if execution.status == 'failed' and not task.notify_on_failure:
            return

        smtp_config = {
            'host': self.app.config.get('SMTP_HOST'),
            'port': self.app.config.get('SMTP_PORT'),
            'user': self.app.config.get('SMTP_USER'),
            'password': self.app.config.get('SMTP_PASSWORD'),
            'from': self.app.config.get('SMTP_FROM'),
            'use_tls': self.app.config.get('SMTP_USE_TLS'),
        }

        if not smtp_config['host'] or not smtp_config['from']:
            return

        try:
            msg = MIMEMultipart()
            msg['From'] = smtp_config['from']
            msg['To'] = task.notify_email
            status_cn = {'success': '成功', 'failed': '失败', 'running': '运行中', 'retrying': '重试中'}
            msg['Subject'] = f'[任务调度] {task.name} - 执行{status_cn.get(execution.status, execution.status)}'

            body = f"""
任务名称: {task.name}
执行状态: {status_cn.get(execution.status, execution.status)}
开始时间: {execution.start_time.strftime('%Y-%m-%d %H:%M:%S') if execution.start_time else ''}
结束时间: {execution.end_time.strftime('%Y-%m-%d %H:%M:%S') if execution.end_time else ''}
重试次数: {execution.retry_attempt}

输出日志:
{execution.output_log[:2000] if execution.output_log else '(无)'}
"""
            msg.attach(MIMEText(body, 'plain', 'utf-8'))

            server = smtplib.SMTP(smtp_config['host'], smtp_config['port'], timeout=10)
            if smtp_config['use_tls']:
                server.starttls()
            if smtp_config['user'] and smtp_config['password']:
                server.login(smtp_config['user'], smtp_config['password'])
            server.send_message(msg)
            server.quit()
        except Exception:
            pass

    def _trigger_dependent_tasks(self, task, execution):
        if execution.status != 'success':
            return
        with self.app.app_context():
            from models import db, Task
            dependent_tasks = Task.query.filter_by(depends_on=task.id, enabled=True, paused=False).all()
            for dt in dependent_tasks:
                self.run_job_now(dt.id, trigger_source_execution_id=execution.id)

    def get_scheduler_status(self):
        if not self.scheduler:
            return {'running': False, 'jobs': []}
        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append({
                'id': job.id,
                'name': job.name,
                'next_run_time': job.next_run_time.isoformat() if job.next_run_time else None,
                'trigger': str(job.trigger),
            })
        return {
            'running': self.scheduler.running,
            'active_jobs': len(jobs),
            'jobs': jobs,
        }

    def reload_all_jobs(self):
        with self.app.app_context():
            from models import db, Task
            for job in list(self.scheduler.get_jobs()):
                try:
                    self.scheduler.remove_job(job.id)
                except Exception:
                    pass
            tasks = Task.query.filter_by(enabled=True).all()
            for task in tasks:
                try:
                    self.add_job(task)
                except Exception:
                    pass


scheduler_manager = SchedulerManager()