"""Saral on a Hugging Face ZeroGPU Space — one container, the same processes as docker-compose.

ZeroGPU Spaces must be Gradio apps that start through `demo.launch()`, so this file is a small
supervisor around Gradio's server:

  postgres   local disk (/tmp), restored from / dumped to the /data bucket (see below)
  redis      localhost only; the run bus and trace streams are fine being ephemeral
  migrations `alembic upgrade head` on every boot
  worker     `python -m saral.worker.main` as its own process, restarted if it dies
  api        Saral's FastAPI app, mounted at /api on Gradio's server
  frontend   the static Next.js build (deploy/hf-space/build.sh), served at / and /eval

Why dump instead of running Postgres on the bucket: a bucket keeps files across restarts but not
their permissions, and Postgres refuses a data directory it doesn't own (seen in the probe). So
the database lives on local disk and is dumped to /data whenever it changed, checked every
SARAL_DUMP_EVERY_S seconds (default 60), and on SIGTERM. A settings-change restart was seen to
skip the SIGTERM dump, so the periodic one is what bounds the loss (about a minute).

Saral never uses the GPU. ZeroGPU is just the free hardware a Gradio Space runs on.
"""

# ZeroGPU: `spaces` must be imported before anything touches torch.
import spaces  # noqa: I001

import contextlib
import glob
import os
import pwd
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)  # Saral's data paths (data/policy_corpus, …) are relative to the repo root

PG_PORT, REDIS_PORT = 5433, 6379
PG_DATA = Path("/tmp/pgdata")
DUMP = Path(os.environ.get("SARAL_DUMP_PATH", "/data/saral.dump"))
DUMP_EVERY_S = int(os.environ.get("SARAL_DUMP_EVERY_S", "60"))
SHUTDOWN_MARK = DUMP.parent / "last_shutdown.txt"
DB = "saral"

# Defaults for this deployment; Space variables / secrets override them.
SPACE_URL = f"https://{os.environ['SPACE_HOST']}" if os.environ.get("SPACE_HOST") else ""
for key, value in {
    "APP_ENV": "prod",
    "DEMO_MODE": "true",
    "LOG_JSON": "false",
    "DATABASE_URL": f"postgresql+asyncpg://postgres@127.0.0.1:{PG_PORT}/{DB}",
    "REDIS_URL": f"redis://127.0.0.1:{REDIS_PORT}/0",
    "STORE_BACKEND": "postgres",
    "CHECKPOINT_BACKEND": "postgres",
    "EMBEDDER": "sentence-transformer",
    "FRONTEND_ORIGINS": SPACE_URL or "http://localhost:7860",
    # Hugging Face's proxy appends the real client IP to X-Forwarded-For.
    "CLIENT_IP_HEADER": "X-Forwarded-For",
    "CLIENT_IP_FROM_RIGHT": "1",
    "WORKER_METRICS_PORT": "0",
}.items():
    os.environ.setdefault(key, value)
# The image sets PYTHONPATH already: prepend, so child processes (worker, alembic) import saral.
os.environ["PYTHONPATH"] = os.pathsep.join(
    filter(None, [str(ROOT / "backend"), os.environ.get("PYTHONPATH")])
)
sys.path.insert(0, str(ROOT / "backend"))


def log(msg: str) -> None:
    print(f"[space] {msg}", flush=True)


# --- postgres ------------------------------------------------------------------------------

PG_BIN = sorted(glob.glob("/usr/lib/postgresql/*/bin"))[-1]
PG_USER = pwd.getpwnam("postgres")  # created by the Debian package; initdb refuses root


def _as_postgres() -> None:
    os.setgid(PG_USER.pw_gid)
    os.setuid(PG_USER.pw_uid)


def _pg(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, check=True, capture_output=True, text=True,
        preexec_fn=_as_postgres if os.getuid() == 0 else None, **kw,
    )


def _client(tool: str, *args: str) -> list[str]:
    return [f"{PG_BIN}/{tool}", "-h", "127.0.0.1", "-p", str(PG_PORT), "-U", "postgres", *args]


