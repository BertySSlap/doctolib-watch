#!/data/data/com.termux/files/usr/bin/sh
cd "$HOME/doctolib-watch" || exit 1

while true; do
  python surveille_maiia.py >> maiia_service.log 2>&1
  status=$?
  [ "$status" -eq 2 ] && exit 0
  sleep 300
done
