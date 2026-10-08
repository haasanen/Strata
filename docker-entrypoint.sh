#!/bin/sh
# Entry point for the Strata container. The engine is compiled during docker
# build and lives in the image, so the first start only downloads the model.
# The install config is kept on the /data volume so a recreated container skips
# the setup pass and goes straight to serving.
set -e
cd /opt/strata || exit 1

STRATA_DATA="${STRATA_DATA:-/data}"
FAMILY="${FAMILY:-qwen}"
MODEL="${MODEL:-IQ2_XS}"
CONTEXT="${CONTEXT:-32768}"
VISION="${VISION:-no}"          # no | yes | cpu (the image encoder on the CPU)
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"
API_KEY="${API_KEY:-}"
KV="${KV:-}"                    # int8 | q4_0 | k8v4; empty: setup.py's own default (int8)
GPUS="${GPUS:-}"                # "0,2" or "all": one model across several cards (docs/MULTI_GPU.md)
GPU="${GPU:-}"                  # one card, numbered as nvidia-smi numbers them
LAYER_SPLIT="${LAYER_SPLIT:-}"  # with GPUS: where each later card's layers start (default: auto)
LOW_RAM="${LOW_RAM:-auto}"      # on: the experts come from the pack's experts.bin, not from RAM
GGUF_DIR="${GGUF_DIR:-}"        # a mounted folder with GGUF files you already have: no download
RESIDENT_BUDGET_GIB="${RESIDENT_BUDGET_GIB:-}"   # UD-Q4_K_XL: GiB of experts kept in RAM (default: setup's pick)
KV_STREAMING="${KV_STREAMING:-}" # auto | on | off; empty: setup.py's own default (auto)
CONFIG="${CONFIG:-}"            # a config file to start with (wins over MODEL's /data/config/strata-<model>.json)

# setup.py starts the newest strata-*.json it finds, so link in exactly the one
# this family and model were set up with. The config is the recorded output of
# that setup (the pack, the profile, the quant, the KV decision), not settings
# the entry point could rebuild from env vars. qwen has an empty family tag.
#
# fork patch (/switch): drop configs left by an earlier start of this container
# (a restart keeps the filesystem): with two linked configs setup.py asks "Which
# one?" interactively, hits EOF in a container, and the restart policy loops it.
rm -f /opt/strata/strata-*.json
case "$FAMILY" in qwen) prefix="" ;; *) prefix="${FAMILY}-" ;; esac
tag="${prefix}$(printf '%s' "$MODEL" | tr 'A-Z' 'a-z')"
cfg="$STRATA_DATA/config/strata-$tag.json"
mkdir -p "$STRATA_DATA/config"

# fork patch (/switch): config/active_model overrides the MODEL/FAMILY env, so a
# restart comes up on the model the last switch chose. The file holds "<family>
# <model>", e.g. "unsloth UD-Q4_K_XL". Delete it to go back to the env settings.
if [ -f "$STRATA_DATA/config/active_model" ]; then
  read -r afam amod < "$STRATA_DATA/config/active_model" || true
  if [ -n "$afam" ] && [ -n "$amod" ]; then
    echo "active_model: $afam/$amod overrides MODEL=$MODEL FAMILY=$FAMILY"
    FAMILY="$afam"; MODEL="$amod"
    case "$FAMILY" in qwen) prefix="" ;; *) prefix="${FAMILY}-" ;; esac
    tag="${prefix}$(printf '%s' "$MODEL" | tr 'A-Z' 'a-z')"
    cfg="$STRATA_DATA/config/strata-$tag.json"
  fi
fi

# REINSTALL is only needed to change settings for a model that is already set up
# (context, vision, KV, host, api_key). Switching between models already on the
# volume needs no setup pass: their config is already there.
#
# KV / GPUS / GPU / LAYER_SPLIT are passed only when set, so an unset one keeps
# setup.py's own default. LOW_RAM is always passed: setup.py measures the PC's RAM
# from /proc/meminfo, which in a container is the host's total, not the container's
# limit, so a memory-capped container has to ask for the low-RAM mode itself.
#
# Which config the server starts with (#1244): CONFIG when set; else a link in /opt/strata that already points
# into $STRATA_DATA/config/ (a pod command that picked one by linking); else MODEL's strata-<model>.json.
link="/opt/strata/strata-$tag.json"
keep=""
if [ -n "$CONFIG" ]; then
  [ -f "$CONFIG" ] || { echo "CONFIG=$CONFIG does not exist." >&2; exit 1; }
