import os
import json
import queue
import socket
import threading
import time
import uuid
import datetime
import ssl
import platform
import shutil
from dotenv import load_dotenv

try:
    import psutil
except ImportError:
    psutil = None

load_dotenv()

for _v in ["MASTER_PORT", "SERVER_UUID"]:
    if _v not in os.environ:
        raise EnvironmentError(f"Variável ausente: {_v}")

# Bind em 0.0.0.0 para funcionar tanto no master original quanto no worker eleito
HOST        = "0.0.0.0"
PORT        = int(os.environ["MASTER_PORT"])
SERVER_UUID = os.environ["SERVER_UUID"]

# Porta UDP para descoberta do master por broadcast (SERVER_UUID)
DISCOVERY_PORT = int(os.getenv("DISCOVERY_PORT", str(PORT + 1)))

WORKER_STALE_TIMEOUT = int(os.getenv("WORKER_STALE_TIMEOUT", "30"))
MASTER_NEIGHBORS_RAW = os.getenv("MASTER_NEIGHBORS", "")
MASTER_CAPACITY = int(os.getenv("MASTER_CAPACITY", "100"))
MASTER_RELEASE_THRESHOLD = int(os.getenv("MASTER_RELEASE_THRESHOLD", "60"))
MASTER_HELP_TIMEOUT = int(os.getenv("MASTER_HELP_TIMEOUT", "5"))
LOAD_MONITOR_INTERVAL = int(os.getenv("LOAD_MONITOR_INTERVAL", "5"))

# ── Supervisor configuration ──
SUPERVISOR_HOST = os.getenv("SUPERVISOR_HOST", "nuted-ia.dev")
SUPERVISOR_PORT = int(os.getenv("SUPERVISOR_PORT", "443"))
SUPERVISOR_TLS = os.getenv("SUPERVISOR_TLS", "true").lower() in ("1", "true", "yes")
SUPERVISOR_SNI = os.getenv("SUPERVISOR_SNI", "nuted-ia.dev")
SUPERVISOR_INTERVAL = int(os.getenv("SUPERVISOR_INTERVAL", "10"))
SUPERVISOR_PAYLOAD_VERSION = os.getenv("SUPERVISOR_PAYLOAD_VERSION", "sprint4-monitor")

# ── Telemetry global variables ──
server_start_time = time.time()
cpu_history = []
cpu_history_lock = threading.Lock()

# Thread-safe running tasks tracking
running_tasks = {}
running_tasks_lock = threading.Lock()

# Counters for completed/failed tasks
tasks_completed_count = 0
tasks_failed_count = 0
tasks_stats_lock = threading.Lock()


def _detect_local_ip() -> str:
    """Detecta o IP local da máquina (não 127.x)."""
    for remote in [("8.8.8.8", 80), ("1.1.1.1", 80)]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(remote)
                ip = s.getsockname()[0]
                if ip and not ip.startswith("127."):
                    return ip
        except Exception:
            pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"


def _detect_broadcast(local_ip: str) -> str:
    """
    Deriva o endereço de broadcast direcionado a partir do IP local.
    Assume subrede /24 (mais comum em redes locais).
    Ex: 10.62.206.23 → 10.62.206.255
    Redes /24 cobrem a grande maioria dos ambientes LAN/universitários.
    """
    env_val = os.getenv("WORKER_BROADCAST_ADDRESS", "")
    # Se o usuário definiu explicitamente e não é o genérico, usa o dele
    if env_val and env_val != "255.255.255.255":
        return env_val
    # Auto-detecta o broadcast direcionado da subrede /24
    prefix = ".".join(local_ip.split(".")[:3])
    return f"{prefix}.255"


def _parse_neighbors(raw: str) -> list[dict]:
    out = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 3:
            print(f"[MASTER] Neighbor inválido ignorado: {item}")
            continue
        master_id, host, port = parts
        try:
            out.append({
                "master_id": master_id,
                "host":      host,
                "port":      int(port),
            })
        except ValueError:
            print(f"[MASTER] Neighbor inválido ignorado: {item}")
    return out


def _format_address(host: str, port: int) -> str:
    return f"{host}:{port}"


def _parse_address(address: str) -> tuple[str, int]:
    if not isinstance(address, str) or ":" not in address:
        raise ValueError("Address must be in ip:port format")
    host, port = address.rsplit(":", 1)
    return host, int(port)


