"""scheduler.py — File-based task scheduler.

Reads tasks from MEMORY_DIR/SCHEDULE.md, runs them via the configured CLI, and
tracks status and reports in MEMORY_DIR/.

Schedule file format (SCHEDULE.md):
    | ID | Scheduled Time     | Task Description | Assigned To   | Status       | Tester Status | Report Path | Recurrence |
    |:---|:---|:---|:---|:---|:---|:---|:---|
    | 1  | 2026-01-01 09:00   | Summarize news   | Claude-Worker | [ ] Pending  | [ ] Waiting   |             | daily 09:00 |

Recurrence formats:
    once / empty   → one-shot
    every Xm       → every X minutes
    every Xh       → every X hours
    every Xd       → every X days
    daily HH:MM    → every day at HH:MM
    weekly DAY HH:MM → every week on DAY at HH:MM
"""

import asyncio
import logging
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import dotenv_values

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('scheduler')

# All paths derived from MEMORY_DIR (same env var as config.py)
_MEMORY_DIR = os.environ.get("MEMORY_DIR", str(Path.home() / "memories"))
SCHEDULE_FILE = str(Path(_MEMORY_DIR) / "SCHEDULE.md")
REPORTS_DIR = str(Path(_MEMORY_DIR) / "task_reports")
REPORT_DONE_FILE = str(Path(_MEMORY_DIR) / "tasks_done.md")

TIMEZONE = os.environ.get('TIMEZONE', 'America/New_York')
_TZ = ZoneInfo(TIMEZONE)
TASK_TIMEOUT = int(os.environ.get('TASK_TIMEOUT', '300'))  # seconds

# Load bot credentials from .env for worker context
_env_path = Path(__file__).parent / '.env'
_env_vars = dotenv_values(_env_path)
BOT_TOKEN = _env_vars.get('TELEGRAM_BOT_TOKEN', '')
CHAT_ID = _env_vars.get('ALLOWED_USER_ID', '')


def get_now() -> datetime:
    return datetime.now(_TZ)


