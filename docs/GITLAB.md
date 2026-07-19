# Git hosting / CI

Remote сейчас: **GitHub** `git@github.com:mikrozz/ai-trading-bot.git`  
Репозиторий: https://github.com/mikrozz/ai-trading-bot

CI на GitHub: [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) — lint → test → build (Docker).  
[`.gitlab-ci.yml`](../.gitlab-ci.yml) оставлен на случай переноса на GitLab.

## Если переносите на GitLab

Создайте пустой проект (без README), затем:

```bash
cd /opt/ai-trading-bot
git remote set-url origin git@YOUR_GITLAB_HOST:GROUP/ai-trading-bot.git
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
