# Развёртывание общедоступного стенда

Реестр ЕИС отвечает на запросы из России, поэтому стенд размещается на сервере в российском дата-центре
(Yandex Cloud, Selectel, Timeweb Cloud и др.) или на компьютере в России.

## Сервер с постоянным адресом

Подойдёт виртуальная машина Ubuntu 22.04 или 24.04: 2 vCPU, 2 ГБ памяти, 15 ГБ диска, публичный IPv4, открытые
порты 80 и 443.

```bash
curl -fsSL https://get.docker.com | sh
git clone <адрес репозитория> nmcd-analyst && cd nmcd-analyst
cp .env.example .env        # впишите ключи модели и поискового API
export NMCD_DOMAIN=<IP через дефисы>.sslip.io      # например 84-201-12-34.sslip.io, или свой домен
docker compose -f docker-compose.yml -f deploy/docker-compose.public.yml up -d --build
```

Через минуту стенд открывается по адресу `https://$NMCD_DOMAIN`: Caddy сам получает сертификат Let's Encrypt.
Режим стенда (`NMCD_PUBLIC=1`): на главной каждый посетитель видит только свои расчёты, одновременно идёт
не больше четырёх расчётов (`NMCD_MAX_QUEUE`), модель тратит не больше 2 млн токенов в сутки
(`LLM_DAILY_TOKEN_BUDGET`), после этого расчёт идёт по правилам.

Обновление: `git pull`, затем та же команда `docker compose … up -d --build`. Остановка: `docker compose -f
docker-compose.yml -f deploy/docker-compose.public.yml down`.

## Временный адрес с любого компьютера

```bash
./deploy/tunnel.sh
```

Скрипт запускает сервис в режиме стенда и туннель Cloudflare без регистрации; адрес
`https://<слова>.trycloudflare.com` появится в выводе. Он действует, пока скрипт работает и компьютер включён,
и меняется при каждом запуске.
