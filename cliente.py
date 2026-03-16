import socket
import json
import time
import schedule

# Configurações do Master alvo
MASTER_IP = '10.62.206.35'
MASTER_PORT = 5000
WORKER_UUID = "WRK-01-ALPHA"

def enviar_heartbeat():
    """Função que conecta, envia o payload, recebe a resposta e desconecta."""
    # 1. Criação do Socket (sempre um novo a cada chamada)
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    
    try:
        # 2. Conecta ao Master
        client_socket.connect((MASTER_IP, MASTER_PORT))
        print(f"[CONECTADO] Conectado ao Master {MASTER_IP}:{MASTER_PORT}")
        
        # 3. Prepara e envia o Payload
        payload_envio = {
            "SERVER_UUID": "SRV-MASTER",
            "WORKER_UUID": WORKER_UUID,
            "TASK": "HEARTBEAT"
        }
        
        mensagem_envio = json.dumps(payload_envio) + "\n"
        client_socket.send(mensagem_envio.encode('utf-8'))
        print("[ENVIADO] Heartbeat enviado.")
        
        # 4. Recebe a resposta do Master
        buffer = ""
        data = client_socket.recv(1024).decode('utf-8')
        
        if data:
            buffer += data
            while '\n' in buffer:
                mensagem_str, buffer = buffer.split('\n', 1)
                try:
                    payload_resposta = json.loads(mensagem_str)
                    print(f"[RECEBIDO] Resposta do Master: {payload_resposta.get('RESPONSE')}")
                except json.JSONDecodeError:
                    print("Erro ao decodificar JSON do Master.")
        else:
             print("[AVISO] Nenhuma resposta recebida (Master encerrou a conexão precocemente).")

    except ConnectionRefusedError:
        print("[ERRO] Não foi possível conectar ao Master. Ele está online?")
    except Exception as e:
        print(f"[ERRO] Falha na comunicação: {e}")
    finally:
        # 5. Encerramento (Garante que a conexão será fechada após o ciclo)
        client_socket.close()
        print("[DESCONECTADO] Conexão encerrada.\n")

def start_worker():
    print("Iniciando o Worker com agendamento (Schedule)...")
    
    # Executa a primeira vez imediatamente (opcional)
    enviar_heartbeat()
    
    # Agenda a execução da função a cada 5 segundos
    schedule.every(5).seconds.do(enviar_heartbeat)
    
    try:
        # Loop principal apenas para manter o agendador rodando
        while True:
            schedule.run_pending()
            time.sleep(1) # Sleep curto para não consumir muita CPU
    except KeyboardInterrupt:
        print("\n[ENCERRANDO] Worker desligado pelo usuário.")

if __name__ == "__main__":
    start_worker()