# Planning da Implementação

## Contexto e Requisitos

### Objetivo
Implementar um sistema P2P distribuído onde:
- Workers se conectam a um master central
- O master distribui tarefas para os workers
- Se o master falhar, os workers elegem automaticamente um novo master
- Nenhum IP fixo é necessário — descoberta automática via broadcast

### Requisitos Funcionais
| # | Requisito | Sprint |
|---|---|---|
| RF-01 | Workers enviam heartbeat periódico ao master | Sprint 1 |
| RF-02 | Master registra workers que enviam heartbeat | Sprint 1 |
| RF-03 | Workers detectam falha do master por heartbeats consecutivos sem resposta | Sprint 1 |
| RF-04 | Workers elegem novo master de forma automática e determinística | Sprint 1 |
| RF-05 | Novo master notifica todos os outros workers | Sprint 1 |
| RF-06 | Master distribui tarefas da fila FIFO para workers | Sprint 2 |
| RF-07 | Workers reportam resultado (OK/NOK) e master confirma com ACK | Sprint 2 |
| RF-08 | Worker "Emprestado" identifica master original no payload | Sprint 2 |
| RF-09 | IP do master descoberto via broadcast UDP (sem IP fixo) | Sprint 2.1 |
| RF-10 | Eleição via broadcast UDP puro (sem peers pré-configurados) | Sprint 2.1 |

### Requisitos Não-Funcionais
- Tolerância a falhas: o sistema continua operando após queda do master
- Determinismo: todos os nós chegam ao mesmo vencedor sem comunicação extra
- Configuração mínima: apenas UUID e portas no `.env`, IPs descobertos automaticamente
- Compatibilidade com redes corporativas/universitárias (broadcast `/24`, não `255.255.255.255`)

---

## Sprints e Iterações

### Sprint 1 — Infraestrutura Base e Eleição TCP

**Objetivo:** Estabelecer comunicação heartbeat e eleição de master.

**Decisões de design tomadas:**

#### Eleição: Por que Bully Algorithm adaptado?
O Bully Algorithm é ideal para este cenário porque:
1. **Determinístico**: a chave de ordenação `(-free_space, uuid)` é calculada localmente por cada nó com os mesmos dados → mesmo resultado sem votação
2. **Sem coordenador central**: não depende de um nó externo para desempatar
3. **Simples de implementar**: sem estado distribuído complexo, sem Raft/Paxos

**Critério de eleição escolhido:**
- Mais espaço livre em disco → assumimos que o nó com mais recursos pode assumir o papel de master
- UUID lexicográfico → desempate estável e previsível

#### Descoberta de peers: Via master
- O master registra workers ao receber heartbeats e devolve a lista de peers na resposta
- Vantagem: nenhuma configuração manual de IPs de outros workers
- Desvantagem: workers não se conhecem se o master nunca esteve disponível

#### Comunicação: TCP puro
- Heartbeat via TCP: confiável, detecta desconexão
- Eleição via TCP: mensagens `WORKER_STATUS` unicast para peers conhecidos
- Motivo: UDP broadcast era bloqueado por firewalls em testes iniciais

**Itens entregues:**
- `servidor.py` com registro de workers, heartbeat e fila de tarefas
- `cliente.py` com heartbeat, eleição Bully, `_start_status_server`, `_start_udp_listener`
- `known_peers` populado via resposta `ALIVE` do master

---

### Sprint 2 — Ciclo de Tarefas

**Objetivo:** Implementar distribuição de tarefas master→worker com resultado e ACK.

**Protocolo de tarefa (5 payloads):**
```
2.1  Worker → Master : WORKER ALIVE (apresentação)
2.1b Worker → Master : WORKER ALIVE + SERVER_UUID (emprestado)
2.2  Master → Worker : TASK QUERY (tarefa)
2.3  Master → Worker : NO_TASK (fila vazia)
2.4  Worker → Master : STATUS OK/NOK (resultado)
2.5  Master → Worker : STATUS ACK (confirmação)
```

**Decisão: conexão TCP única por ciclo**
- A mesma conexão TCP aberta para a apresentação é reutilizada para toda a troca (2.1→2.2→2.4→2.5)
- Evita overhead de múltiplas conexões por tarefa

**Conceito "Emprestado":**
- Um worker conectado a um master eleito (diferente do original configurado no `.env`) é "emprestado"
- Identificado pelo campo `SERVER_UUID` no payload, que referencia o master original
- Permite ao master eleito saber de qual cluster cada worker veio

---

### Sprint 2.1 — Discovery via Broadcast e Eleição sem Peers

**Objetivo:** Eliminar IP fixo do `.env` e tornar a eleição independente de peers conhecidos.

#### Problema 1: IP fixo do master no `.env`

**Situação anterior:**
```env
MASTER_IP=10.62.202.48   ← precisava ser atualizado manualmente em toda troca
```

**Análise:** Em redes com DHCP ou múltiplos masters eleitos, o IP muda com frequência. Configurar manualmente é frágil.

