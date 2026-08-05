# TAP LMS — Server Setup & Restore Guide

This document covers setting up a fresh Ubuntu server and restoring a TAP LMS
instance from a backup. Tested on Ubuntu 22.04 / GCP Compute Engine.

---

## Prerequisites

- Ubuntu 22.04 VM (GCP or equivalent)
- A bench user account (e.g. `lms-dev`) — do **not** run bench as root
- SSH access to the server
- Backup files from the old server:
  - `<timestamp>-database.sql.gz`
  - `<timestamp>-files.tar` (public files)
  - `<timestamp>-private-files.tar` (private files)

---

## 1. Install System Dependencies

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git python3-pip python3-venv redis-server \
    postgresql postgresql-contrib nginx supervisor \
    libpq-dev wkhtmltopdf cron
```

### Node.js (via nvm — must be Node 16)

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.0/install.sh | bash
source ~/.bashrc
nvm install 16
nvm use 16
nvm alias default 16
```

> **Important:** Frappe v14's `socketio.js` requires Node 16. Node 18+ will cause
> a spawn error in supervisor.

### Add bench to PATH

`pip install frappe-bench` installs bench to `~/.local/bin` which is not in
PATH by default:

```bash
echo 'export PATH=$HOME/.local/bin:$PATH' >> ~/.bashrc
source ~/.bashrc
bench --version  # verify
```

### Install yarn

Frappe requires yarn for building frontend assets:

```bash
npm install -g yarn
```

### Make node visible to supervisor (runs as root)

```bash
sudo ln -sf ~/.nvm/versions/node/v16.*/bin/node /usr/local/bin/node
sudo ln -sf ~/.nvm/versions/node/v16.*/bin/npm /usr/local/bin/npm
```

---

## 2. Install bench

```bash
pip3 install frappe-bench
```

---

## 3. Initialise the Bench

```bash
bench init frappe-bench --frappe-branch version-14
cd frappe-bench
```

---

## 4. Get Apps

```bash
bench get-app https://github.com/<org>/tap_lms
bench get-app https://github.com/<org>/business_theme_v14
# add any other custom apps here
```

---

## 5. PostgreSQL Setup

Frappe on the old server uses `frappe_db` as the database role. Mirror this
on the new server so the backup restores without ownership errors.

```bash
sudo -u postgres psql
```

```sql
CREATE ROLE frappe_db LOGIN PASSWORD '<password from old site_config.json>';
ALTER ROLE frappe_db CREATEDB;
CREATE DATABASE frappe_db OWNER frappe_db;
GRANT frappe_db TO postgres;
\q
```

---

## 6. Create the Site

```bash
bench new-site tap_lms.dev \
    --db-type postgres \
    --db-host localhost
# enter the frappe_db password when prompted
# set an admin password when prompted
```

After site creation, update the site config to use the `frappe_db` user:

```bash
bench --site tap_lms.dev set-config db_name frappe_db
bench --site tap_lms.dev set-config db_password '<frappe_db password>'
bench use tap_lms.dev
```

---

## 7. Restore the Database

Bypass `bench restore` (it tries to read `tabSingles` before the DB is
populated, causing an error). Restore directly via psql instead:

```bash
# drop the empty database bench just created and restore the backup
sudo -u postgres psql -c "DROP DATABASE frappe_db;"
sudo -u postgres psql -c "CREATE DATABASE frappe_db OWNER frappe_db;"

gunzip -c /path/to/<timestamp>-database.sql.gz | sudo -u postgres psql -d frappe_db
```

---

## 7a. Copy the Encryption Key

Frappe encrypts sensitive fields (passwords, API tokens, credentials) using an
encryption key stored in `site_config.json`. The restored database contains
data encrypted with the **old server's key** — if the new server has a
different key, all encrypted fields will be unreadable.

On the **old server**:

```bash
cat ~/frappe-bench/sites/tap_lms.dev/site_config.json | grep encryption_key
```

On the **new server**:

```bash
bench --site tap_lms.dev set-config encryption_key <key from old server>
```

> This affects RabbitMQ Settings, Glific Settings, and any other DocType
> that stores passwords or tokens. Skipping this step will cause silent
> failures when the app tries to connect to external services.

---

## 8. Restore Files

The backup tars contain the full site path internally (e.g.
`./tap_lms.dev/public/files/...`). Extract to `sites/`:

```bash
cd ~/frappe-bench

# public files
tar -xf /path/to/<timestamp>-files.tar \
    --transform='s|tap_lms\.dev|tap_lms.dev|' \
    -C sites/

# private files
tar -xf /path/to/<timestamp>-private-files.tar \
    --transform='s|tap_lms\.dev|tap_lms.dev|' \
    -C sites/

# fix ownership
sudo chown -R lms-dev:lms-dev sites/tap_lms.dev/public/files
sudo chown -R lms-dev:lms-dev sites/tap_lms.dev/private/files
```

