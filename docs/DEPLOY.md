# Running Just Job on a server

On a server it fetches around the clock without your computer on: the full run every 4 hours, a
refresh of companies with recent matches every hour, and the dashboard always up behind a password.

Any Linux server with systemd and Python 3.11+ works: Ubuntu 24.04 or Debian 12 on Oracle Cloud's
Always Free tier, or a small VPS (DigitalOcean, AWS Lightsail, Hetzner). It needs about 1 CPU,
1 GB of memory and a few GB of disk.

## 1. Create the server

**Oracle Cloud (free):** Compute → Instances → Create instance. Image: Canonical Ubuntu 24.04.
Shape: `VM.Standard.A1.Flex` with 1 OCPU and 6 GB (Ampere, Always Free). Add your SSH public key.
If it says "out of capacity", try another availability domain or try again later.

If you'll use a domain (step 5), open ports 80 and 443: Networking → Virtual cloud networks → your
VCN → Security lists → Default → Add ingress rules, source `0.0.0.0/0`, TCP, destination ports
`80,443`. The setup script opens them in the server's own firewall.

**Any VPS:** create an Ubuntu 24.04 server with your SSH key. With a domain, allow ports 80 and 443.

## 2. Get the code onto it

```bash
ssh ubuntu@SERVER_IP
sudo apt-get update && sudo apt-get install -y git python3
git clone https://github.com/ankit67776/JustJob.git && cd JustJob
```

The repository is private, so `git clone` asks for your GitHub username and a password: use a
fine-grained personal access token (GitHub → Settings → Developer settings → Personal access
tokens) with read access to the JustJob repository's contents.

## 3. Copy your keys and data from your computer

In PowerShell on your computer, from the project folder:

```powershell
ssh ubuntu@SERVER_IP "mkdir -p ~/JustJob/data"
scp .env ubuntu@SERVER_IP:~/JustJob/
scp data\jobs.db ubuntu@SERVER_IP:~/JustJob/data/
scp resume.txt ubuntu@SERVER_IP:~/JustJob/          # only if you use one
```

This keeps your matches, AI scores and Applied / Hidden lists. Skip `jobs.db` to start empty.

## 4. Set a dashboard password

On the server: `nano ~/JustJob/.env` and set `DASHBOARD_PASSWORD=` to a long password.

## 5. Run the setup

Without a domain, the dashboard is reachable only through an SSH tunnel (nothing is exposed):

```bash
cd ~/JustJob && bash scripts/setup_linux.sh
```

With a domain, it's served over HTTPS. A free one works: sign in at duckdns.org, create e.g.
`yourname.duckdns.org` and set its IP to the server's public IP. Then:

```bash
cd ~/JustJob && DOMAIN=yourname.duckdns.org bash scripts/setup_linux.sh
```

Start the first run right away instead of waiting for the timer (about 40 minutes):

```bash
sudo systemctl start justjob-run
```

## 6. Open the dashboard

- With a domain: `https://yourname.duckdns.org`, then sign in.
- Without: on your computer run `ssh -L 8765:127.0.0.1:8765 ubuntu@SERVER_IP`, keep it open, and
  go to http://127.0.0.1:8765.

## 7. Stop the runs on your computer

Once the server is fetching, remove the Windows tasks so both don't fetch the same jobs:

```powershell
Unregister-ScheduledTask -TaskName JustJob -Confirm:$false
Unregister-ScheduledTask -TaskName JustJob-hot -Confirm:$false
```

## Day to day

| What | Command (on the server) |
|---|---|
| When the next runs are | `systemctl list-timers 'justjob*'` |
| Last full run's output | `journalctl -u justjob-run -n 50` |
| Last hourly refresh | `journalctl -u justjob-hot -n 50` |
| Dashboard errors | `journalctl -u justjob-web -n 50` |
| Counts and board health | `cd ~/JustJob && python3 -m justjob stats` |
| Update to the latest code | `cd ~/JustJob && git pull && sudo systemctl restart justjob-web` |
| Find new companies (monthly) | `cd ~/JustJob && python3 -m justjob harvest` |
| Back up the database | `scp ubuntu@SERVER_IP:~/JustJob/data/jobs.db .` (from your computer) |
