"""
Worker P2P — Eleição de master via Bully Algorithm (TCP puro)

CONFIGURAÇÃO OBRIGATÓRIA em cada máquina (.env):
  SERVER_UUID=<uuid-do-master>   ← mesmo valor usado no servidor
  WORKER_UUID=<uuid-deste-worker>
  MASTER_PORT=<porta-tcp-do-master>
  WORKER_PORT=<porta-tcp-deste-worker>

  O IP do master é descoberto AUTOMATICAMENTE via broadcast UDP.
  Não é necessário configurar MASTER_IP.

  DISCOVERY_PORT (opcional, padrão = MASTER_PORT+1): porta UDP de descoberta.

COMO FUNCIONA A ELEIÇÃO:
  1. Um nó detecta que o master caiu (N heartbeats falhos).
  2. Ele consulta o WORKER_STATUS de todos os peers via TCP.
  3. Todos os nós aplicam a mesma ordem: (-free_space, uuid) → mesmo vencedor.
  4. O vencedor vira master e notifica via TCP + UDP broadcast.
  5. Os demais atualizam o ponteiro para o novo master.
"""

import os
import json
import shutil
import socket
import subprocess
import threading
import time

import schedule
from dotenv import load_dotenv

load_dotenv()

# ── Variáveis de ambiente ─────────────────────────────────────────────────────

for _var in ["MASTER_PORT", "SERVER_UUID", "WORKER_UUID", "WORKER_PORT"]:
    if _var not in os.environ:
        raise EnvironmentError(f"Variável ausente no .env: {_var}")

MASTER_PORT      = int(os.environ["MASTER_PORT"])
WORKER_UUID      = os.environ["WORKER_UUID"]
_WH_ENV          = os.getenv("WORKER_HOST", "")
WORKER_PORT      = int(os.environ["WORKER_PORT"])
# [LEGADO] WORKER_PEERS_STR — peers estáticos; eleição agora usa broadcast
# WORKER_PEERS_STR = os.getenv("WORKER_PEERS", "")

WORKER_DISCOVERY_ENABLED  = os.getenv("WORKER_DISCOVERY_ENABLED", "true").lower() in ("1", "true", "yes")
_BROADCAST_ENV            = os.getenv("WORKER_BROADCAST_ADDRESS", "")
WORKER_DISCOVERY_TIMEOUT  = int(os.getenv("WORKER_DISCOVERY_TIMEOUT", "2"))
HEARTBEAT_THRESHOLD       = int(os.getenv("HEARTBEAT_THRESHOLD", "4"))
HEARTBEAT_INTERVAL        = int(os.getenv("HEARTBEAT_INTERVAL", "5"))
ELECTION_STATUS_TIMEOUT   = int(os.getenv("ELECTION_STATUS_TIMEOUT", "4"))
NEW_MASTER_WAIT           = int(os.getenv("NEW_MASTER_WAIT", "12"))
TASK_INTERVAL             = int(os.getenv("TASK_INTERVAL", "10"))

# UUID do master alvo — usado na descoberta por broadcast e para detectar "Emprestado"
ORIGINAL_SERVER_UUID = os.getenv("SERVER_UUID", "")

# Porta UDP de descoberta do master (deve coincidir com o servidor)
DISCOVERY_PORT = int(os.getenv("DISCOVERY_PORT", str(MASTER_PORT + 1)))

# Eleição via broadcast — tempos configuráveis
ELECTION_DELAY           = int(os.getenv("ELECTION_DELAY", "30"))   # espera antes de eleger
ELECTION_COLLECT_TIMEOUT = int(os.getenv("ELECTION_COLLECT_TIMEOUT", "5"))  # janela de coleta


# ── Helpers de inicialização ──────────────────────────────────────────────────

# [LEGADO] _parse_peers — não usado com eleição via broadcast
# def _parse_peers(raw: str):
#     out = []
#     for item in raw.split(","):
#         item = item.strip()
#         if not item or ":" not in item:
#             continue
#         host, port = item.rsplit(":", 1)
#         try:
#             out.append((host.strip(), int(port.strip())))
#         except ValueError:
#             pass
#     return out


