# Ansible

Идемпотентный bootstrap хоста под MVP.

## Запуск (локально / RedOS)

```bash
cd /opt/ai-trading-bot/ansible
ansible-playbook playbooks/site.yml
```

Roles:
- `docker_host` — docker service
- `trading_bot` — dirs, secrets mode, `docker compose up -d redis timescaledb`

Секреты не копируются в git: ожидается `~/.config/trading-bot/binance_testnet.env`.
