#!/usr/bin/env bash
# Deletes EVERYTHING created by create-vm.sh (the whole resource group). Irreversible.
set -euo pipefail
: "${RG:=vu-handbook-mcp-rg}"
read -r -p "Delete resource group $RG and all its resources? Type the name to confirm: " c
[ "$c" = "$RG" ] && az group delete -n "$RG" --yes --no-wait && echo "deleting $RG"
