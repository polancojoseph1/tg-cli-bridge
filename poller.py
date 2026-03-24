"""poller.py — Git push poller for bridgebot trigger system.

Polls configured git repos every N seconds and fires webhook triggers
when new commits are detected on watched branches.

Any webhook trigger in agents.db with:
    config.event     = "push"
    config.repo_path = "/path/to/repo"
    config.branch    = "main"   (optional, defaults to "main")

...will be fired automatically when a new commit lands on that branch.
No GitHub webhook configuration required.
"""

import json
import logging
import os
import sqlite3
import subprocess
import time
import urllib.request

# ── Config ───────────────────────────────────────────────────────────────────
POLL_INTERVAL  = 5    # seconds between polls
COOLDOWN_SECS  = 300  # don't re-fire the same trigger within 5 minutes
DB_PATH       = os.path.expanduser(os.environ.get("POLLER_DB_PATH", os.path.join(os.environ.get("MEMORY_DIR", "~"), "agents.db")))
STATE_FILE    = os.path.expanduser(os.environ.get("POLLER_STATE_FILE", "~/.jefe/poller_state.json"))
SERVER_URL    = os.environ.get("POLLER_SERVER_URL", "http://localhost:8585")
LOG_FILE      = os.path.expanduser("~/Library/Logs/jefe/poller.log")

# ── Logging ──────────────────────────────────────────────────────────────────
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [poller] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("poller")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_triggers() -> list[dict]:
    """Return all enabled webhook triggers that have a repo_path configured."""
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, agent_id, config FROM triggers "
            "WHERE trigger_type='webhook' AND enabled=1"
        ).fetchall()
        con.close()
    except Exception as e:
        log.error("DB read failed: %s", e)
        return []

    result = []
    for row in rows:
        try:
            config = json.loads(row["config"])
        except Exception:
            config = {}

        if config.get("event") == "push" and config.get("repo_path"):
            result.append({
                "id":        row["id"],
                "agent_id":  row["agent_id"],
                "repo_path": config["repo_path"],
                "branch":    config.get("branch", "main"),
            })
    return result


def get_remote_hash(repo_path: str, branch: str) -> str | None:
    """Fetch from remote and return the latest commit hash on that branch."""
    import re as _re
    if not _re.match(r"^[a-zA-Z0-9][a-zA-Z0-9/_.-]*[a-zA-Z0-9]$", branch):
        log.warning("Skipping trigger — invalid branch name: %r", branch)
        return None
    try:
        subprocess.run(
            ["git", "fetch", "origin", branch, "--quiet"],
            cwd=repo_path, capture_output=True, timeout=15,
        )
        result = subprocess.run(
            ["git", "rev-parse", f"origin/{branch}"],
            cwd=repo_path, capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception as e:
        log.warning("git fetch failed for %s: %s", repo_path, e)
        return None


def fire_trigger(trigger_id: str, branch: str, commit_hash: str) -> bool:
    """POST a GitHub-like push payload to the local webhook endpoint."""
    payload = json.dumps({
        "ref":    f"refs/heads/{branch}",
        "after":  commit_hash,
        "source": "poller",
    }).encode()

    req = urllib.request.Request(
        f"{SERVER_URL}/triggers/webhook/{trigger_id}",
        data=payload,
        headers={
            "Content-Type":    "application/json",
            "X-GitHub-Event":  "push",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            log.info("Fired trigger '%s' → %s", trigger_id, body)
            return body.get("ok", False)
    except Exception as e:
        log.error("Failed to fire trigger '%s': %s", trigger_id, e)
        return False


def poll_once(state: dict) -> bool:
    """Check all git-backed webhook triggers. Returns True if state changed."""
    triggers = get_triggers()
    if not triggers:
        return False

    changed = False
    for t in triggers:
        tid    = t["id"]
        repo   = t["repo_path"]
        branch = t["branch"]
        key    = f"{tid}:{repo}:{branch}"

        current_hash = get_remote_hash(repo, branch)
        if not current_hash:
            continue

        last_hash = state.get(key)

        if last_hash is None:
            # First run — seed state, don't fire
            log.info("Seeding '%s' at %s (%s/%s)", tid, current_hash[:8], repo, branch)
            state[key] = current_hash
            changed = True

        elif current_hash != last_hash:
            cooldown_key = f"{key}:last_fired"
            last_fired = state.get(cooldown_key, 0)
            if time.time() - last_fired < COOLDOWN_SECS:
                # Update hash silently — commit is noted but report already queued
                log.info("Cooldown active for '%s', skipping fire (hash %s)", tid, current_hash[:8])
                state[key] = current_hash
                changed = True
            else:
                log.info(
                    "New commit on '%s' [%s/%s]: %s → %s",
                    tid, repo, branch, last_hash[:8], current_hash[:8],
                )
                if fire_trigger(tid, branch, current_hash):
                    state[key] = current_hash
                    state[cooldown_key] = time.time()
                    changed = True

    return changed


def main():
    log.info(
        "Poller started — interval=%ds  db=%s  server=%s",
        POLL_INTERVAL, DB_PATH, SERVER_URL,
    )
    state = load_state()
    while True:
        try:
            if poll_once(state):
                save_state(state)
        except Exception as e:
            log.error("Unhandled error in poll_once: %s", e)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