def _detect_host() -> str:
    if _WH_ENV:
        return _WH_ENV
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
    Assume subrede /24 (mais comum em LANs e redes universitárias).
    Ex: 10.62.206.23 → 10.62.206.255
    Se o usuário definiu WORKER_BROADCAST_ADDRESS explicitamente
    (e não é o genérico 255.255.255.255), usa o valor dele.
    """
    if _BROADCAST_ENV and _BROADCAST_ENV != "255.255.255.255":
        return _BROADCAST_ENV
    prefix = ".".join(local_ip.split(".")[:3])
    return f"{prefix}.255"


# [LEGADO] STATIC_PEERS — peers estáticos; eleição agora via broadcast
# STATIC_PEERS = _parse_peers(WORKER_PEERS_STR)
WORKER_HOST              = _detect_host()
WORKER_BROADCAST_ADDRESS = _detect_broadcast(WORKER_HOST)

print("=" * 60)
print(f"  Worker iniciando")
print(f"  UUID : {WORKER_UUID}")
print(f"  Host : {WORKER_HOST}:{WORKER_PORT}")
print(f"  Master UUID alvo: {ORIGINAL_SERVER_UUID or '(qualquer)'}")
print(f"  Broadcast master : {WORKER_BROADCAST_ADDRESS}:{DISCOVERY_PORT}")
print(f"  Broadcast eleição: {WORKER_BROADCAST_ADDRESS}:{WORKER_PORT}")
print(f"  ELECTION_DELAY   : {ELECTION_DELAY}s")
print("=" * 60)


# ── Descoberta do master por broadcast UDP ────────────────────────────────────

def discover_master(retries: int = 3, timeout: float = 3.0) -> tuple[str, int] | None:
    """
    Envia FIND_MASTER via broadcast UDP e aguarda resposta MASTER_FOUND
    com o IP real do servidor que possui o SERVER_UUID procurado.
    Retorna (ip, port) ou None se não encontrar.
    """
    request = json.dumps({
        "TASK":        "FIND_MASTER",
        "SERVER_UUID": ORIGINAL_SERVER_UUID,
        "WORKER_UUID": WORKER_UUID,
    }).encode()

    for attempt in range(1, retries + 1):
        print(f"[DISCOVERY] Tentativa {attempt}/{retries} — "
              f"broadcast FIND_MASTER (UUID={ORIGINAL_SERVER_UUID or 'qualquer'})")
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.settimeout(timeout)
                s.sendto(request, (WORKER_BROADCAST_ADDRESS, DISCOVERY_PORT))

                deadline = time.time() + timeout
                while time.time() < deadline:
                    try:
                        data, addr = s.recvfrom(4096)
                        resp = json.loads(data.decode())
                        if resp.get("TASK") != "MASTER_FOUND":
                            continue
                        # Aceita qualquer UUID ou o UUID exato procurado
                        if (ORIGINAL_SERVER_UUID
                                and resp.get("SERVER_UUID") != ORIGINAL_SERVER_UUID):
                            continue
                        ip   = resp.get("MASTER_IP")
                        port = resp.get("MASTER_PORT", MASTER_PORT)
                        if ip:
                            print(f"[DISCOVERY] Master encontrado: {ip}:{port} "
                                  f"(UUID={resp.get('SERVER_UUID')})")
                            return (ip, int(port))
                    except socket.timeout:
                        break
                    except Exception:
                        pass
        except Exception as exc:
            print(f"[DISCOVERY] Erro no broadcast: {exc}")

    print("[DISCOVERY] Master não encontrado via broadcast.")
    return None


# ── Estado global ─────────────────────────────────────────────────────────────

state_lock = threading.Lock()

failed_hb            = 0
is_master            = False
election_in_progress = False
master_proc          = None          # subprocess do servidor.py quando eleito
new_master_event     = threading.Event()  # sinalizado quando NEW_MASTER chega

current_master = {
    "uuid": ORIGINAL_SERVER_UUID or "MASTER",
    "ip":   None,   # preenchido pela descoberta via broadcast
    "port": MASTER_PORT,
}

# [LEGADO] known_peers — workers não conhecem peers; eleição agora via broadcast
# Mantido para compatibilidade com código legado comentado abaixo.
known_peers      = []   # workers não conhecem peers antes do master cair
known_peers_lock = threading.Lock()


# ── Primitivas de rede ────────────────────────────────────────────────────────

def send_tcp(host: str, port: int, payload: dict, timeout: int = 3):
    """Envia payload JSON via TCP e retorna lista de respostas JSON."""
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
    except Exception:
        pass
    return msgs


def send_udp(payload: dict):
    """Envia payload JSON via UDP broadcast (melhor esforço)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(json.dumps(payload).encode(),
                     (WORKER_BROADCAST_ADDRESS, WORKER_PORT))
    except Exception:
        pass