**Solução escolhida: Broadcast UDP com resposta unicast**
```
Cliente ── FIND_MASTER {SERVER_UUID} ──► broadcast UDP :DISCOVERY_PORT
Servidor ◄─────────────────────────────────────────────────────────────
Servidor ── MASTER_FOUND {ip, port} ──► unicast para o remetente
```

Por que UDP e não TCP?
- UDP permite broadcast nativo
- O servidor não precisa saber o IP do cliente antecipadamente
- O overhead é mínimo (apenas na inicialização)

Por que `SERVER_UUID` no request?
- Permite múltiplos masters na mesma rede com UUIDs diferentes
- O servidor só responde se o UUID bater (ou se a request for genérica)

**Porta de discovery separada (`MASTER_PORT + 1 = 8001`):**
- Isola o tráfego de discovery do tráfego de operação
- Evita conflito com o listener UDP dos workers (que usa `WORKER_PORT`)

#### Problema 2: Broadcast `255.255.255.255` bloqueado

**Diagnóstico:** Workers e servidor na rede `10.62.206.x` não se encontravam via `255.255.255.255`.

**Causa raiz:** `255.255.255.255` é o "limited broadcast" — roteadores e switches corporativos tipicamente bloqueiam esse tráfego por segurança. O correto é o "directed broadcast" da subrede.

**Solução: Auto-detecção do broadcast `/24`**
```python
def _detect_broadcast(local_ip: str) -> str:
    # 10.62.206.23 → 10.62.206.255
    prefix = ".".join(local_ip.split(".")[:3])
    return f"{prefix}.255"
```

Regras de prioridade:
1. `WORKER_BROADCAST_ADDRESS` definido no `.env` (e ≠ `255.255.255.255`) → usa o manual
2. Caso contrário → auto-detecta `/24` a partir do IP local

**Assunção `/24`:** Cobre ~99% dos ambientes LAN e redes universitárias. Para redes com prefixo diferente, o usuário pode sobrescrever via `.env`.

#### Problema 3: Eleição depende de peers conhecidos (que vêm do master)

**Situação anterior:**
- Workers conheciam peers via heartbeat do master (`PEERS` na resposta `ALIVE`)
- Eleição consultava esses peers via TCP `WORKER_STATUS`
- **Problema:** Se o master cai antes de distribuir os peers, os workers não se conhecem e não conseguem eleger

**Solução: Eleição via ELECTION_BROADCAST UDP**

Filosofia: workers não precisam conhecer uns aos outros enquanto o master está ativo. Ao detectar falha, todos fazem broadcast simultâneo e se auto-descobrem naquele momento.

```
Todos detectam master inativo
        ↓
[ELECTION_DELAY = 30s] — janela para cancelamento se NEW_MASTER chegar
        ↓
Cada worker abre socket UDP broadcast
Envia ELECTION_BROADCAST {uuid, host, port, free_space}
        ↓
Cada worker responde ELECTION_RESPONSE para o remetente (unicast)
        ↓
Cada worker coleta respostas por ELECTION_COLLECT_TIMEOUT segundos
        ↓
Ordena candidatos (inclui si mesmo) → mesmo vencedor em todos
        ↓
Vencedor → _become_master() + broadcast NEW_MASTER
Perdedor → aguarda NEW_MASTER (timeout → reinicia eleição)
```

**Por que ELECTION_DELAY de 30 segundos?**
- Após detectar falha do master, pode ser que um novo master já tenha sido eleito (por exemplo, eleição iniciada por outro worker que detectou primeiro)
- 30s é tempo suficiente para o NEW_MASTER chegar e cancelar a eleição desnecessária
- Configurável via `.env` para ambientes com latência diferente

**Mecanismo de resposta unicast:**
- O worker que envia ELECTION_BROADCAST abre um socket UDP em porta efêmera
- A resposta ELECTION_RESPONSE vai para a porta efêmera do remetente (não para o WORKER_PORT)
- Isso evita conflito com o listener UDP permanente (que está no WORKER_PORT)

---

## Decisões de Design — Resumo

| Decisão | Alternativa Considerada | Motivo da Escolha |
|---|---|---|
| Bully Algorithm | Raft, PBFT, Paxos | Simples, determinístico, sem estado distribuído |
| Broadcast UDP para discovery | DNS, serviço de nomes, multicast | Zero configuração, funciona em redes locais |
| Broadcast `/24` auto-detectado | `255.255.255.255` | `255.255.255.255` bloqueado em redes corporativas |
| ELECTION_DELAY antes da eleição | Eleição imediata | Evita eleições desnecessárias se NEW_MASTER já vem |
| Conexão TCP única por tarefa | Nova conexão por mensagem | Menos overhead, fluxo mais simples |
| `SERVER_UUID` como identidade | IP fixo | IP muda; UUID é estável e semântico |
| Subprocesso para `servidor.py` | Mesma thread | Isolamento limpo; servidor roda de forma independente |