def send_telegram(text: str) -> None:
    """Send a Telegram notification via the bot API (sync, for use in thread context)."""
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("BOT_TOKEN or CHAT_ID not set — cannot send Telegram notification")
        return
    try:
        import httpx
        resp = httpx.post(
            f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
            json={'chat_id': CHAT_ID, 'text': text},
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info(f'Telegram notification sent: {text[:80]}')
    except Exception as e:
        logger.error(f'Failed to send Telegram notification: {e}')


WEEKDAY_MAP = {
    'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6,
    'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
    'friday': 4, 'saturday': 5, 'sunday': 6,
}


def parse_recurrence(recurrence_str: str):
    """Parse a recurrence string and return (type, value) or None for one-shot."""
    if not recurrence_str:
        return None
    s = recurrence_str.strip().lower()
    if s in ('once', '-', ''):
        return None

    m = re.match(r'^every\s+(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)$', s)
    if m:
        val = int(m.group(1))
        unit = m.group(2)[0]
        unit_map = {'m': 'minutes', 'h': 'hours', 'd': 'days'}
        return (unit_map[unit], val)

    m = re.match(r'^daily(?:\s+(\d{1,2}:\d{2}))?$', s)
    if m:
        time_str = m.group(1) or '00:00'
        return ('daily', time_str)

    m = re.match(r'^weekly\s+(\w+)(?:\s+(\d{1,2}:\d{2}))?$', s)
    if m:
        day = m.group(1).lower()
        time_str = m.group(2) or '00:00'
        if day in WEEKDAY_MAP:
            return ('weekly', (day, time_str))

    return None


def calc_next_run(recurrence, last_run_time: datetime) -> datetime | None:
    """Given a parsed recurrence and the last run time, return the next run datetime."""
    rtype, rval = recurrence

    if rtype == 'minutes':
        return last_run_time + timedelta(minutes=rval)
    elif rtype == 'hours':
        return last_run_time + timedelta(hours=rval)
    elif rtype == 'days':
        return last_run_time + timedelta(days=rval)
    elif rtype == 'daily':
        h, m = map(int, rval.split(':'))
        next_run = last_run_time.replace(hour=h, minute=m, second=0, microsecond=0)
        if next_run <= last_run_time:
            next_run += timedelta(days=1)
        return next_run
    elif rtype == 'weekly':
        day_str, time_str = rval
        target_weekday = WEEKDAY_MAP[day_str]
        h, m = map(int, time_str.split(':'))
        next_run = last_run_time.replace(hour=h, minute=m, second=0, microsecond=0)
        days_ahead = target_weekday - next_run.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        next_run += timedelta(days=days_ahead)
        return next_run

    return None


def read_schedule() -> list[dict]:
    if not os.path.exists(SCHEDULE_FILE):
        return []
    try:
        with open(SCHEDULE_FILE, 'r') as f:
            lines = f.readlines()

        tasks = []
        if len(lines) < 3:
            return []
        for line in lines[3:]:
            if not line.strip() or not line.startswith('|'):
                continue
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 8:
                task_id = parts[1]
                if not task_id or re.match(r'^[:\-\s]+$', task_id):
                    continue
                task = {
                    'id': task_id,
                    'time': parts[2],
                    'task': parts[3],
                    'assigned_to': parts[4],
                    'status': parts[5],
                    'tester_status': parts[6],
                    'report_path': parts[7] if len(parts) > 7 else '',
                    'recurrence': parts[8] if len(parts) > 8 else 'once',
                }
                tasks.append(task)
        return tasks
    except Exception as e:
        logger.error(f'Error reading schedule: {e}')
        return []


def write_schedule(tasks: list[dict]) -> None:
    """Write the full task list back to SCHEDULE.md."""
    header = [
        '# Task Schedule\n',
        '\n',
        '| ID | Scheduled Time | Task Description | Assigned To | Status | Tester Status | Report Path | Recurrence |\n',
        '|:---|:---|:---|:---|:---|:---|:---|:---|\n'
    ]
    lines = header
    for task in tasks:
        recurrence = task.get('recurrence', 'once')
        report = task.get('report_path', '')
        line = f"| {task['id']} | {task['time']} | {task['task']} | {task['assigned_to']} | {task['status']} | {task['tester_status']} | {report} | {recurrence} |\n"
        lines.append(line)
    with open(SCHEDULE_FILE, 'w') as f:
        f.writelines(lines)


def update_task_status(task_id: str, new_status: str, report_path: str | None = None) -> None:
    tasks = read_schedule()
    for task in tasks:
        if task['id'] == task_id:
            task['status'] = new_status
            if report_path:
                task['report_path'] = report_path
    write_schedule(tasks)


def _load_agent_system_prompt(agent_id: str) -> str:
    """Load agent system prompt from agents.db for scheduled agent tasks."""
    try:
        import sqlite3
        from pathlib import Path as _Path
        db_path = str(_Path(_MEMORY_DIR) / 'agents.db')
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT system_prompt, skills FROM agents WHERE id = ?', (agent_id,)).fetchone()
        conn.close()
        if not row:
            return ''
        prompt = row['system_prompt'] or ''
        try:
            import json
            import sys
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from agent_skills import build_skills_prompt
            skills = json.loads(row['skills'] or '[]')
            skill_section = build_skills_prompt(skills)
            if skill_section:
                prompt = prompt + '\n\n' + skill_section
        except Exception:
            pass
        return prompt
    except Exception as e:
        logger.warning(f'Failed to load agent system prompt for {agent_id}: {e}')
        return ''


def _build_worker_prompt(task_id: str, task_desc: str, assigned_to: str = 'Worker') -> str:
    agent_system_prompt = ''
    agent_label = 'scheduled task worker'
    if assigned_to and assigned_to.startswith('Agent:'):
        agent_id = assigned_to[6:].strip().lower()
        agent_system_prompt = _load_agent_system_prompt(agent_id)
        if agent_system_prompt:
            agent_label = f'{agent_id} specialist agent'

    agent_persona_section = ''
    if agent_system_prompt:
        agent_persona_section = (
            f'## Your Expert Persona\n\n'
            f'{agent_system_prompt}\n\n'
        )

    return (
        f'TASK ID: {task_id}\n'
        f'OBJECTIVE: {task_desc}\n\n'
        f'{agent_persona_section}'
        f'## Context & Tools\n\n'
        f'You are a {agent_label}. Complete the objective above autonomously.\n\n'
        f'### Sending Telegram Messages\n'
        f'To send a Telegram notification, use:\n'
        f'```bash\n'
        f'curl -s -X POST "https://api.telegram.org/bot{BOT_TOKEN}/sendMessage" '
        f'-H "Content-Type: application/json" '
        f'-d \'{{"chat_id": {CHAT_ID}, "text": "YOUR_MESSAGE_HERE"}}\'\n'
        f'```\n\n'
        f'### Memory & Files\n'
        f'- Notes and memory files are at: {_MEMORY_DIR}/\n'
        f'- Key files: USER.md, MEMORY.md\n'
        f'- You can read any file on the system to find information needed for the task.\n\n'
        f'### Rules\n'
        f'- Complete the task, then confirm what you did.\n'
        f'- If the task says to send a message, you MUST actually send it via the Telegram API.\n'
        f'- Do NOT just describe what you would do - actually do it.\n'
    )


def _reschedule_if_recurring(task: dict) -> None:
    """If a task has a recurrence, add a new pending row with the next run time."""
    recurrence_str = task.get('recurrence', 'once')
    recurrence = parse_recurrence(recurrence_str)
    if not recurrence:
        return

    now = get_now()
    next_run = calc_next_run(recurrence, now)
    if not next_run:
        return

    next_time_str = next_run.strftime('%Y-%m-%d %H:%M')

    tasks = read_schedule()
    max_id = 0
    for t in tasks:
        try:
            max_id = max(max_id, int(t['id']))
        except ValueError:
            pass

    try:
        int(task['id'])
        new_id = str(max_id + 1).zfill(len(task['id']))
    except ValueError:
        new_id = task['id']

    new_task = {
        'id': new_id,
        'time': next_time_str,
        'task': task['task'],
        'assigned_to': task['assigned_to'],
        'status': '[ ] Pending',
        'tester_status': '[ ] Waiting',
        'report_path': '',
        'recurrence': recurrence_str,
    }
    tasks.append(new_task)
    write_schedule(tasks)

    logger.info(f"Recurring task '{task['task']}' rescheduled as #{new_task['id']} at {next_time_str}")
    send_telegram(f"[Scheduler] Recurring task rescheduled as #{new_task['id']} → next run: {next_time_str}")


def run_task(task: dict) -> None:
    task_id = task['id']
    task_desc = task['task']
    assigned_to = task.get('assigned_to', 'Worker')

    os.makedirs(REPORTS_DIR, exist_ok=True)
    report_file = os.path.join(REPORTS_DIR, f'TASK_{task_id}.md')

    logger.info(f'Starting task {task_id}: {task_desc}')
    update_task_status(task_id, '[~] Running')
    send_telegram(f'[Scheduler] Task #{task_id} started: {task_desc}')

    try:
        prompt = _build_worker_prompt(task_id, task_desc, assigned_to=assigned_to)
        env = os.environ.copy()
        env.pop('CLAUDECODE', None)

        # Use CLI_COMMAND from env (defaults to 'claude')
        cli_cmd = env.get('CLI_COMMAND', 'claude')
        result = subprocess.run(
            [cli_cmd, '-p', '--dangerously-skip-permissions', prompt],
            capture_output=True, text=True, env=env,
            timeout=TASK_TIMEOUT,
        )

        with open(report_file, 'w') as f:
            f.write(f'# Task Report: {task_id}\n')
            f.write(f'Task: {task_desc}\n\n')
            f.write(f'Completed at: {get_now().strftime("%Y-%m-%d %H:%M:%S")}\n\n')
            f.write('## Output:\n')
            f.write(result.stdout)
            if result.stderr:
                f.write('\n## Stderr:\n')
                f.write(result.stderr)

        try:
            with open(REPORT_DONE_FILE, 'a') as f:
                f.write(f'## [DONE] {get_now().strftime("%Y-%m-%d %H:%M:%S")}\n')
                f.write(f'Task: {task_desc}\n')
                f.write(f'ID: {task_id} | Report: {report_file}\n\n---\n\n')
        except Exception as e:
            logger.error(f'Failed to write to done report: {e}')

        worker_failed = (
            result.returncode != 0
            or ('error' in result.stderr.lower() if result.stderr else False)
        )

        if worker_failed:
            update_task_status(task_id, '[!] Error', report_file)
            send_telegram(f'[Scheduler] Task #{task_id} FAILED: {task_desc}\nReport: {report_file}')
            logger.error(f'Task {task_id} worker returned errors.')
        else:
            update_task_status(task_id, '[x] Done', report_file)
            send_telegram(f'[Scheduler] Task #{task_id} completed: {task_desc}')
            logger.info(f'Task {task_id} completed.')

        _reschedule_if_recurring(task)

    except subprocess.TimeoutExpired:
        logger.error(f'Task {task_id} timed out after {TASK_TIMEOUT}s')
        update_task_status(task_id, f'[!] Timeout ({TASK_TIMEOUT}s)')
        send_telegram(f'[Scheduler] Task #{task_id} TIMED OUT after {TASK_TIMEOUT}s: {task_desc}')
        _reschedule_if_recurring(task)
    except Exception as e:
        logger.error(f'Error executing task {task_id}: {e}')
        update_task_status(task_id, f'[!] Error: {str(e)}')
        send_telegram(f'[Scheduler] Task #{task_id} ERROR: {str(e)}')
        _reschedule_if_recurring(task)


async def scheduler_loop() -> None:
    logger.info('Scheduler loop started.')
    while True:
        try:
            tasks = await asyncio.to_thread(read_schedule)
            now = get_now()
            for task in tasks:
                if '[ ] Pending' in task['status']:
                    try:
                        scheduled_time_str = task['time']
                        scheduled_time = datetime.strptime(scheduled_time_str, '%Y-%m-%d %H:%M')
                        scheduled_time = scheduled_time.replace(tzinfo=_TZ)
                        same_day = scheduled_time.date() == now.date()
                        if now >= scheduled_time and same_day:
                            await asyncio.to_thread(run_task, task)
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f'Error in scheduler loop: {e}')
        await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(scheduler_loop())