def get_free_space() -> int:
    return shutil.disk_usage(".").free


# ── [LEGADO] Descoberta de peers via master ───────────────────────────────────
# Workers não conhecem peers entre si enquanto o master está ativo.
# A eleição agora usa broadcast UDP direto (start_election_broadcast abaixo).
#
# def _update_known_peers(peers_from_master: list):
#     global known_peers
#     updated = set()  # sem STATIC_PEERS
#     for p in peers_from_master:
#         h, port = p.get("host"), p.get("port")
#         if h and port:
#             updated.add((h, int(port)))
#     with known_peers_lock:
#         known_peers = [(h, p) for h, p in updated
#                        if not (h == WORKER_HOST and p == WORKER_PORT)]
#
# def get_peers():
#     with known_peers_lock:
#         peers = set(known_peers)
#     if WORKER_DISCOVERY_ENABLED:
#         try:
#             with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
#                 s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
#                 s.settimeout(WORKER_DISCOVERY_TIMEOUT)
#                 s.sendto(
#                     json.dumps({"TASK": "DISCOVER_WORKER",
#                                 "WORKER_UUID": WORKER_UUID}).encode(),
#                     (WORKER_BROADCAST_ADDRESS, WORKER_PORT),
#                 )
#                 deadline = time.time() + WORKER_DISCOVERY_TIMEOUT
#                 while time.time() < deadline:
#                     try:
#                         data, _ = s.recvfrom(4096)
#                         r = json.loads(data.decode())
#                         if (r.get("TASK") == "DISCOVER_RESPONSE"
#                                 and r.get("WORKER_UUID") != WORKER_UUID):
#                             h, p = r.get("WORKER_HOST"), r.get("WORKER_PORT")
#                             if h and p:
#                                 peers.add((h, int(p)))
#                     except socket.timeout:
#                         break
#                     except Exception:
#                         pass
#         except Exception:
#             pass
#     return [(h, p) for h, p in peers
#             if not (h == WORKER_HOST and p == WORKER_PORT)]


# ── Eleição (Bully Algorithm adaptado) ───────────────────────────────────────

def _election_key(node: dict):
    """
    Chave de ordenação determinística — igual em todos os nós.
    Vence quem tiver MAIS espaço livre.
    Em empate, vence UUID menor lexicograficamente (estável).
    """
    return (-node.get("FREE_SPACE", 0), node.get("WORKER_UUID", ""))


