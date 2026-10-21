#!/bin/sh
set -eu

case "${1:-}" in
  local)
    for tool in python3 git rg; do
      command -v "$tool" >/dev/null 2>&1 || { echo "$tool is unavailable" >&2; exit 2; }
    done
    echo 'Local Python, Git and search tools are available.'
    ;;
  sqlserver)
    command -v sqlcmd >/dev/null 2>&1 || { echo 'Linux sqlcmd is unavailable; Windows sqlserver-read-tool.ps1 cannot run here.' >&2; exit 2; }
    echo 'Linux sqlcmd is installed; connection and read-only query behavior are unverified.'
    ;;
  kafka)
    command -v kafka-run-class.sh >/dev/null 2>&1 || { echo 'Linux Kafka CLI is unavailable; Windows kafka-read-tool.ps1 cannot run here.' >&2; exit 2; }
    echo 'Linux Kafka CLI is installed; broker access and read-only behavior are unverified.'
    ;;
  helpdesk)
    echo 'HelpDesk uses the Windows helpdesk-local.ps1 transport; no Linux transport is configured.' >&2
    exit 2
    ;;
  excel)
    echo 'Microsoft Excel COM is unavailable in Linux; use explicit OOXML fallback and report missing live recalculation.' >&2
    exit 2
    ;;
  *)
    echo 'Usage: standalone-linux-runtime-check.sh local|sqlserver|kafka|helpdesk|excel' >&2
    exit 2
    ;;
esac
