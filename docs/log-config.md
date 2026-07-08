# System Operations: Logging, Rotation & Security Hardening

This document outlines the setup, architecture, and maintenance operations for application logging, cloud log harvesting, and gateway security controls.

---

## 1. Structured Logging Setup (GCP Cloud Logging)

To trace issues effectively using Google Cloud Platform (GCP) monitoring dashboards, application logs must be emitted as raw, un-prefixed JSON objects. Writing to standard output (`stdout`) via Gunicorn introduces process tracking prefixes that break GCP's JSON parsing engine [STEM].

### Implementation
We utilize a dedicated, isolated logging channel that bypasses Gunicorn streams and writes directly to disk under the active bench's `logs/` directory. It automatically extracts unique request context identifiers from Frappe’s thread-local memory to allow cross-log correlation [STEM]. Refer [monitoring.py](../tap_lms/monitoring.py)


### Usage Example
Developers can call this function from **any** nesting depth without altering intermediate function parameters or passing request arguments manually:
```python
@frappe.whitelist(allow_guest=True)
def save_submission(submission_id):
    # Business logic execution...
    emit_structured_log("INFO", "submission_processed", submission_id=submission_id)
```

---

## 2. Automated Log File Rotation

We use RotationLogger from the logger library to rotate the log files once they reach 10MB and to keep last 5 files available incase required. An alternative to this approach, suggested by Gemini, is to use the below (NOTE: Do not use both these methods at the same time):

Because the structured log grows continuously in production, we use Linux's native `logrotate` engine with a `copytruncate` directive [STEM]. This ensures logs are safely truncated without forcing a restart of Gunicorn or dropping active TCP/HTTP client connections [STEM].

By omitting explicit user and group names from the `create` directive, `logrotate` dynamically inspects who owns the existing log file and duplicates those exact permissions on the new, empty file [STEM]. This prevents the file from being hijacked by the `root` user context.

### Configuration Procedure
1. Create a dedicated rotation configuration file:
   ```bash
   sudo nano /etc/logrotate.d/frappe-gcp-structured
   ```

2. Paste the following user-agnostic configuration rules:
   ```text
   /home/*/frappe-bench/logs/gcp_structured.log {
       daily
       missingok
       rotate 14
       compress
       delaycompress
       notifempty
       copytruncate
       create 0664
   }
   ```

3. Validate the layout format configuration using a dry-run test:
   ```bash
   sudo logrotate -d /etc/logrotate.d/frappe-gcp-structured
   ```

---

## 3. Security Hardening (Nginx Access Control)

Automated vulnerability scanners routinely query paths looking for standard environment configurations (e.g., `/aws_credentials.env`, `.env.staging`). Frappe catches these requests and returns a custom webpage with an HTTP `200 OK` network status code, triggering false alarms in security monitoring agents [STEM]. We drop these requests instantly at the firewall boundary layer.

### Backup Rule Location
Always keep a backup of the Nginx configuration snippet inside your app repository directory so it can be easily recovered if a deployment overwrites the active web server files:
`apps/tap_lms/deployment_configs/nginx_security.conf`

```nginx
# =========================================================================
# SECURITY HARDENING: Explicitly block credential harvesting bot requests
# =========================================================================
location ~* \.(env|env\..*|aws_credentials|git|bak|sql)\$ {
    log_not_found off;
    access_log off;
    return 404;
}
```

### Active System Configuration Procedure
1. Open the primary active site Nginx configuration file:
   ```bash
   sudo nano /home/gcp-data/frappe-bench/config/nginx.conf
   ```

2. Paste the security rule block inside the main `server { ... }` block block matching your application routing directives.

3. Verify that Nginx passes down the correlation ID header to Gunicorn within the `location /` section:
   ```nginx
   proxy_set_header X-Request-Id \$request_id;
   ```

4. Audit the file syntax rules and cycle the daemon live:
   ```bash
   sudo nginx -t
   sudo systemctl reload nginx
   ```

---

## 4. Manual Production Deployment Runbook

Because this environment relies on manual deployments without an automated CI/CD pipeline, follow this exact sequence whenever pulling new code updates to ensure security configurations and logging systems are not broken or overwritten.

### Step-by-Step Manual Release Sequence:

1. **Pull the latest repository updates:**
   ```bash
   cd /home/gcp-data/rjs/frappe_tap
   git fetch --all
   git checkout main   # Or your active target branch
   git pull origin main
   ```

2. **Re-sync system environment structures:**
   ```bash
   cd /home/gcp-data/frappe-bench
   bench setup requirements
   bench --site your_site_name migrate
   ```

3. **CRITICAL: Restore Nginx Security Configuration:**
   If a teammate ran `bench setup nginx` during this deployment window, your gateway security rules were overwritten. Open `/home/gcp-data/frappe-bench/config/nginx.conf` and ensure your custom `location` security block is manually appended back inside the primary `server { ... }` configuration space.

4. **Verify and Reload Gateway Operations:**
   ```bash
   sudo nginx -t
   sudo systemctl reload nginx
   ```

5. **Clear Application Context & Restart Workers:**
   ```bash
   bench clear-cache
   sudo supervisorctl restart frappe-bench-web:*
   ```

6. **Verify Live Log Streams:**
   Confirm that your tracking pipelines are clean and streaming:
   ```bash
   tail -n 20 -f /home/gcp-data/frappe-bench/logs/gcp_structured.log
   ```