> If you are restoring to a site with a **different name** (e.g. old server
> was `tap_lms.dev`, new server is `tap_lms.localhost`), update the
> `--transform` pattern accordingly.

---

## 9. Install Apps into the Site

```bash
bench --site tap_lms.dev install-app tap_lms
bench --site tap_lms.dev install-app business_theme_v14
```

---

## 10. Run Migrations

Start Redis first (supervisor isn't set up yet):

```bash
redis-server ~/frappe-bench/config/redis_cache.conf &
redis-server ~/frappe-bench/config/redis_queue.conf &

bench --site tap_lms.dev migrate
```

---

## 11. Set Up Supervisor

Supervisor manages Redis, Gunicorn, workers, and socketio so they start
automatically on reboot without needing `bench start`.

```bash
bench setup supervisor
sudo ln -sf ~/frappe-bench/config/supervisor.conf \
    /etc/supervisor/conf.d/frappe-bench.conf
```

### Fix socketio — use full node path

Supervisor runs as root and can't find nvm-installed node via PATH. Update
the socketio command to use the full node path:

```bash
sed -i 's|command=.*bench socketio|command=/home/lms-dev/.nvm/versions/node/v16.20.2/bin/node /home/lms-dev/frappe-bench/apps/frappe/socketio.js|' \
    ~/frappe-bench/config/supervisor.conf
```

Verify the change:

```bash
grep -A4 "node-socketio\]" ~/frappe-bench/config/supervisor.conf
```

### Kill any manually started Redis instances before starting supervisor

```bash
sudo pkill -f "redis-server.*frappe-bench"
```

### Start supervisor

```bash
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start all
sudo systemctl enable supervisor
sudo supervisorctl status
```

All processes should show `RUNNING`. If `node-socketio` shows `BACKOFF`,
check `~/frappe-bench/logs/node-socketio.error.log`.

---

## 11a. Start the Feedback Consumer

The feedback consumer listens to the `plagiarism_feedback` RabbitMQ queue and
processes feedback results. It is **not** started by supervisor automatically —
it must be added as a separate supervisor program.

Add the following to `~/frappe-bench/config/supervisor.conf` (placement within the file doesn't matter — supervisor processes all `[program:...]` blocks regardless of order):

```ini
[program:frappe-bench-feedback-consumer]
command=/home/lms-dev/frappe-bench/env/bin/python /home/lms-dev/frappe-bench/apps/tap_lms/scripts/console_consumer.py
directory=/home/lms-dev/frappe-bench/sites
environment=SITE_NAME="tap_lms.dev"
user=lms-dev
autostart=true
autorestart=true
stdout_logfile=/home/lms-dev/frappe-bench/logs/feedback-consumer.log
stderr_logfile=/home/lms-dev/frappe-bench/logs/feedback-consumer.error.log
```

> **Note on log files:** Structured business logic logs (feedback events,
> errors) go to `gcp_structured.log` via `monitoring.py`. The supervisor
> `stdout_logfile` and `stderr_logfile` are a safety net for raw stdout/stderr
> output — startup crashes, import errors, and anything that bypasses
> structured logging. Both are needed.

> **Working directory:** The `directory=/home/lms-dev/frappe-bench/sites`
> setting is critical — the consumer script must be invoked from the `sites/`
> folder for Frappe to resolve the site name correctly. Supervisor `cd`s to
> this directory before executing the command, equivalent to running:
> ```bash
> cd ~/frappe-bench/sites
> ../env/bin/python ../apps/tap_lms/scripts/console_consumer.py
> ```

Then reload supervisor:

```bash
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start frappe-bench-feedback-consumer
sudo supervisorctl status
```

Verify it started correctly:

```bash
cat ~/frappe-bench/logs/feedback-consumer.log
```

If you see `tap_lms.dev does not exist`, the `SITE_NAME` env var is not being
picked up or the `directory` is wrong. If you see the consumer connecting to
RabbitMQ, it's running correctly.

> **Warning:** If you are also running a local podman setup connected to the
> same CloudAMQP instance, stop the local consumer first to avoid race
> conditions — two consumers competing for the same queue will cause
> intermittent failures and messages going to the DLQ. Check active consumers
> in CloudAMQP Manager → Queues → `plagiarism_feedback` → Consumers before
> starting.

---

## 12. Set Up nginx

```bash
bench setup nginx
sudo rm /etc/nginx/sites-enabled/default  # remove default page

sudo ln -sf ~/frappe-bench/config/nginx.conf \
    /etc/nginx/conf.d/frappe-bench.conf
```

### Add log_format (not included by default on Ubuntu)

```bash
sudo sed -i '/http {/a\\n\tlog_format main '"'"'$remote_addr - $remote_user [$time_local] "$request" $status $body_bytes_sent "$http_referer" "$http_user_agent" "$http_x_forwarded_for"'"'"';' \
    /etc/nginx/nginx.conf
```

### Set server_name

```bash
sed -i 's|server_name ;|server_name tap_lms.dev;|' \
    ~/frappe-bench/config/nginx.conf
```

### Fix asset permissions

nginx runs as `www-data` and cannot read files in the home directory by
default. Grant traversal and read access to the assets only:

```bash
chmod o+x /home/lms-dev
chmod o+x /home/lms-dev/frappe-bench
chmod o+x /home/lms-dev/frappe-bench/sites
chmod -R o+rX /home/lms-dev/frappe-bench/sites/assets
```

### Start nginx

```bash
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl start nginx
```

---

## 13. Build Assets

```bash
bench build
```

---

## 14. GCP Firewall

Open port 80 in the GCP Console:

**Compute Engine → VM Instances → click instance → Edit → Firewalls →
check "Allow HTTP traffic"**

Or via gcloud:

```bash
gcloud compute firewall-rules create allow-http \
    --allow tcp:80 \
    --target-tags http-server \
    --description "Allow HTTP traffic"
```

---

## 15. Final Steps

```bash
bench --site tap_lms.dev clear-cache
bench --site tap_lms.dev clear-website-cache
sudo supervisorctl restart all
sudo systemctl restart nginx
```

Add the server IP to your local `/etc/hosts` to resolve the site name:

```bash
# on your LOCAL machine
echo "<server-external-ip>  tap_lms.dev" | sudo tee -a /etc/hosts
```

Then browse to `http://tap_lms.dev` and log in with:

- **Username:** `Administrator`
- **Password:** the Administrator password from the old server (restored from DB)

To reset the admin password if needed:

```bash
bench --site tap_lms.dev set-admin-password <newpassword>
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `bench: command not found` after pip install | `~/.local/bin` not in PATH | `echo 'export PATH=$HOME/.local/bin:$PATH' >> ~/.bashrc && source ~/.bashrc` |
| `FileNotFoundError: /usr/bin/crontab` during bench init | cron not installed | `sudo apt install -y cron` then remove partial bench and retry |
| `engine "node" is incompatible, Expected version ">=18"` | Wrong Node version for Frappe v15 | Install Node 18 via nvm; Node 16 is for v14 only |
| `must be member of role "frappe_db"` | Role doesn't exist on new server | Section 5 — create `frappe_db` role |
| `relation "tabSingles" does not exist` | bench restore pre-flight on empty DB | Section 7 — restore via psql directly |
| `ModuleNotFoundError: No module named 'business_theme_v14'` | App not installed | `bench get-app` + `install-app` |
| `Service redis_cache is not running` during migrate | Redis not started | Start Redis manually before migrate |
| `node-socketio BACKOFF` in supervisor | Wrong node version or path | Use Node 16 full nvm path in supervisor.conf |
| `Error: Cannot start socketio: node not found` | supervisor can't find node in PATH | Use full nvm path in supervisor.conf command |
| Assets 404 in browser | Bundle hash mismatch or permissions | Run `bench build`, fix `o+rX` on assets |
| nginx serves default page instead of Frappe | Default site enabled | `sudo rm /etc/nginx/sites-enabled/default` |
| `unknown log format "main"` in nginx | log_format not defined | Add log_format to `/etc/nginx/nginx.conf` http block |
| Permission denied on assets | www-data can't read home dir | `chmod o+x` on path, `o+rX` on assets |
| Encrypted fields unreadable / external service auth failures | Encryption key mismatch | Section 7a — copy encryption key from old server |
| `SerializationFailure: could not serialize access` in console | Concurrent DB write from pe_dispatcher | Run `frappe.db.rollback()` then retry |
| `InFailedSqlTransaction: current transaction is aborted` | Previous statement failed, transaction broken | Run `frappe.db.rollback()` then retry |
| Messages going to DLQ intermittently | Two consumers competing for same queue | Stop local podman consumer; check CloudAMQP Manager → Consumers |
| `tap_lms.localhost does not exist` in consumer | `SITE_NAME` env var not set or wrong | Set `export SITE_NAME=tap_lms.dev` before starting consumer |

---

## Notes

- **Encryption key** must be copied from the old server (Section 7a) — without
  it, RabbitMQ, Glific, and all other stored credentials will silently fail.
- **Feedback consumer** must be added to supervisor manually (Section 11a) —
  it is not included in `bench setup supervisor` output. Without it, plagiarism
  feedback results will pile up in the `plagiarism_feedback` queue unprocessed.
- **Never run two consumers** against the same CloudAMQP queue simultaneously
  (e.g. local podman + dev server) — messages will be split between them,
  causing intermittent failures and DLQ buildup. Always check CloudAMQP
  Manager → Queues → Consumers before starting the consumer on a new server.
- GCS credentials (`GOOGLE_APPLICATION_CREDENTIALS`) must be configured
  separately for submission image uploads to work.
- After any code deploy, restart workers:
  `sudo supervisorctl restart frappe-bench-workers:`
- After any hooks.py change, restart everything:
  `sudo supervisorctl restart all`