# ── [LEGADO] Eleição via TCP/peers (Bully Algorithm com peers conhecidos) ───────────
# Substituiído por start_election_broadcast (eleição via broadcast UDP puro).
#
# def _query_status(host, port, bucket, lock):
#     responses = send_tcp(host, port, {"TASK": "WORKER_STATUS"}, timeout=ELECTION_STATUS_TIMEOUT)
#     for r in responses:
#         if r.get("TASK") == "WORKER_STATUS_RESPONSE":
#             with lock:
#                 bucket.append(r)
#
# def start_election():
#     global election_in_progress, failed_hb
#     with state_lock:
#         if is_master or election_in_progress:
#             return
#         election_in_progress = True
#         failed_hb = 0
#     print("[ELECTION] ════ Iniciando eleição [LEGADO] ════")
#     new_master_event.clear()
#     peers  = get_peers()
#     bucket = []
#     lock   = threading.Lock()
#     threads = [threading.Thread(target=_query_status, args=(h, p, bucket, lock), daemon=True)
#                for h, p in peers]
#     for t in threads: t.start()
#     for t in threads: t.join(timeout=ELECTION_STATUS_TIMEOUT + 1)
#     if new_master_event.is_set():
#         with state_lock: election_in_progress = False
#         return
#     my_info = {"WORKER_UUID": WORKER_UUID, "WORKER_HOST": WORKER_HOST,
#                "WORKER_PORT": WORKER_PORT, "FREE_SPACE": get_free_space()}
#     candidates = [my_info] + bucket
#     candidates.sort(key=_election_key)
#     winner = candidates[0]
#     if winner["WORKER_UUID"] == WORKER_UUID:
#         with state_lock: election_in_progress = False
#         _become_master()
#         _notify_new_master()
#     else:
#         with state_lock: election_in_progress = False
#         received = new_master_event.wait(timeout=NEW_MASTER_WAIT)
#         if not received:
#             threading.Thread(target=start_election, daemon=True).start()


# ── Eleição via broadcast UDP (sem conhecimento prévio de peers) ────────────────

def start_election_broadcast():
    """
    Eleição via broadcast UDP puro — workers não precisam conhecer peers:
      1. Envia ELECTION_BROADCAST na subrede e coleta ELECTION_RESPONSE.
      2. Adiciona o próprio nó como candidato.
      3. Ordena pelos mesmos critérios determinísticos (- free_space, uuid).
      4. Vencedor vira master e anuncia via broadcast; demais aguardam NEW_MASTER.
    """
    global election_in_progress, failed_hb

    with state_lock:
        if is_master or election_in_progress:
            return
        election_in_progress = True
        failed_hb = 0

    print("[ELECTION] ════ Iniciando eleição via broadcast UDP ════")
    new_master_event.clear()

    my_info = {
        "WORKER_UUID": WORKER_UUID,
        "WORKER_HOST": WORKER_HOST,
        "WORKER_PORT": WORKER_PORT,
        "FREE_SPACE":  get_free_space(),
    }
    candidates = [my_info]

    broadcast_target = (WORKER_BROADCAST_ADDRESS, WORKER_PORT)
    print(f"[ELECTION] Enviando ELECTION_BROADCAST → {broadcast_target}")

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.settimeout(ELECTION_COLLECT_TIMEOUT)
            s.sendto(json.dumps({
                "TASK":        "ELECTION_BROADCAST",
                "WORKER_UUID": WORKER_UUID,
                "WORKER_HOST": WORKER_HOST,
                "WORKER_PORT": WORKER_PORT,
                "FREE_SPACE":  get_free_space(),
            }).encode(), broadcast_target)

            deadline = time.time() + ELECTION_COLLECT_TIMEOUT
            while time.time() < deadline:
                try:
                    data, _ = s.recvfrom(4096)
                    resp = json.loads(data.decode())
                    if (resp.get("TASK") == "ELECTION_RESPONSE"
                            and resp.get("WORKER_UUID") != WORKER_UUID):
                        candidates.append(resp)
                        print(f"[ELECTION] Candidato: {resp['WORKER_UUID']} "
                              f"({resp.get('FREE_SPACE', 0) // (1024**3)} GB livre)")
                except socket.timeout:
                    break
                except Exception:
                    pass
    except Exception as exc:
        print(f"[ELECTION] Erro no socket de eleição: {exc}")

    # Se chegou NEW_MASTER durante a coleta, cancela
    if new_master_event.is_set():
        print("[ELECTION] NEW_MASTER recebido durante coleta. Eleição cancelada.")
        with state_lock:
            election_in_progress = False
        return

    candidates.sort(key=_election_key)
    winner = candidates[0]

    print(f"[ELECTION] Candidatos ({len(candidates)}): "
          + " | ".join(
              f"{c['WORKER_UUID']} ({c.get('FREE_SPACE', 0) // (1024**3)} GB)"
              for c in candidates
          ))
    print(f"[ELECTION] Vencedor → {winner['WORKER_UUID']} "
          f"({winner['WORKER_HOST']}:{winner['WORKER_PORT']})")

    # ── Passo 3: agir ────────────────────────────────────────────────────────
    if winner["WORKER_UUID"] == WORKER_UUID:
        with state_lock:
            election_in_progress = False
        _become_master()
        _notify_new_master()
    else:
        with state_lock:
            election_in_progress = False
        print(f"[ELECTION] Aguardando anúncio do vencedor ({NEW_MASTER_WAIT}s)…")
        received = new_master_event.wait(timeout=NEW_MASTER_WAIT)
        if not received:
            print("[ELECTION] Vencedor não anunciou. Reiniciando eleição…")
            threading.Thread(target=start_election_broadcast, daemon=True).start()


