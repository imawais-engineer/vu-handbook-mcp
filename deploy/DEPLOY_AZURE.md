# Deploy vu-handbook-mcp on an Azure VM

Result: `https://<your-host>/mcp`. This is a Streamable HTTP MCP endpoint with automatic HTTPS (Caddy + Let's Encrypt), bearer-token auth and per-IP rate limiting. It runs on one small Ubuntu VM that sets itself up on first boot.

```
Client (Claude / Cursor / agent) --HTTPS--> Caddy :443 --> serve.py :8000 (auth + rate limit) --> FastMCP (mcp_server.py, read-only SQLite)
```

## What is in `deploy/`
| File | Purpose |
|---|---|
| `serve.py` | Production entrypoint. It wraps the unchanged `mcp_server.py` with bearer auth, rate limiting and the allowed Host header. `GET /healthz` stays open. |
| `Dockerfile` | `python:3.12-slim`, runs as non-root uid 10001. Ships `data/` + `docs/lms/`. Has a healthcheck. Build it from the repo root. |
| `docker-compose.yml` | `app` (no published ports, read-only filesystem, all capabilities dropped, 512 MB limit) + `caddy` (ports 80/443) |
| `Caddyfile` | TLS, HSTS, 1 MB body limit, streaming-safe reverse proxy |
| `.env.example` | All settings. Copy it to `deploy/.env` (mode 600, never commit). |
| `cloud-init.yaml` | First boot: installs Docker, clones the repo, generates a token **on the VM**, starts the stack |
| `azure/create-vm.sh` / `azure/destroy.sh` | az CLI: resource group, VM, firewall (NSG) with only 80/443, SSH key only, static IP + free DNS name |
| `update.sh` | Update or roll back on the VM |
| `vu-handbook-mcp.service` | systemd alternative without Docker |
| `github-actions-deploy.yml.example` | Optional one-click deploy via Azure Run Command. **Not enabled.** |

## 0. Before you start (Azure for Students)
- **Subscription:** Azure for Students (GitHub Student Developer Pack). It gives a $100 credit for 12 months with no card. It also includes **750 h/month free** of `B1s`, `B2ats_v2` or `B2pts_v2` Linux VMs and 2 × 64 GB P6 SSDs, for the first 12 months.
- **Allowed regions:** students can deploy only to about 5 regions.
  1. Open Portal → **Policy → Assignments → "Allowed resource deployment regions"**.
  2. Read the allowed locations and pick the one closest to Pakistan, e.g. `centralindia`, `uaenorth` or `southeastasia`.
- **Size:** prefer **Standard_B2ats_v2** (2 vCPU, 1 GiB). `B1s` is being retired and is often blocked on student subscriptions. The server needs about 100 MB of RAM.

### Cost estimate (per month)
| Item | Free tier | After free tier / pay-as-you-go (approx.) |
|---|---|---|
| VM B2ats_v2 (or B1s) | $0 (750 h) | ~$7–8 |
| OS disk 64 GB Premium SSD P6 | $0 | ~$10 (use Standard SSD 30 GB ~$2.4 to save) |
| Standard static public IP | ~$3.65 | ~$3.65 |
| Outbound data (first 100 GB) | $0 | $0 |
| **Total** | **~$3.65 from the $100 credit** | ~$12–22 |

Set a budget alert: Portal → **Cost Management → Budgets → $10/month, alert at 50 %**.

## 1. Pick the hostname
- **Option A (free, no DNS work):** the Azure DNS label `<label>.<region>.cloudapp.azure.com`, e.g. `vu-handbook-mcp.centralindia.cloudapp.azure.com`.
- **Option B (branded):** `vumcp.imawais.engineer`. Create an **A record** pointing to the VM's static IP *before* first boot, or re-run `update.sh` after DNS has propagated, because Caddy retries certificates automatically.

## 2a. Create with az CLI (recommended)
Use Azure Cloud Shell (portal ▸ `>_` icon, az already logged in) or a local `az login`.
```bash
git clone https://github.com/imawais-engineer/vu-handbook-mcp.git && cd vu-handbook-mcp
export LOCATION=centralindia            # one of YOUR allowed regions
export DNS_LABEL=vu-handbook-mcp        # must be unique in the region
export ACME_EMAIL=you@example.com       # Let's Encrypt notices
# Option B only:  export MCP_PUBLIC_HOST=vumcp.imawais.engineer
# Optional SSH from your IP only:  export MY_IP=$(curl -s https://api.ipify.org)
bash deploy/azure/create-vm.sh
```
The script prints the IP, the DNS name, the MCP URL and the command to read the token.
- **Token:** created on the VM. Read it with `az vm run-command invoke … grep ^MCP_BEARER_TOKENS=`. Store it in your password manager, never in git or chat.
- **First boot:** takes about 3–5 minutes (apt, Docker build, certificate).

## 2b. Or click through the Portal
1. **Create a resource → Virtual machine.**
   - **Basics:**
     - Resource group: new `vu-handbook-mcp-rg`
     - Name: `vu-handbook-mcp`
     - Region: an allowed one
     - Image: **Ubuntu Server 24.04 LTS x64 Gen2**
     - Size: **Standard_B2ats_v2**
     - Authentication: **SSH public key**, user `azureuser`
     - Inbound ports: **None**
   - **Disks:** OS disk 64 GiB **Premium SSD LRS** (the free P6 size).
   - **Networking:**
     - Public IP: new, **Static**
     - NIC network security group: **Advanced**, then add inbound rules for **80, 443** (TCP) and 443 (UDP)
     - SSH 22: add it only from *My IP* if you want it
   - **Monitoring:** Boot diagnostics **Disable**.
   - **Advanced → Custom data:** paste `deploy/cloud-init.yaml`, after replacing `__MCP_PUBLIC_HOST__` and `__ACME_EMAIL__`.
2. **Review + create.**
3. Set the DNS label: VM → **Overview → DNS name → Configure** → label, then Save.

## 3. Smoke test
```bash
HOST=vu-handbook-mcp.centralindia.cloudapp.azure.com   # your host
TOKEN=...                                               # from step 2
curl -s https://$HOST/healthz                           # {"status":"ok",...}
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://$HOST/mcp -d '{}'   # 401 without token
curl -s https://$HOST/mcp -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}'
# -> event: message / data: {... "serverInfo":{"name":"vu-student-support" ...}}
npx @modelcontextprotocol/inspector   # GUI: Streamable HTTP, URL https://$HOST/mcp, header Authorization: Bearer $TOKEN
```

## 4. Add it to MCP clients
**Claude Code**
```bash
claude mcp add --transport http vu-handbook https://$HOST/mcp --header "Authorization: Bearer $TOKEN"
```
**Cursor** (`~/.cursor/mcp.json`). Keep the token in an environment variable:
```json
{ "mcpServers": { "vu-handbook": {
    "url": "https://vumcp.example.com/mcp",
    "headers": { "Authorization": "Bearer ${env:VU_MCP_TOKEN}" } } } }
```
**Claude Desktop** (`claude_desktop_config.json`, via the `mcp-remote` bridge):
```json
{ "mcpServers": { "vu-handbook": {
    "command": "npx",
    "args": ["-y", "mcp-remote", "https://vumcp.example.com/mcp", "--header", "Authorization:${AUTH_HEADER}"],
    "env": { "AUTH_HEADER": "Bearer <token>" } } } }
```
**Claude.ai / ChatGPT custom connectors:** these support only OAuth or no auth, not static bearer tokens. To list the server there, set `MCP_ALLOW_ANONYMOUS=1`. The handbook is public data, and the rate limit still applies. Alternatively, add OAuth later.

## 5. Update and rollback
```bash
ssh azureuser@$HOST            # or: az vm run-command invoke ... --scripts "<cmd>"
sudo /opt/vu-handbook-mcp/deploy/update.sh            # latest main
sudo /opt/vu-handbook-mcp/deploy/update.sh <old-sha>  # rollback (previous SHAs in deploy/.deploy-history)
```
- **Rotate the token:** edit `MCP_BEARER_TOKENS` in `/opt/vu-handbook-mcp/deploy/.env`, then run `docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d`. You can list two tokens, comma-separated, for a zero-downtime switch.
- **Refresh the handbook data:** run `python3 tools/refresh.py` locally, commit `data/`, then run `update.sh`.

## 6. Monitoring
- **Health:** `https://$HOST/healthz`. Add a free uptime monitor (UptimeRobot / Better Stack) every 5 minutes, or an Azure Monitor availability alert.
- **Logs:** `sudo docker compose -f /opt/vu-handbook-mcp/deploy/docker-compose.yml logs -f --tail 100`. Caddy writes JSON access logs, and you can grep for `"status":401` or `429` to spot abuse.
- **VM:** Portal → VM → **Metrics** (CPU credits, network). Set an alert for CPU > 80 % for 15 minutes.
- **Cost:** the budget alert from step 0.

## 7. Security checklist
- [ ] Firewall (NSG) allows only 80/443. 22 is closed, or open only to your IP. SSH uses keys only, no password.
- [ ] `deploy/.env` has mode 600 and is not in git. The token is ≥ 32 random chars and is stored in a password manager.
- [ ] `MCP_ALLOW_ANONYMOUS=0` unless you deliberately publish it for Claude.ai/ChatGPT.
- [ ] The rate limit is tuned (`MCP_RATE_PER_MIN`, `MCP_RATE_BURST`).
- [ ] The app container is non-root, read-only, capabilities dropped and not published. Only Caddy is exposed.
- [ ] HTTPS works (`curl -I https://$HOST/healthz` shows HSTS). Port 80 only redirects.
- [ ] DNS-rebinding protection: Host must equal `MCP_PUBLIC_HOST`, otherwise 421.
- [ ] Unattended security upgrades are on (cloud-init). Reboot monthly: `sudo reboot`.
- [ ] The budget alert is set. To delete everything: `bash deploy/azure/destroy.sh`.
- [ ] The server is read-only over public handbook data. It has no student records and no credentials.
