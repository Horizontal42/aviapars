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

## Обновление

```bash
cd /opt/aviapars
git pull
bash deploy/deploy.sh
```
