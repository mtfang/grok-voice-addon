#!/bin/bash
# Acceptance trials (box): story under simulated device echo + noise, a different voice cuts in.
# usage: barge_trials.sh <label> <barge_wav> <gain_db> <n> [echo_db]
cd "$(dirname "$0")/results"
TOK=$(python3 -c "import json;print(json.load(open('../local/options.box.json')).get('client_token',''))")
P=/workspace/grok-voice-agent/.venv/bin/python; A=/workspace/grok-voice-agent/test_audio
label=$1; bw=$2; gain=$3; n=$4; edb=${5:-2}
for i in $(seq 1 $n); do
  after=$(python3 -c "import random;print(round(random.uniform(3,7),1))")
  ECHO_DB=$edb ECHO_CLIP_DBFS=${ECHO_CLIP_DBFS:-} NOISE_DBFS=-28 NOISE_COLOR=pink BARGE_WAV=$bw BARGE_GAIN_DB=$gain BARGE_AFTER_S=$after MAX_S=${MAX_S:-18} \
    timeout 40 $P ../test_client.py 127.0.0.1 10700 trial_${label}_$i $A/cmd_long.wav "$TOK" > /tmp/trial_${label}_$i.out 2>&1
  python3 - "$label" "$i" <<'PY'
import json,sys
r=json.load(open(f"trial_{sys.argv[1]}_{sys.argv[2]}.json"))
c=r.get("barge_clear_after_s"); si=r.get("self_interrupts")
print(f"{sys.argv[1]} #{sys.argv[2]}: clear_after={c} self_interrupts={si} pass={c is not None and c<=0.6 and si==0}", flush=True)
PY
done
