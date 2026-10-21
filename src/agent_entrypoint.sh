#!/bin/sh
set -eu

mkdir -p "$CODEX_HOME/skills" "$CODEX_HOME/plugins"
if [ -f /opt/agent-bundle/marketplaces.toml ]; then
    if ! grep -q '^# BEGIN AIDEV79 AGENT MARKETPLACES$' "$CODEX_HOME/config.toml" 2>/dev/null; then
        {
            printf '\n# BEGIN AIDEV79 AGENT MARKETPLACES\n'
            cat /opt/agent-bundle/marketplaces.toml
            printf '# END AIDEV79 AGENT MARKETPLACES\n'
        } >> "$CODEX_HOME/config.toml"
    fi
    while IFS= read -r plugin; do
        [ -n "$plugin" ] || continue
        codex plugin add "$plugin" >/dev/null
    done < /opt/agent-bundle/plugin-list.txt
fi
if [ -d /opt/agent-bundle/standalone-skills ]; then
    for skill in /opt/agent-bundle/standalone-skills/*; do
        name=${skill##*/}
        if [ ! -e "$CODEX_HOME/skills/$name" ]; then
            ln -s "$skill" "$CODEX_HOME/skills/$name"
        fi
    done
fi
mkdir -p "$CODEX_HOME/ponytail-data"
if [ ! -f /opt/agent-bundle/marketplaces.toml ]; then
    if [ ! -e "$CODEX_HOME/hooks.json" ]; then
        cp /opt/agent-bundle/ponytail/hooks/hooks.json "$CODEX_HOME/hooks.json"
    fi
    if ! cmp -s "$CODEX_HOME/hooks.json" /opt/agent-bundle/ponytail/hooks/hooks.json; then
        echo "Ponytail hooks.json differs from the reviewed bundle; inspect it before starting Codex" >&2
        exit 1
    fi
fi
if [ ! -e "$CODEX_HOME/skills/ponytail" ]; then
    ln -s /opt/agent-bundle/ponytail/skills/ponytail "$CODEX_HOME/skills/ponytail"
fi
if [ ! -e "$CODEX_HOME/plugins/ponytail" ]; then
    ln -s /opt/agent-bundle/ponytail "$CODEX_HOME/plugins/ponytail"
fi
if [ "${TT_TRAIN_HELPER:-}" = "1" ]; then
    attempt=0
    until [ -S /run/td/helper.sock ]; do
        attempt=$((attempt + 1))
        if [ "$attempt" -ge 60 ]; then
            echo "Train Driver helper did not become ready" >&2
            exit 1
        fi
        sleep 0.5
    done
fi
exec node /usr/local/bin/agent-env-launcher.mjs "$@"