def start_postgres() -> None:
    PG_DATA.mkdir(parents=True, exist_ok=True)
    os.chown(PG_DATA, PG_USER.pw_uid, PG_USER.pw_gid)
    if not (PG_DATA / "PG_VERSION").exists():
        _pg([f"{PG_BIN}/initdb", "-D", str(PG_DATA), "-U", "postgres", "--auth=trust"])
    _pg([f"{PG_BIN}/pg_ctl", "-D", str(PG_DATA), "-l", "/tmp/postgres.log", "-w", "start",
         "-o", f"-p {PG_PORT} -k /tmp -c listen_addresses=127.0.0.1"])
    exists = subprocess.run(
        _client("psql", "-tAc", f"SELECT 1 FROM pg_database WHERE datname='{DB}'"),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if exists:
        return
    subprocess.run(_client("createdb", DB), check=True)
    if DUMP.exists() and DUMP.stat().st_size > 0:
        local = Path("/tmp/restore.dump")
        shutil.copyfile(DUMP, local)
        r = subprocess.run(_client("pg_restore", "-d", DB, "--no-owner", str(local)),
                           capture_output=True, text=True)
        # pg_restore exits non-zero on harmless warnings too; the migrations below verify.
        log(f"restored {DUMP} ({DUMP.stat().st_size} bytes), pg_restore exit {r.returncode}")
    else:
        log("no dump on /data: fresh database")


_dump_lock = threading.Lock()


def dump_postgres() -> None:
    if not DUMP.parent.exists():
        return  # no bucket mounted: nothing to persist to
    with _dump_lock:
        local = Path("/tmp/saral.dump")
        try:
            subprocess.run(_client("pg_dump", "-Fc", "-f", str(local), DB), check=True,
                           capture_output=True, text=True, timeout=120)
            part = DUMP.with_suffix(".part")
            shutil.copyfile(local, part)
            os.replace(part, DUMP)
            log(f"dumped database to {DUMP} ({DUMP.stat().st_size} bytes)")
        except Exception as e:  # noqa: BLE001 — never take the app down over a backup
            log(f"dump failed: {e}")


def _writes() -> str:
    """Rows inserted/updated/deleted since start: unchanged means nothing new to dump."""
    q = "SELECT coalesce(sum(n_tup_ins + n_tup_upd + n_tup_del), 0) FROM pg_stat_user_tables"
    r = subprocess.run(_client("psql", "-d", DB, "-tAc", q), capture_output=True, text=True)
    return r.stdout.strip()


def dump_loop() -> None:
    last = None
    while True:
        time.sleep(DUMP_EVERY_S)
        now = _writes()
        if now != last:
            dump_postgres()
            last = now


# --- redis, migrations, worker -------------------------------------------------------------


def start_redis() -> None:
    subprocess.Popen(
        ["redis-server", "--port", str(REDIS_PORT), "--bind", "127.0.0.1", "--save", "",
         "--appendonly", "no"],
        stdout=subprocess.DEVNULL,
    )
    import redis

    r = redis.Redis(port=REDIS_PORT)
    for _ in range(100):
        try:
            r.ping()
            return
        except redis.ConnectionError:
            time.sleep(0.1)
    raise RuntimeError("redis did not start")


def migrate() -> None:
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)


def supervise_worker() -> None:
    """The worker is its own process, as in docker-compose; restart it if it exits."""
    while True:
        proc = subprocess.Popen([sys.executable, "-m", "saral.worker.main"])
        code = proc.wait()
        log(f"worker exited with {code}; restarting in 5s")
        time.sleep(5)


# --- boot ----------------------------------------------------------------------------------

if SHUTDOWN_MARK.exists():
    log(f"previous shutdown: {SHUTDOWN_MARK.read_text().strip()}")
    SHUTDOWN_MARK.unlink(missing_ok=True)
else:
    log("previous shutdown left no marker (killed before the SIGTERM dump, or first boot)")
start_postgres()
start_redis()
migrate()
threading.Thread(target=supervise_worker, daemon=True).start()
threading.Thread(target=dump_loop, daemon=True).start()

import gradio as gr  # noqa: E402
from starlette.responses import FileResponse  # noqa: E402
from starlette.routing import Mount, Route  # noqa: E402
from starlette.staticfiles import StaticFiles  # noqa: E402

from saral.api.app import app as saral_api  # noqa: E402


@spaces.GPU
def _unused_gpu_hook() -> str:
    """ZeroGPU expects a GPU function to exist; Saral itself runs entirely on CPU."""
    return "unused"


with gr.Blocks(title="Saral") as demo:
    gr.Markdown("Saral runs at [/](/). This page is the Gradio shell ZeroGPU Spaces require.")

STATIC = ROOT / "static"


def _page(name: str):
    async def endpoint(_request):
        return FileResponse(STATIC / name)

    return endpoint


def on_shutdown(signum, _frame) -> None:
    log(f"signal {signum}: dumping database before exit")
    dump_postgres()
    with contextlib.suppress(OSError):
        SHUTDOWN_MARK.write_text(f"signal {signum} at {time.strftime('%Y-%m-%d %H:%M:%S')}, dumped\n")
    subprocess.run([f"{PG_BIN}/pg_ctl", "-D", str(PG_DATA), "-m", "fast", "stop"],
                   preexec_fn=_as_postgres if os.getuid() == 0 else None, check=False)
    os._exit(0)


if __name__ == "__main__":
    # SSR puts a Node proxy in front of Python that doesn't forward the routes added below.
    demo.launch(prevent_thread_lock=True, ssr_mode=False)
    # Saral's frontend and API take precedence over Gradio's own routes.
    demo.app.router.routes[0:0] = [
        Route("/", _page("index.html")),
        Route("/eval", _page("eval.html")),
        Mount("/_next", app=StaticFiles(directory=STATIC / "_next")),
        Mount("/api", app=saral_api),
    ]
    signal.signal(signal.SIGTERM, on_shutdown)
    log("ready")
    demo.block_thread()