elif [ "${REINSTALL:-0}" != "1" ] && [ -L "$link" ] && [ -f "$link" ]; then
  case "$(readlink "$link")" in "$STRATA_DATA"/config/*) keep=1 ;; esac
fi

if [ -n "$CONFIG" ]; then
  ln -sfn "$CONFIG" "$link"
  echo "Config: $CONFIG (from CONFIG)"
elif [ -n "$keep" ]; then
  echo "Config: $(readlink "$link") (existing link kept)"
elif [ "${REINSTALL:-0}" = "1" ] || [ ! -f "$cfg" ]; then
  if [ -n "$GGUF_DIR" ]; then
    echo "Setting up $tag from the GGUF files in $GGUF_DIR (the engine is already in the image)."
  else
    echo "Setting up $tag: downloading the model (~70 GB; the engine is already in the image)."
  fi
  set -- --family "$FAMILY" --model "$MODEL" --context "$CONTEXT" --vision "$VISION" \
    --data-dir "$STRATA_DATA" --host "$HOST" --api-key "$API_KEY" \
    --port "$PORT" --no-start --low-ram "$LOW_RAM"
  if [ -n "$KV" ]; then set -- "$@" --kv "$KV"; fi
  if [ -n "$GPUS" ]; then set -- "$@" --gpus "$GPUS"; fi
  if [ -n "$GPU" ]; then set -- "$@" --gpu "$GPU"; fi
  if [ -n "$LAYER_SPLIT" ]; then set -- "$@" --layer-split "$LAYER_SPLIT"; fi
  if [ -n "$GGUF_DIR" ]; then set -- "$@" --gguf-dir "$GGUF_DIR"; fi
  if [ -n "$RESIDENT_BUDGET_GIB" ]; then set -- "$@" --resident-budget-gib "$RESIDENT_BUDGET_GIB"; fi
  if [ -n "$KV_STREAMING" ]; then set -- "$@" --kv-streaming "$KV_STREAMING"; fi
  .venv/bin/python setup.py --setup --yes "$@"
  [ -e "/opt/strata/strata-$tag.json" ] && { cmp -s "/opt/strata/strata-$tag.json" "$cfg" || cp -f "/opt/strata/strata-$tag.json" "$cfg"; }
  echo "Config: $cfg (from MODEL $MODEL, just set up)"
else
  # #1244: the copy on the volume is the one that counts, so a regular file left in /opt/strata by an earlier setup
  # (or by an image built with one) must not stand in for it: edits to /data/config would be ignored
  ln -sfn "$cfg" "/opt/strata/strata-$tag.json"
  echo "Config: $cfg (from MODEL $MODEL)"
fi

# EXTRA_ENGINE_ARGS: engine arguments to keep in the config across starts and
# reinstalls (fork patch; upstream's setup rewrites the config and drops hand
# edits). Space-separated, e.g. "--prefill auto:32768 --conversation-cache-mib
# 8192". Each flag replaces the config's value for it, or is appended.
if [ -n "${EXTRA_ENGINE_ARGS:-}" ]; then
  .venv/bin/python - "$cfg" <<'PYEOF'
import json, sys, shlex
path = sys.argv[1]
extra = shlex.split(__import__("os").environ["EXTRA_ENGINE_ARGS"])
with open(path) as f:
    cfg = json.load(f)
args = cfg.setdefault("args", [])
# server-only options: the Python server reads them from the config JSON, the
# engine binary rejects them on its command line. Never leave them in args.
SERVER_ONLY = {"--fit-max-tokens"}
clean, j = [], 0
while j < len(args):
    if args[j] in SERVER_ONLY:
        j += 2 if j + 1 < len(args) and not args[j + 1].startswith("--") else 1
    else:
        clean.append(args[j]); j += 1
args[:] = clean
if __import__("os").environ.get("EXTRA_CONFIG_JSON"):
    cfg.update(json.loads(__import__("os").environ["EXTRA_CONFIG_JSON"]))
    print("EXTRA_CONFIG_JSON merged into", path)
i = 0
while i < len(extra):
    flag = extra[i]
    val = extra[i + 1] if i + 1 < len(extra) and not extra[i + 1].startswith("--") else None
    if flag in args:
        at = args.index(flag)
        if val is None:
            args[at:at + 1] = [flag]
        else:
            args[at:at + 2] = [flag, val]
    else:
        args += [flag] + ([val] if val is not None else [])
    i += 2 if val is not None else 1
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print("EXTRA_ENGINE_ARGS merged into", path)
PYEOF
fi

# Later starts skip straight here: setup.py finds the installed config and
# launches serve/server.py (OpenAI- and Anthropic-compatible API on :8080).
# GPUS / GPU / LAYER_SPLIT are repeated on purpose. Given at the start they pin the
# cards for this model, and setup.py saves them in its config; without them a config
# that names one card is offered once to a pair, on its own, when the host has two
# cards that can share the model (setup.py's offer_together, docs/MULTI_GPU.md).
set -- --port "$PORT"
if [ -n "$GPUS" ]; then set -- "$@" --gpus "$GPUS"; fi
if [ -n "$GPU" ]; then set -- "$@" --gpu "$GPU"; fi
if [ -n "$LAYER_SPLIT" ]; then set -- "$@" --layer-split "$LAYER_SPLIT"; fi
exec .venv/bin/python setup.py "$@"
