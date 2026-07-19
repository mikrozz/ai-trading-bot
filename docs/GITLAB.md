# GitLab

CI уже в репозитории: [`.gitlab-ci.yml`](../.gitlab-ci.yml) — stages `lint` → `test` → `build`.

## Подключить remote

На GitLab создайте пустой проект (без README), затем:

```bash
cd /opt/ai-trading-bot
git remote add origin git@YOUR_GITLAB_HOST:GROUP/ai-trading-bot.git
# или HTTPS:
# git remote add origin https://YOUR_GITLAB_HOST/GROUP/ai-trading-bot.git

git push -u origin main
```

После первого push pipeline должен прогнать lint/test/build.

## Runner

Нужен shared/group runner с Docker executor (image python:3.11-slim доступен).  
Секреты Binance **не** класть в CI variables для lint/test — тесты оффлайн.

## Deploy stage

Manual production deploy — следующий этап (после staging host). Сейчас deploy через Ansible локально:

```bash
cd /opt/ai-trading-bot/ansible && ansible-playbook playbooks/site.yml
```