def _send_tcp(host: str, port: int, payload: dict, timeout: float = 5.0) -> list[dict]:
    msgs = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((host, port))
            s.sendall((json.dumps(payload) + "\n").encode())
            buf = ""
            while True:
                try:
                    chunk = s.recv(4096).decode()
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    try:
                        msgs.append(json.loads(line))
                    except Exception:
                        pass
    except Exception as exc:
        print(f"[MASTER] Erro TCP para {host}:{port} — {exc}")
    return msgs


SERVER_HOST      = _detect_local_ip()
BROADCAST_ADDR   = _detect_broadcast(SERVER_HOST)
print(f"[MASTER] IP detectado automaticamente: {SERVER_HOST}")
print(f"[MASTER] Broadcast de descoberta: {BROADCAST_ADDR}")
MASTER_NEIGHBORS = _parse_neighbors(MASTER_NEIGHBORS_RAW)
help_lock = threading.Lock()

# Initialize neighbor states for tracking liveness
neighbor_states = {
    n["master_id"]: {
        "status": "unavailable",
        "last_heartbeat": None
    }
    for n in MASTER_NEIGHBORS
}
neighbor_states_lock = threading.Lock()

def _update_neighbor_state(master_id: str, status: str):
    with neighbor_states_lock:
        if master_id in neighbor_states:
            neighbor_states[master_id]["status"] = status
            if status == "available":
                neighbor_states[master_id]["last_heartbeat"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def _find_neighbor_id_by_address(address: str) -> str | None:
    try:
        host, port = _parse_address(address)
        for neighbor in MASTER_NEIGHBORS:
            if neighbor["host"] == host and neighbor["port"] == port:
                return neighbor["master_id"]
    except Exception:
        pass
    return None

# ── Registro de workers ───────────────────────────────────────────────────────
# { worker_uuid: {uuid, host, port, last_seen, busy, borrowed, origin_master_id, original_master_address, borrowed_to}} 

registry      = {}
registry_lock = threading.Lock()


def _register_worker(uuid, host, port, borrowed=None,
                     origin_master_id=None,
                     original_master_address=None,
                     borrowed_to=None):
    with registry_lock:
        previous = registry.get(uuid, {})
        registry[uuid] = {
            "uuid":                    uuid,
            "host":                    host,
            "port":                    int(port),
            "last_seen":               time.time(),
            "busy":                    previous.get("busy", False),
            "borrowed":                borrowed if borrowed is not None else previous.get("borrowed", False),
            "origin_master_id":        origin_master_id if origin_master_id is not None else previous.get("origin_master_id"),
            "original_master_address": original_master_address if original_master_address is not None else previous.get("original_master_address"),
            "borrowed_to":             borrowed_to if borrowed_to is not None else previous.get("borrowed_to"),
            "pending_return":          previous.get("pending_return", False),
        }


def _get_active_peers(exclude_uuid=None):
    """Retorna lista de workers ativos, mantendo os inativos no registro para telemetria."""
    now = time.time()
    with registry_lock:
        return [
            {"uuid": w["uuid"], "host": w["host"], "port": w["port"]}
            for u, w in registry.items()
            if u != exclude_uuid 
            and (now - w["last_seen"] <= WORKER_STALE_TIMEOUT)
            and not w.get("borrowed", False)
        ]


def _get_idle_workers():
    """Retorna workers locais disponíveis para ceder ou para tarefa."""
    now = time.time()
    with registry_lock:
        return [
            w for w in registry.values()
            if (now - w["last_seen"] <= WORKER_STALE_TIMEOUT)
            and not w.get("busy", False)
            and not w.get("borrowed", False)
            and not w.get("pending_return", False)
        ]


def _get_borrowed_workers():
    now = time.time()
    with registry_lock:
        return [
            w for w in registry.values()
            if (now - w["last_seen"] <= WORKER_STALE_TIMEOUT)
            and w.get("borrowed", False)
        ]


def _mark_worker_busy(worker_uuid, busy=True):
    with registry_lock:
        if worker_uuid in registry:
            registry[worker_uuid]["busy"] = busy


def _mark_worker_borrowed(worker_uuid, borrowed=True, borrowed_to=None,
                          original_master_address=None, pending_return=False):
    with registry_lock:
        if worker_uuid in registry:
            registry[worker_uuid]["borrowed"] = borrowed
            if borrowed_to is not None:
                registry[worker_uuid]["borrowed_to"] = borrowed_to
            if original_master_address is not None:
                registry[worker_uuid]["original_master_address"] = original_master_address
            registry[worker_uuid]["pending_return"] = pending_return


def _register_temporary_worker(worker_uuid, host, port, original_master_address):
    _register_worker(
        worker_uuid,
        host,
        port,
        borrowed=True,
        origin_master_id=None,
        original_master_address=original_master_address,
    )


def _find_neighbor_address(master_id: str) -> str | None:
    for neighbor in MASTER_NEIGHBORS:
        if neighbor["master_id"] == master_id:
            return _format_address(neighbor["host"], neighbor["port"])
    return None


def _current_load() -> int:
    return task_queue.qsize()


def _send_command_redirect(worker, target_address, request_id=None):
    if not target_address:
        raise ValueError("target_address is required for command_redirect")
    request_id = request_id or str(uuid.uuid4())
    payload = {
        "type": "command_redirect",
        "request_id": request_id,
        "payload": {
            "new_master_address": target_address,
        },
    }
    print(f"[MASTER] Enviando command_redirect para {worker['uuid']} "
          f"({worker['host']}:{worker['port']}) → {target_address}")
    _send_tcp(worker["host"], worker["port"], payload, timeout=MASTER_HELP_TIMEOUT)


def _handle_notify_worker_returned(msg):
    payload = msg.get("payload", {}) if isinstance(msg, dict) else {}
    worker_id = payload.get("worker_id")
    if not worker_id:
        return
    with registry_lock:
        worker = registry.get(worker_id)
        if worker:
            print(f"[MASTER] Notify_worker_returned recebido para {worker_id}")
            worker["borrowed"] = False
            worker["borrowed_to"] = None
            worker["original_master_address"] = None
            worker["pending_return"] = False


def _handle_request_help(msg):
    request_id = msg.get("request_id")
    payload = msg.get("payload", {}) if isinstance(msg, dict) else {}
    if not request_id or not isinstance(payload, dict):
        return {
            "type": "response_rejected",
            "request_id": request_id or str(uuid.uuid4()),
            "payload": {"reason": "invalid_request"},
        }

    current_load = _current_load()
    workers_needed = int(payload.get("workers_needed", 1))

    if current_load > MASTER_CAPACITY:
        reason = "high_load"
        print(f"[MASTER] request_help recusado ({reason}) — carga atual {current_load}, capacidade {MASTER_CAPACITY}")
        return {
            "type": "response_rejected",
            "request_id": request_id,
            "payload": {"reason": reason},
        }

    idle_workers = _get_idle_workers()
    if not idle_workers:
        print("[MASTER] request_help recusado (no_workers_available)")
        return {
            "type": "response_rejected",
            "request_id": request_id,
            "payload": {"reason": "no_workers_available"},
        }

    target_address = _find_neighbor_address(payload.get("master_id"))
    if not target_address:
        print(f"[MASTER] request_help recusado (unknown_master {payload.get('master_id')})")
        return {
            "type": "response_rejected",
            "request_id": request_id,
            "payload": {"reason": "unknown_master"},
        }

    chosen = idle_workers[:min(workers_needed, len(idle_workers))]
    worker_details = []
    for worker in chosen:
        _mark_worker_borrowed(worker["uuid"], borrowed=True,
                              borrowed_to=payload.get("master_id"),
                              original_master_address=_format_address(SERVER_HOST, PORT),
                              pending_return=False)
        worker_details.append({
            "id": worker["uuid"],
            "address": _format_address(worker["host"], worker["port"]),
        })

    print(f"[MASTER] request_help aceito — ofertando {len(worker_details)} workers")
    response = {
        "type": "response_accepted",
        "request_id": request_id,
        "payload": {
            "workers_offered": len(worker_details),
            "worker_details": worker_details,
        },
    }

    # Envia os redirecionamentos em segundo plano para não bloquear o request_help.
    def _redirect_batch():
        for worker in chosen:
            try:
                _send_command_redirect(worker, target_address)
            except Exception as exc:
                print(f"[MASTER] Falha ao redirecionar {worker['uuid']}: {exc}")
                _mark_worker_borrowed(worker["uuid"], borrowed=False,
                                      borrowed_to=None,
                                      original_master_address=None)

    threading.Thread(target=_redirect_batch, daemon=True).start()
    return response


def _request_help_from_neighbor(neighbor, workers_needed):
    request_id = str(uuid.uuid4())
    payload = {
        "type": "request_help",
        "request_id": request_id,
        "payload": {
            "master_id": SERVER_UUID,
            "current_load": _current_load(),
            "capacity": MASTER_CAPACITY,
            "workers_needed": workers_needed,
        },
    }
    responses = _send_tcp(neighbor["host"], neighbor["port"], payload,
                          timeout=MASTER_HELP_TIMEOUT)
    if not responses:
        print(f"[MASTER] Sem resposta de {neighbor['master_id']} ({neighbor['host']}:{neighbor['port']})")
        _update_neighbor_state(neighbor["master_id"], "unavailable")
        return None
    _update_neighbor_state(neighbor["master_id"], "available")
    for resp in responses:
        if resp.get("request_id") == request_id:
            return resp
    return None


def _try_request_help():
    if not MASTER_NEIGHBORS:
        return
    with help_lock:
        current_load = _current_load()
        if current_load <= MASTER_CAPACITY:
            return
        workers_needed = max(1, current_load - MASTER_CAPACITY)
        print(f"[MASTER] Saturado: solicitando ajuda para {workers_needed} workers")
        for neighbor in MASTER_NEIGHBORS:
            if workers_needed <= 0:
                break
            response = _request_help_from_neighbor(neighbor, workers_needed)
            if not response:
                continue
            if response.get("type") == "response_accepted":
                details = response.get("payload", {}).get("worker_details", [])
                offered = response.get("payload", {}).get("workers_offered", len(details))
                workers_needed -= offered
                print(f"[MASTER] {neighbor['master_id']} ofereceu {offered} workers")
            else:
                reason = response.get("payload", {}).get("reason", "unknown")
                print(f"[MASTER] {neighbor['master_id']} recusou help ({reason})")


def _release_borrowed_workers():
    borrowed = _get_borrowed_workers()
    if not borrowed:
        return
    if _current_load() >= MASTER_RELEASE_THRESHOLD:
        return

    print(f"[MASTER] Carga normalizou. Liberando {len(borrowed)} workers emprestados.")
    for worker in borrowed:
        original_address = worker.get("original_master_address")
        if not original_address:
            continue
        request_id = str(uuid.uuid4())
        payload = {
            "type": "command_release",
            "request_id": request_id,
            "payload": {
                "original_master_address": original_address,
            },
        }
        print(f"[MASTER] Enviando command_release para {worker['uuid']} ({worker['host']}:{worker['port']})")
        _send_tcp(worker["host"], worker["port"], payload, timeout=MASTER_HELP_TIMEOUT)

        notify_payload = {
            "type": "notify_worker_returned",
            "request_id": str(uuid.uuid4()),
            "payload": {
                "worker_id": worker["uuid"],
            },
        }
        try:
            original_host, original_port = _parse_address(original_address)
            _send_tcp(original_host, original_port, notify_payload,
                      timeout=MASTER_HELP_TIMEOUT)
            print(f"[MASTER] Notificado master de origem sobre retorno de {worker['uuid']}")
        except Exception as exc:
            print(f"[MASTER] Falha ao notificar retorno de {worker['uuid']}: {exc}")


def _load_monitor():
    while True:
        try:
            _try_request_help()
            _release_borrowed_workers()
        except Exception as exc:
            print(f"[MASTER] Erro no monitor de carga: {exc}")
        time.sleep(LOAD_MONITOR_INTERVAL)


task_queue    = queue.Queue()
task_log      = []          # histórico de tarefas concluídas
task_log_lock = threading.Lock()

# Popula fila com tarefas iniciais de exemplo
_INITIAL_TASKS = [
    {"id": f"task-{i:03d}", "USER": f"user{i}", "created_at": time.time()}
    for i in range(1, 21)
]
for _t in _INITIAL_TASKS:
    task_queue.put(_t)

print(f"[MASTER] Fila iniciada com {task_queue.qsize()} tarefas.")


def _log_task(worker_uuid, task_id, status, origin):
    """Registra conclusão de tarefa no log."""
    global tasks_completed_count, tasks_failed_count
    entry = {
        "worker":    worker_uuid,
        "task_id":   task_id,
        "status":    status,
        "origin":    origin,   # 'local' ou 'emprestado de <SERVER_UUID>'
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with task_log_lock:
        task_log.append(entry)
    with tasks_stats_lock:
        if status == "OK":
            tasks_completed_count += 1
        elif status == "NOK":
            tasks_failed_count += 1
    print(f"[MASTER] LOG | Worker={worker_uuid} ({origin}) | "
          f"Task={task_id} | Status={status}")


# ── Telemetry & Metrics Helpers ──

def _get_oldest_task_age() -> int:
    now = time.time()
    oldest_time = now
    
    with task_queue.mutex:
        for t in task_queue.queue:
            created = t.get("created_at")
            if created and created < oldest_time:
                oldest_time = created
                
    with running_tasks_lock:
        for t_info in running_tasks.values():
            created = t_info.get("task", {}).get("created_at")
            if created and created < oldest_time:
                oldest_time = created
                
    if oldest_time < now:
        return int(now - oldest_time)
    return 0


def _get_system_metrics():
    now = time.time()
    metrics = {
        "uptime_seconds": int(now - server_start_time),
        "load_average_1m": 0.0,
        "load_average_5m": 0.0,
        "cpu": {
            "usage_percent": 0.0,
            "count_logical": os.cpu_count() or 1,
            "count_physical": os.cpu_count() or 1,
        },
        "memory": {
            "total_mb": 0,
            "available_mb": 0,
            "percent_used": 0.0,
            "memory_used": 0,
        },
        "disk": {
            "total_gb": 0.0,
            "free_gb": 0.0,
            "percent_used": 0.0,
        }
    }
    
    # Disk
    try:
        du = shutil.disk_usage(".")
        metrics["disk"]["total_gb"] = round(du.total / (1024**3), 2)
        metrics["disk"]["free_gb"] = round(du.free / (1024**3), 2)
        metrics["disk"]["percent_used"] = round((du.used / du.total) * 100, 2)
    except Exception:
        pass
        
    # CPU & Memory
    if psutil:
        try:
            metrics["cpu"]["usage_percent"] = round(psutil.cpu_percent(), 2)
            metrics["cpu"]["count_logical"] = psutil.cpu_count(logical=True) or os.cpu_count() or 1
            metrics["cpu"]["count_physical"] = psutil.cpu_count(logical=False) or os.cpu_count() or 1
        except Exception:
            pass
            
        if hasattr(os, "getloadavg"):
            try:
                avg = os.getloadavg()
                metrics["load_average_1m"] = round(avg[0], 2)
                metrics["load_average_5m"] = round(avg[1], 2)
            except Exception:
                pass
        else:
            try:
                cpu_count = metrics["cpu"]["count_logical"]
                with cpu_history_lock:
                    if cpu_history:
                        avg_1m = sum(cpu_history[-6:]) / len(cpu_history[-6:])
                        metrics["load_average_1m"] = round((avg_1m / 100) * cpu_count, 2)
                        avg_5m = sum(cpu_history) / len(cpu_history)
                        metrics["load_average_5m"] = round((avg_5m / 100) * cpu_count, 2)
            except Exception:
                pass
                
        try:
            mem = psutil.virtual_memory()
            metrics["memory"]["total_mb"] = int(mem.total / (1024**2))
            metrics["memory"]["available_mb"] = int(mem.available / (1024**2))
            metrics["memory"]["percent_used"] = round(mem.percent, 2)
            metrics["memory"]["memory_used"] = int((mem.total - mem.available) / (1024**2))
        except Exception:
            pass
            
    return metrics


def _get_farm_state():
    now = time.time()
    
    total_registered = len(registry)
    
    with registry_lock:
        workers_alive_list = [w for w in registry.values() if now - w["last_seen"] <= WORKER_STALE_TIMEOUT]
        workers_alive = len(workers_alive_list)
        workers_utilization = sum(1 for w in workers_alive_list if w.get("busy"))
        
        workers_borrowed = sum(1 for w in workers_alive_list if w.get("borrowed") and w.get("borrowed_to"))
        workers_received = sum(1 for w in workers_alive_list if w.get("borrowed") and not w.get("borrowed_to"))
        workers_home = sum(1 for w in workers_alive_list if not w.get("borrowed"))
        
        borrowed_workers_payload = []
        for w in workers_alive_list:
            if w.get("borrowed"):
                if w.get("borrowed_to"):
                    borrowed_workers_payload.append({
                        "direction": "out",
                        "peer_uuid": w.get("borrowed_to")
                    })
                elif w.get("original_master_address"):
                    peer_uuid = _find_neighbor_id_by_address(w.get("original_master_address")) or "unknown"
                    borrowed_workers_payload.append({
                        "direction": "in",
                        "peer_uuid": peer_uuid
                    })
                    
    workers_failed = max(0, total_registered - workers_alive)
    workers_idle = max(0, workers_alive - workers_utilization)
    workers_available_capacity = workers_idle
    
    tasks_pending = task_queue.qsize()
    with running_tasks_lock:
        tasks_running = len(running_tasks)
        
    with tasks_stats_lock:
        tasks_completed = tasks_completed_count
        tasks_failed_tasks = tasks_failed_count
        
    oldest_task_age = _get_oldest_task_age()
    
    return {
        "workers": {
            "total_registered": total_registered,
            "workers_utilization": workers_utilization,
            "workers_alive": workers_alive,
            "workers_idle": workers_idle,
            "workers_borrowed": workers_borrowed,
            "workers_received": workers_received,
            "workers_failed": workers_failed,
            "workers_home": workers_home,
            "workers_available_capacity": workers_available_capacity,
            "borrowed_workers": borrowed_workers_payload,
        },
        "tasks": {
            "tasks_pending": tasks_pending,
            "tasks_running": tasks_running,
            "tasks_completed": tasks_completed,
            "tasks_failed": tasks_failed_tasks,
            "oldest_task_age_s": oldest_task_age,
        }
    }


def _get_neighbors_payload():
    payload = []
    with neighbor_states_lock:
        for master_id, state in neighbor_states.items():
            payload.append({
                "server_uuid": master_id,
                "status": state["status"],
                "last_heartbeat": state["last_heartbeat"] or datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            })
    return payload


# ── Background Telemetry Services ──

def _cpu_history_updater():
    while True:
        try:
            if psutil:
                cpu = psutil.cpu_percent()
            else:
                cpu = 0.0
            with cpu_history_lock:
                cpu_history.append(cpu)
                if len(cpu_history) > 30:
                    cpu_history.pop(0)
        except Exception:
            pass
        time.sleep(10)


def _neighbor_monitor():
    while True:
        for neighbor in MASTER_NEIGHBORS:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(2.0)
                    s.connect((neighbor["host"], neighbor["port"]))
                _update_neighbor_state(neighbor["master_id"], "available")
            except Exception:
                _update_neighbor_state(neighbor["master_id"], "unavailable")
        time.sleep(10)


def _supervisor_sender():
    print(f"[SUPERVISOR] Thread iniciada. Host={SUPERVISOR_HOST}:{SUPERVISOR_PORT}, TLS={SUPERVISOR_TLS}")
    while True:
        try:
            sys_metrics = _get_system_metrics()
            farm_metrics = _get_farm_state()
            neighbors_metrics = _get_neighbors_payload()
            
            payload = {
                "server_uuid": SERVER_UUID,
                "hostname": socket.gethostname(),
                "role": "master",
                "task": "performance_report",
                "timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "message_id": str(uuid.uuid4()),
                "payload_version": SUPERVISOR_PAYLOAD_VERSION,
                "performance": {
                    "system": sys_metrics,
                    "farm_state": farm_metrics,
                    "config_thresholds": {
                        "max_task": MASTER_CAPACITY,
                        "warn_cpu_percent": 85,
                        "warn_memory_percent": 85,
                        "release_task": MASTER_RELEASE_THRESHOLD,
                    },
                    "neighbors": neighbors_metrics
                }
            }
            
            _send_to_supervisor(payload)
            
        except Exception as e:
            print(f"[SUPERVISOR] Erro no coletor/enviador: {e}")
            
        time.sleep(SUPERVISOR_INTERVAL)


def _send_to_supervisor(payload):
    sock = None
    try:
        sock = socket.create_connection((SUPERVISOR_HOST, SUPERVISOR_PORT), timeout=5.0)
        if SUPERVISOR_TLS:
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=SUPERVISOR_SNI)
            
        msg = json.dumps(payload) + "\n"
        sock.sendall(msg.encode("utf-8"))
        print(f"[SUPERVISOR] Relatório de desempenho enviado com sucesso ({SUPERVISOR_HOST}:{SUPERVISOR_PORT}).")
    except Exception as e:
        print(f"[SUPERVISOR] Erro ao enviar para o supervisor: {e}")
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


# ── Handler de conexão ────────────────────────────────────────────────────────

def handle_client(conn, addr):
    print(f"[MASTER] Conectado: {addr}")
    buffer       = ""
    current_task = None   # tarefa atribuída nesta conexão (aguardando STATUS)

    try:
        while True:
            data = conn.recv(4096).decode()
            if not data:
                break

            buffer += data
            while "\n" in buffer:
                message, buffer = buffer.split("\n", 1)
                try:
                    msg = json.loads(message)
                except json.JSONDecodeError:
                    continue

                print(f"[MASTER] Recebido de {addr}: {msg}")

                msg_type = msg.get("type", "").lower()
                if msg_type:
                    if msg_type == "request_help":
                        response = _handle_request_help(msg)
                        conn.send((json.dumps(response) + "\n").encode())
                        continue

                    if msg_type == "notify_worker_returned":
                        _handle_notify_worker_returned(msg)
                        continue

                    if msg_type == "register_temporary_worker":
                        payload = msg.get("payload", {})
                        worker_uuid = payload.get("worker_id")
                        original_master_address = payload.get("original_master_address")
                        worker_host = addr[0]
                        worker_port = payload.get("worker_port") or 8000
                        if worker_uuid and original_master_address:
                            _register_temporary_worker(
                                worker_uuid,
                                worker_host,
                                worker_port,
                                original_master_address,
                            )
                            print(f"[MASTER] Worker temporário {worker_uuid} registrado de {original_master_address}")
                        continue

                    print(f"[MASTER] Tipo desconhecido recebido: {msg_type} — ignorando")
                    continue

                # ── HEARTBEAT ─────────────────────────────────────────────────
                if msg.get("TASK") == "HEARTBEAT":
                    worker_uuid = msg.get("WORKER_UUID")
                    worker_host = msg.get("WORKER_HOST")
                    worker_port = msg.get("WORKER_PORT")

                    if worker_uuid and worker_host and worker_port:
                        _register_worker(worker_uuid, worker_host, worker_port)

                    peers = _get_active_peers(exclude_uuid=worker_uuid)

                    conn.send((json.dumps({
                        "SERVER_UUID": SERVER_UUID,
                        "TASK":        "HEARTBEAT",
                        "RESPONSE":    "ALIVE",
                        "PEERS":       peers,
                    }) + "\n").encode())

                # ── APRESENTAÇÃO DO WORKER (Payload 2.1 / 2.1b) ───────────────
                elif msg.get("WORKER") == "ALIVE":
                    worker_uuid = msg.get("WORKER_UUID", "?")
                    origin_uuid = msg.get("SERVER_UUID")   # presente se "emprestado"

                    if origin_uuid:
                        origin = f"emprestado de {origin_uuid}"
                    else:
                        origin = "local"

                    print(f"[MASTER] Worker {worker_uuid} se apresentou ({origin})")

                    # Distribui tarefa ou informa fila vazia
                    try:
                        current_task = task_queue.get_nowait()
                        print(f"[MASTER] Distribuindo {current_task['id']} "
                              f"→ {worker_uuid}")
                        
                        # Track task in running tasks
                        with running_tasks_lock:
                            running_tasks[current_task["id"]] = {
                                "task": current_task,
                                "worker_uuid": worker_uuid,
                                "started_at": time.time(),
                            }
                            
                        # Payload 2.2
                        conn.send((json.dumps({
                            "TASK":    "QUERY",
                            "USER":    current_task["USER"],
                            "TASK_ID": current_task["id"],
                        }) + "\n").encode())

                    except queue.Empty:
                         print(f"[MASTER] Fila vazia para {worker_uuid}")
                         # Payload 2.3
                         conn.send((json.dumps({
                             "TASK": "NO_TASK",
                         }) + "\n").encode())
                         current_task = None

                # ── RESULTADO DO WORKER (Payload 2.4) ─────────────────────────
                elif msg.get("STATUS") in ("OK", "NOK") and msg.get("TASK") == "QUERY":
                    worker_uuid = msg.get("WORKER_UUID", "?")
                    status      = msg.get("STATUS")
                    task_id     = current_task["id"] if current_task else "?"

                    # Determina origem (local ou emprestado)
                    origin_uuid = msg.get("SERVER_UUID")
                    origin = f"emprestado de {origin_uuid}" if origin_uuid else "local"

                    _log_task(worker_uuid, task_id, status, origin)

                    # Remove from running tasks
                    if current_task:
                        with running_tasks_lock:
                            running_tasks.pop(current_task["id"], None)

                    # Payload 2.5 — ACK imediato
                    conn.send((json.dumps({
                        "STATUS": "ACK",
                    }) + "\n").encode())

                    current_task = None

                else:
                    conn.send((json.dumps({
                        "SERVER_UUID": SERVER_UUID,
                        "TASK":        "ERROR",
                        "RESPONSE":    "UNKNOWN_TASK",
                    }) + "\n").encode())

    except Exception as exc:
        print(f"[MASTER] Erro com {addr}: {exc}")
    finally:
        if current_task:
            print(f"[MASTER] Conexão com {addr} perdida. Devolvendo tarefa {current_task['id']} à fila.")
            task_queue.put(current_task)
            with running_tasks_lock:
                running_tasks.pop(current_task["id"], None)
        conn.close()
        print(f"[MASTER] Conexão encerrada: {addr}")


# ── Listener UDP de descoberta do master ─────────────────────────────────────

def _start_discovery_listener():
    """
    Escuta broadcasts UDP na DISCOVERY_PORT.
    Quando recebe FIND_MASTER com SERVER_UUID correto,
    responde com o IP real do servidor para que o cliente conecte.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", DISCOVERY_PORT))
        print(f"[DISCOVERY] Listener UDP na porta {DISCOVERY_PORT} "
              f"(UUID={SERVER_UUID})")
    except Exception as exc:
        print(f"[DISCOVERY] Falha ao abrir socket UDP: {exc}")
        return

    while True:
        try:
            data, addr = sock.recvfrom(4096)
            payload = json.loads(data.decode())
        except Exception:
            continue

        if payload.get("TASK") != "FIND_MASTER":
            continue

        requested_uuid = payload.get("SERVER_UUID", "")
        if requested_uuid and requested_uuid != SERVER_UUID:
            # Não é para este servidor
            continue

        print(f"[DISCOVERY] Requisição FIND_MASTER de {addr} "
              f"(UUID={requested_uuid or 'qualquer'})")

        response = json.dumps({
            "TASK":        "MASTER_FOUND",
            "SERVER_UUID": SERVER_UUID,
            "MASTER_IP":   SERVER_HOST,
            "MASTER_PORT": PORT,
        }).encode()
        try:
            sock.sendto(response, addr)
            print(f"[DISCOVERY] Respondido para {addr}: {SERVER_HOST}:{PORT}")
        except Exception as exc:
            print(f"[DISCOVERY] Erro ao responder: {exc}")


# ── Start ─────────────────────────────────────────────────────────────────────

def start_server():
    # Inicia listener UDP de descoberta em thread daemon
    threading.Thread(target=_start_discovery_listener, daemon=True).start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen()

    print(f"[MASTER] Servidor TCP iniciado na porta {PORT}")
    print(f"[MASTER] UUID: {SERVER_UUID}")
    print(f"[MASTER] IP: {SERVER_HOST}")

    threading.Thread(target=_load_monitor, daemon=True).start()
    
    # Inicia threads de monitoramento de CPU, vizinhos e envio de telemetria ao supervisor
    threading.Thread(target=_cpu_history_updater, daemon=True).start()
    threading.Thread(target=_neighbor_monitor, daemon=True).start()
    threading.Thread(target=_supervisor_sender, daemon=True).start()

    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle_client, args=(conn, addr),
                         daemon=True).start()


if __name__ == "__main__":
    start_server()