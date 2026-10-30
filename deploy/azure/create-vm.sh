#!/usr/bin/env bash
# Creates: resource group, Ubuntu 24.04 VM (free-tier size), NSG with ONLY 80/443 open,
# SSH key-only auth (port 22 closed unless MY_IP is set), static public IP + free DNS label,
# and cloud-init that installs Docker and starts vu-handbook-mcp behind Caddy (HTTPS).
# Prereqs: az CLI logged in (az login), correct subscription selected. Review before running.
set -euo pipefail

: "${RG:=vu-handbook-mcp-rg}"
: "${LOCATION:?set LOCATION to one of your allowed regions, e.g. centralindia, southeastasia, uaenorth}"
: "${VM_NAME:=vu-handbook-mcp}"
: "${VM_SIZE:=Standard_B2ats_v2}"        # free 750 h/month on Azure for Students; alt: Standard_B1s / Standard_B2s
: "${IMAGE:=Canonical:ubuntu-24_04-lts:server:latest}"   # or Ubuntu2204
: "${DNS_LABEL:=vu-handbook-mcp-$RANDOM}" # -> $DNS_LABEL.$LOCATION.cloudapp.azure.com
: "${MCP_PUBLIC_HOST:=$DNS_LABEL.$LOCATION.cloudapp.azure.com}"  # or your own subdomain
: "${ACME_EMAIL:?set ACME_EMAIL for Lets Encrypt notices}"
: "${ADMIN_USER:=azureuser}"
: "${SSH_PUBKEY:=$HOME/.ssh/id_ed25519.pub}"
: "${MY_IP:=}"                            # optional: your public IPv4 to allow SSH from

HERE="$(cd "$(dirname "$0")/.." && pwd)"
CI=$(mktemp)
sed -e "s|__MCP_PUBLIC_HOST__|$MCP_PUBLIC_HOST|" -e "s|__ACME_EMAIL__|$ACME_EMAIL|" "$HERE/cloud-init.yaml" > "$CI"

[ -f "$SSH_PUBKEY" ] || ssh-keygen -t ed25519 -f "${SSH_PUBKEY%.pub}" -N "" -C "$ADMIN_USER@$VM_NAME"

az group create -n "$RG" -l "$LOCATION" -o none

az network nsg create -g "$RG" -n "$VM_NAME-nsg" -o none
az network nsg rule create -g "$RG" --nsg-name "$VM_NAME-nsg" -n allow-http-https \
  --priority 100 --direction Inbound --access Allow --protocol Tcp \
  --destination-port-ranges 80 443 -o none
az network nsg rule create -g "$RG" --nsg-name "$VM_NAME-nsg" -n allow-http3 \
  --priority 105 --direction Inbound --access Allow --protocol Udp \
  --destination-port-ranges 443 -o none
if [ -n "$MY_IP" ]; then
  az network nsg rule create -g "$RG" --nsg-name "$VM_NAME-nsg" -n allow-ssh-my-ip \
    --priority 110 --direction Inbound --access Allow --protocol Tcp \
    --source-address-prefixes "$MY_IP/32" --destination-port-ranges 22 -o none
fi

az network public-ip create -g "$RG" -n "$VM_NAME-ip" --sku Standard --allocation-method Static \
  --dns-name "$DNS_LABEL" -o none

az vm create -g "$RG" -n "$VM_NAME" \
  --image "$IMAGE" --size "$VM_SIZE" \
  --admin-username "$ADMIN_USER" --ssh-key-values "$SSH_PUBKEY" --authentication-type ssh \
  --public-ip-address "$VM_NAME-ip" --nsg "$VM_NAME-nsg" \
  --os-disk-size-gb 64 --storage-sku Premium_LRS \
  --custom-data "$CI" -o table
rm -f "$CI"

echo
echo "VM IP:   $(az network public-ip show -g "$RG" -n "$VM_NAME-ip" --query ipAddress -o tsv)"
echo "DNS:     $DNS_LABEL.$LOCATION.cloudapp.azure.com"
echo "MCP URL: https://$MCP_PUBLIC_HOST/mcp   (cloud-init needs ~3-5 min)"
echo "Read the bearer token (no SSH needed):"
echo "  az vm run-command invoke -g $RG -n $VM_NAME --command-id RunShellScript --scripts \"grep ^MCP_BEARER_TOKENS= /opt/vu-handbook-mcp/deploy/.env\" --query 'value[0].message' -o tsv"