# ── Ações de quem vira master ─────────────────────────────────────────────────

def _become_master():
    """Atualiza o estado local e lança servidor.py como subprocesso."""
    global is_master, master_proc

    with state_lock:
        is_master = True
        current_master.update({
            "uuid": WORKER_UUID,
            "ip":   WORKER_HOST,
            "port": WORKER_PORT,
        })

    print(f"[ELECTION] ★ {WORKER_UUID} é o novo MASTER ({WORKER_HOST}:{WORKER_PORT}) ★")

    env = os.environ.copy()
    env.update({
        "MASTER_IP":   WORKER_HOST,
        "MASTER_PORT": str(WORKER_PORT),
        "SERVER_UUID": WORKER_UUID,
    })
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "servidor.py")

    if os.path.exists(script):
        try:
            master_proc = subprocess.Popen(
                ["python", script], env=env,
                cwd=os.path.dirname(script),
            )
            print(f"[ELECTION] servidor.py iniciado (PID {master_proc.pid})")
        except Exception as exc:
            print(f"[ELECTION] Falha ao iniciar servidor.py: {exc}")
    else:
        print(f"[ELECTION] AVISO: servidor.py não encontrado em {script}")


def _notify_new_master():
    """Notifica a rede via UDP broadcast sobre o novo master."""
    payload = {
        "TASK":              "NEW_MASTER",
        "MASTER_HOST":       WORKER_HOST,
        "MASTER_PORT":       WORKER_PORT,
        "MASTER_UUID":       WORKER_UUID,
        "MASTER_FREE_SPACE": get_free_space(),
    }
    # [LEGADO] Notificação TCP unicast por peers conhecidos (desativado)
    # for h, p in get_peers():
    #     print(f"[ELECTION] Notificando {h}:{p}")
    #     send_tcp(h, p, payload, timeout=3)

    # Broadcast alcança todos os workers sem conhecimento prévio
    send_udp(payload)
    print("[ELECTION] NEW_MASTER enviado via broadcast.")


def _accept_new_master(host, port, uuid, free_space=0):
    """
    Processa anúncio NEW_MASTER recebido de outro nó.
    Pode ser chamado de qualquer thread; não segura state_lock ao entrar.
    """
    global is_master, election_in_progress, failed_hb

    if not (host and port and uuid):
        return

    print(f"[ELECTION] ✓ Novo master aceito: {uuid} em {host}:{port}")

    with state_lock:
        if is_master and uuid != WORKER_UUID:
            print("[ELECTION] Este nó cede o título de master.")
            is_master = False
        election_in_progress = False
        failed_hb = 0
        current_master.update({
            "uuid": uuid,
            "ip":   host,
            "port": int(port),
        })

    new_master_event.set()   # desbloqueia quem estiver em start_election


# ── Servidor TCP de status ────────────────────────────────────────────────────

