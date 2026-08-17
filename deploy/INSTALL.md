# Установка на VDS (Ubuntu 24.04)

## Первичная настройка

```bash
sudo useradd -r -s /usr/sbin/nologin aviapars
sudo mkdir -p /opt/aviapars /var/lib/aviapars
sudo chown aviapars:aviapars /var/lib/aviapars

sudo git clone <repo-url> /opt/aviapars
cd /opt/aviapars
cp .env.example .env        # заполнить секреты
cp config.example.yaml config.yaml   # при необходимости подправить лимиты

sudo cp deploy/aviapars.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable aviapars

bash deploy/deploy.sh
```

## Своп (обязательно на 1GB VDS)

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
sudo sysctl vm.swappiness=10
echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.conf
```

## Аварийная остановка (только владелец)

Бот слушает `/stop` (admin-only, см. §11 плана) — команда доступна только `ADMIN_TG_ID` из `.env`.
Юнит использует `Restart=on-failure`, поэтому чистая остановка через `/stop` не перезапускается
автоматически. Вернуть в строй после `/stop`:

```bash
ssh <vds-through-tailscale>
sudo systemctl start aviapars
```

Tailscale/SSH работают независимо от процесса бота — cgroup-лимиты (`MemoryMax`/`CPUWeight`
в `aviapars.service`) не дают боту исчерпать ресурсы всего сервера.

## Прокси для Telegram (нужен на VDS в РФ — `api.telegram.org` заблокирован)

Только сессия бота идёт через прокси; все адаптеры-источники остаются на прямом соединении
(см. `ARCHITECTURE.md`). Прокси — локальный SOCKS5, поднятый `xray-core` поверх VLESS Reality
(или любого другого туннеля, который отдаёт SOCKS5/HTTP на localhost):

```bash
bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
```

`/usr/local/etc/xray/config.json` — inbound `socks` на `127.0.0.1:<port>`, outbound —
параметры вашего VLESS/Reality-сервера (сервер, uuid, publicKey, shortId, sni и т.д. — из
подписки провайдера). Затем:

```bash
sudo systemctl enable --now xray
curl -x socks5h://127.0.0.1:<port> https://api.telegram.org   # должен ответить, не таймаутить
```

В `config.yaml` бота:

```yaml
telegram_proxy_url: socks5://127.0.0.1:<port>
```

Перезапустить бота (`systemctl restart aviapars`) — `telegram_proxy_url: null` (по умолчанию)
означает прямое соединение, без прокси.

## Обновление

```bash
cd /opt/aviapars
git pull
bash deploy/deploy.sh
```
