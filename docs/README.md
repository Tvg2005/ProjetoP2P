# 📚 ProjetoP2P — Documentação

Documentação completa do sistema P2P distribuído com eleição automática de master via broadcast UDP.

## Índice

| Documento | Descrição |
|---|---|
| [architecture.md](./architecture.md) | Visão geral da arquitetura, componentes e fluxos de comunicação |
| [planning.md](./planning.md) | Planning de implementação: requisitos, decisões de design e sprints |
| [implementation.md](./implementation.md) | Como cada feature foi implementada (código e raciocínio) |
| [protocol.md](./protocol.md) | Protocolo de mensagens JSON entre nós |
| [configuration.md](./configuration.md) | Referência completa do arquivo `.env` |

---

## TL;DR — Como o sistema funciona

```
servidor.py   →  Master: recebe heartbeats, distribui tarefas, responde a broadcasts
cliente.py    →  Worker: envia heartbeats, executa tarefas, elege novo master se necessário
.env          →  Configuração local de cada máquina
```

1. **Servidor** sobe na máquina master. Workers descobrem o IP do master via **broadcast UDP** (sem IP fixo no `.env`).
2. **Workers** enviam heartbeat periódico. Se o master não responder por `HEARTBEAT_THRESHOLD` vezes seguidas, detectam falha.
3. Após `ELECTION_DELAY` segundos, todos os workers enviam **ELECTION_BROADCAST** na subrede.
4. Cada worker coleta as respostas, calcula o vencedor deterministicamente e o vencedor vira o novo master.
5. O novo master anuncia via **NEW_MASTER broadcast** e todos os workers se reconectam.