def _handle_conn(conn, addr):
    buf = ""
    try:
        while True:
            data = conn.recv(4096).decode()
            if not data:
                break
            buf += data
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue

                task = payload.get("TASK")

                if task == "HEARTBEAT":
                    with state_lock:
                        am_master = is_master
                    conn.sendall((json.dumps({
                        "SERVER_UUID": WORKER_UUID,
                        "TASK":        "HEARTBEAT",
                        "RESPONSE":    "ALIVE" if am_master else "NOT_MASTER",
                    }) + "\n").encode())

                elif task == "WORKER_STATUS":
                    conn.sendall((json.dumps({
                        "TASK":        "WORKER_STATUS_RESPONSE",
                        "WORKER_UUID": WORKER_UUID,
                        "WORKER_HOST": WORKER_HOST,
                        "WORKER_PORT": WORKER_PORT,
                        "FREE_SPACE":  get_free_space(),
                    }) + "\n").encode())

                elif task == "NEW_MASTER":
                    _accept_new_master(
                        payload.get("MASTER_HOST"),
                        payload.get("MASTER_PORT"),
                        payload.get("MASTER_UUID"),
                        payload.get("MASTER_FREE_SPACE", 0),
                    )
                    conn.sendall((json.dumps({
                        "TASK": "NEW_MASTER_ACK", "RESPONSE": "RECEIVED",
                    }) + "\n").encode())

                else:
                    conn.sendall((json.dumps({
                        "TASK": "ERROR", "RESPONSE": "UNKNOWN_TASK",
                    }) + "\n").encode())

    except Exception as exc:
        print(f"[STATUS] Erro em {addr}: {exc}")
    finally:
        conn.close()


def _start_status_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((WORKER_HOST, WORKER_PORT))
    srv.listen()
    print(f"[STATUS] Servidor TCP em {WORKER_HOST}:{WORKER_PORT}")
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=_handle_conn, args=(conn, addr),
                         daemon=True).start()


# ── Listener UDP (discovery + NEW_MASTER via broadcast) ──────────────────────

def _start_udp_listener():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", WORKER_PORT))
        print(f"[DISCOVERY] Listener UDP em porta {WORKER_PORT}")
    except Exception as exc:
        print(f"[DISCOVERY] Falha ao abrir UDP: {exc}")
        return

    while True:
        try:
            data, addr = sock.recvfrom(4096)
            payload = json.loads(data.decode())
        except Exception:
            continue

        task = payload.get("TASK")

        if task == "ELECTION_BROADCAST":
            # Worker responde com seus dados para o candidato calcular o vencedor
            sender_uuid = payload.get("WORKER_UUID")
            if sender_uuid == WORKER_UUID:
                continue  # ignora eco local
            print(f"[ELECTION] ELECTION_BROADCAST de {addr} (UUID={sender_uuid})")
            sock.sendto(json.dumps({
                "TASK":        "ELECTION_RESPONSE",
                "WORKER_UUID": WORKER_UUID,
                "WORKER_HOST": WORKER_HOST,
                "WORKER_PORT": WORKER_PORT,
                "FREE_SPACE":  get_free_space(),
            }).encode(), addr)

        elif task == "DISCOVER_WORKER":
            if payload.get("WORKER_UUID") == WORKER_UUID:
                continue
            sock.sendto(json.dumps({
                "TASK":        "DISCOVER_RESPONSE",
                "WORKER_UUID": WORKER_UUID,
                "WORKER_HOST": WORKER_HOST,
                "WORKER_PORT": WORKER_PORT,
            }).encode(), addr)

        elif task == "NEW_MASTER":
            _accept_new_master(
                payload.get("MASTER_HOST"),
                payload.get("MASTER_PORT"),
                payload.get("MASTER_UUID"),
                payload.get("MASTER_FREE_SPACE", 0),
            )


# ── Eleição com delay ────────────────────────────────────────────────────────

def _trigger_election_with_delay():
    """
    Aguarda ELECTION_DELAY segundos antes de iniciar a eleição via broadcast.
    Permite que um novo master já eleito anuncie-se antes de outro iniciar a eleição.
    Cancela se new_master_event for sinalizado durante a espera.
    """
    global election_in_progress
    print(f"[HEARTBEAT] Aguardando {ELECTION_DELAY}s antes de iniciar eleição…")
    cancelled = new_master_event.wait(timeout=ELECTION_DELAY)
    if cancelled:
        print("[HEARTBEAT] Novo master anunciado durante espera — eleição cancelada.")
        with state_lock:
            election_in_progress = False
        return
    # Reseta o flag para que start_election_broadcast possa prosseguir
    # (ele mesmo o re-seta para True internamente)
    with state_lock:
        election_in_progress = False
    start_election_broadcast()


