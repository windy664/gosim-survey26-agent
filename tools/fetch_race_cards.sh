#!/usr/bin/env bash
# 正式赛开赛后抓取 A–D 卡公开输入（scenarios bucket 会出现新前缀）。
# 用法: tools/fetch_race_cards.sh [轮询次数]   默认一次性；加 --loop 每 60s 重试直到出现新卡
set -u
SUPA="https://vdiemcofukuxglqsmlyz.supabase.co"
KEY_FILE="${KEY_FILE:-/tmp/gosim-anonkey.txt}"
[ -f "$KEY_FILE" ] || { echo "缺 anon key: $KEY_FILE"; exit 1; }
KEY=$(cat "$KEY_FILE")
OUT="$(dirname "$0")/../kit/cloud-cards"
mkdir -p "$OUT"

list() {
  curl -s -m 15 -X POST "$SUPA/storage/v1/object/list/scenarios" \
    -H "apikey: $KEY" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d "{\"prefix\":\"$1\",\"limit\":200}"
}

KNOWN="dev-fortnight dev-reference v4-practice-alpha v4-practice-beta v4-practice-gamma v4-practice-delta v4-public-test"
try=0
while :; do
  try=$((try+1))
  new_cards=""
  for name in $(list "" | python3 -c "import json,sys;[print(x['name']) for x in json.load(sys.stdin)]" 2>/dev/null); do
    skip=0
    for k in $KNOWN; do [ "$name" = "$k" ] && skip=1; done
    [ $skip -eq 0 ] && new_cards="$new_cards $name"
  done
  if [ -n "$new_cards" ]; then
    echo "发现新卡:$new_cards"
    for card in $new_cards; do
      for sub in config public; do
        mkdir -p "$OUT/$card/$sub"
        for f in $(list "$card/$sub/" | python3 -c "import json,sys;[print(x['name']) for x in json.load(sys.stdin) if not x['name'].endswith('/')]" 2>/dev/null); do
          curl -s -m 30 "$SUPA/storage/v1/object/authenticated/scenarios/$card/$sub/$f" \
            -H "apikey: $KEY" -H "Authorization: Bearer $KEY" -o "$OUT/$card/$sub/$f"
          echo "  $card/$sub/$f $(wc -c < "$OUT/$card/$sub/$f") bytes"
        done
      done
    done
    exit 0
  fi
  [ "${1:-}" = "--loop" ] || { echo "暂无新卡（第 $try 次）"; exit 0; }
  sleep 60
done