# ── Heartbeat ─────────────────────────────────────────────────────────────────

def enviar_heartbeat():
    global failed_hb, election_in_progress

    with state_lock:
        if is_master or election_in_progress:
            return
        master_ip   = current_master["ip"]
        master_port = current_master["port"]

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)

    try:
        sock.connect((master_ip, master_port))
        print(f"[HEARTBEAT] Conectado ao master {master_ip}:{master_port}")
        # Inclui host+port para o master registrar este worker
        sock.sendall((json.dumps({
            "WORKER_UUID": WORKER_UUID,
            "WORKER_HOST": WORKER_HOST,
            "WORKER_PORT": WORKER_PORT,
            "TASK":        "HEARTBEAT",
        }) + "\n").encode())

        buf  = ""
        data = sock.recv(4096).decode()
        if not data:
            raise ConnectionError("Sem resposta.")

        buf += data
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            try:
                resp = json.loads(line)
            except Exception:
                continue

            r = resp.get("RESPONSE")
            print(f"[HEARTBEAT] Resposta: {r}")

            if r == "ALIVE":
                with state_lock:
                    failed_hb = 0
                    election_in_progress = False
                # [LEGADO] Peers vinham do master — desativado (workers não conhecem peers)
                # peers_from_master = resp.get("PEERS", [])
                # if peers_from_master:
                #     _update_known_peers(peers_from_master)

            elif r == "NOT_MASTER":
                print("[HEARTBEAT] Nó respondeu NOT_MASTER → iniciando eleição.")
                with state_lock:
                    already   = election_in_progress
                    failed_hb = HEARTBEAT_THRESHOLD
                if not already:
                    with state_lock:
                        election_in_progress = True
                    threading.Thread(target=_trigger_election_with_delay, daemon=True).start()

    except (ConnectionRefusedError, socket.timeout, ConnectionError, OSError) as exc:
        with state_lock:
            failed_hb += 1
            count     = failed_hb
            already   = election_in_progress
        print(f"[HEARTBEAT] Falha {count}/{HEARTBEAT_THRESHOLD} "
              f"com {master_ip}:{master_port} — {exc}")
        if count >= HEARTBEAT_THRESHOLD and not already:
            with state_lock:
                election_in_progress = True   # reserva antes de lançar a thread
            threading.Thread(target=_trigger_election_with_delay, daemon=True).start()

    except Exception as exc:
        print(f"[HEARTBEAT] Erro inesperado: {exc}")
    finally:
        sock.close()
        print("[HEARTBEAT] Encerrado.\n")


# ── Ciclo de tarefas (Sprint 2) ──────────────────────────────────────────────

def pedir_tarefa():
    """
    Ciclo completo de tarefa conforme Sprint 2:
      1. Worker se apresenta ao master (Payload 2.1 ou 2.1b se emprestado)
      2. Master responde QUERY ou NO_TASK
      3. Se QUERY: worker processa e reporta STATUS (Payload 2.4)
      4. Master confirma com ACK (Payload 2.5)
    """
    import random

    with state_lock:
        if is_master or election_in_progress:
            return   # master não pede tarefas / eleição em andamento
        master_ip   = current_master["ip"]
        master_port = current_master["port"]
        master_uuid = current_master.get("uuid", "")

    # Payload 2.1 — apresentação
    payload = {
        "WORKER":      "ALIVE",
        "WORKER_UUID": WORKER_UUID,
    }

    # Payload 2.1b — "Emprestado": master atual é diferente do original
    if ORIGINAL_SERVER_UUID and master_uuid != ORIGINAL_SERVER_UUID:
        payload["SERVER_UUID"] = ORIGINAL_SERVER_UUID
        print(f"[TAREFA] Modo EMPRESTADO (master original: {ORIGINAL_SERVER_UUID})")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)

    try:
        sock.connect((master_ip, master_port))
        print(f"[TAREFA] Conectado ao master {master_ip}:{master_port}")

        # Passo 1: envia apresentação
        sock.sendall((json.dumps(payload) + "\n").encode())

        buf = ""
        # Passo 2: aguarda QUERY ou NO_TASK
        while True:
            try:
                chunk = sock.recv(4096).decode()
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            if "\n" not in buf:
                continue

            line, buf = buf.split("\n", 1)
            try:
                resp = json.loads(line)
            except Exception:
                continue

            task_type = resp.get("TASK")

            # Payload 2.3 — sem tarefas
            if task_type == "NO_TASK":
                print("[TAREFA] Nenhuma tarefa disponível no momento.")
                break

            # Payload 2.2 — tarefa recebida
            elif task_type == "QUERY":
                user    = resp.get("USER", "?")
                task_id = resp.get("TASK_ID", "?")
                print(f"[TAREFA] Recebida | ID={task_id} | USER={user} | Processando...")

                # Passo 3: simula processamento (1–4 segundos)
                sleep_time = random.uniform(1, 4)
                time.sleep(sleep_time)

                # Determina resultado (90% OK, 10% NOK)
                status = "OK" if random.random() < 0.9 else "NOK"

                # Payload 2.4 — reporta resultado
                result_payload = {
                    "STATUS":      status,
                    "TASK":        "QUERY",
                    "WORKER_UUID": WORKER_UUID,
                }
                # Inclui SERVER_UUID se emprestado
                if ORIGINAL_SERVER_UUID and master_uuid != ORIGINAL_SERVER_UUID:
                    result_payload["SERVER_UUID"] = ORIGINAL_SERVER_UUID

                sock.sendall((json.dumps(result_payload) + "\n").encode())
                print(f"[TAREFA] Resultado enviado: {status} (processado em {sleep_time:.1f}s)")

                # Passo 4: aguarda ACK (Payload 2.5)
                try:
                    ack_data = sock.recv(4096).decode()
                    buf += ack_data
                    while "\n" in buf:
                        ack_line, buf = buf.split("\n", 1)
                        try:
                            ack = json.loads(ack_line)
                            if ack.get("STATUS") == "ACK":
                                print(f"[TAREFA] ACK recebido. Tarefa {task_id} concluída.")
                        except Exception:
                            pass
                except socket.timeout:
                    print("[TAREFA] Timeout aguardando ACK.")
                break

    except (ConnectionRefusedError, socket.timeout, OSError) as exc:
        print(f"[TAREFA] Falha ao conectar ao master: {exc}")
    except Exception as exc:
        print(f"[TAREFA] Erro inesperado: {exc}")
    finally:
        sock.close()
        print("[TAREFA] Conexão encerrada.\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def start_worker():
    threading.Thread(target=_start_udp_listener, daemon=True).start()
    threading.Thread(target=_start_status_server, daemon=True).start()

    time.sleep(1)   # aguarda servidores subirem

    # ── Descoberta do master via broadcast UDP ────────────────────────────────
    print("[WORKER] Buscando master via broadcast UDP...")
    result = discover_master(retries=5, timeout=3.0)
    if result is None:
        print("[WORKER] ERRO: Não foi possível encontrar o master na rede.")
        print("[WORKER] Verifique se o servidor está rodando e acessível via broadcast.")
        raise SystemExit(1)

    discovered_ip, discovered_port = result
    with state_lock:
        current_master["ip"]   = discovered_ip
        current_master["port"] = discovered_port
    print(f"[WORKER] Master configurado: {discovered_ip}:{discovered_port}")
    # ─────────────────────────────────────────────────────────────────────────

    enviar_heartbeat()
    schedule.every(HEARTBEAT_INTERVAL).seconds.do(enviar_heartbeat)

    pedir_tarefa()
    schedule.every(TASK_INTERVAL).seconds.do(pedir_tarefa)

    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[ENCERRANDO] Worker desligado.")


if __name__ == "__main__":
    start_worker()
