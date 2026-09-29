# Production Integration Guide

## Quick Setup (30 Minutes)

### Step 1: Prepare Your Log Files

1. Find where your production logs are stored:
   ```bash
   # Linux
   ls /var/log/
   
   # Windows
   dir C:\logs\
   
   # Docker
   docker logs your_container > app.log
   ```

2. Make sure logs are accessible to OpsMemory AI

3. Note the log file format (JSON or plain text)

---

### Step 2: Configure Environment

Edit `.env` file:

```bash
# Enable production data
USE_PRODUCTION_DATA=true

# Log source type
LOG_SOURCE=files

# Log file path (replace with yours)
PRODUCTION_LOG_FILE=/path/to/your/logs/application.log

# Or per-service:
PAYMENT_SERVICE_LOG=/logs/payment.log
AUTH_SERVICE_LOG=/logs/auth.log
```

---

### Step 3: Update Engine to Use Production Logs

Edit `backend/app/sim/engine.py`:

**Add at top of file (after imports):**

```python
import os
from app.core.config import settings

# Production integration
_production_log_adapter = None
if os.getenv("USE_PRODUCTION_DATA") == "true":
    try:
        from app.integrations.production_logs import ProductionLogAdapter
        _production_log_adapter = ProductionLogAdapter(
            log_source=os.getenv("LOG_SOURCE", "files")
        )
        logger.info("Production log adapter initialized")
    except Exception as e:
        logger.error(f"Failed to initialize production logs: {e}")
```

**Modify `recent_logs` method (around line 625):**

```python
def recent_logs(
    self,
    *,
    service_name: str,
    seconds: float,
    level: str | None = None,
    limit: int = 400,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    # PRODUCTION MODE
    if _production_log_adapter is not None:
        logger.info(f"Fetching REAL logs for {service_name}")
        return _production_log_adapter.fetch_recent_logs(
            service_name=service_name,
            seconds=seconds,
            level=level,
            limit=limit
        )
    
    # SIMULATION MODE (original code)
    service = self.service(service_name)
    # ... rest of method unchanged ...
```

---

### Step 4: Test It

1. **Restart backend:**
   ```bash
   cd backend
   .\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
   ```

2. **Check logs:**
   - Look for: "Production log adapter initialized"
   - Should NOT see errors

3. **Test API:**
   - Go to: http://127.0.0.1:8000/docs
   - Find: GET /api/ops/logs
   - Try it!

4. **Check frontend:**
   - Open application
   - Go to incidents page
   - Logs should show REAL data from your files!

---

### Step 5: Map Your Services

Edit `.env` to map your real service names:

```bash
# If your logs use different service names
SERVICE_NAME_MAPPING={"my-api": "api-service", "payments-svc": "payment-service"}
```

Or update `backend/app/database/seed.py` to use your actual service names:

```python
SERVICES = [
    {"name": "your-actual-service-1", "kind": "api", ...},
    {"name": "your-actual-service-2", "kind": "worker", ...},
]
```

---

## Troubleshooting

### Logs Not Appearing?

1. **Check file path:**
   ```python
   # In Python console
   from pathlib import Path
   Path("/your/log/path").exists()  # Should be True
   ```

2. **Check permissions:**
   ```bash
   # Linux
   ls -la /path/to/logs/
   
   # Make readable if needed
   chmod 644 /path/to/logs/*.log
   ```

3. **Check log format:**
   ```bash
   # View first few lines
   head -n 5 /path/to/logs/application.log
   ```

4. **Check backend logs:**
   - Look for errors in terminal where backend is running
   - Should see "Fetching REAL logs for..."

### Still Using Simulator?

Check that:
- [ ] `USE_PRODUCTION_DATA=true` in `.env`
- [ ] Backend restarted after changing `.env`
- [ ] No errors in backend startup logs
- [ ] Log file path is correct

---

## Advanced: Multiple Log Sources

If you have different log sources per service:

```python
# In production_logs.py, modify __init__:

def __init__(self, log_source: str, api_key: str = ""):
    self.log_source = log_source
    self.api_key = api_key
    
    # Per-service configuration
    self.service_configs = {
        "payment-service": {
            "source": "elasticsearch",
            "index": "payments-logs-*"
        },
        "auth-service": {
            "source": "files",
            "path": "/var/log/auth/app.log"
        }
    }

def fetch_recent_logs(self, service_name: str, ...):
    config = self.service_configs.get(service_name, {})
    source = config.get("source", self.log_source)
    
    if source == "elasticsearch":
        return self._fetch_from_elasticsearch(...)
    elif source == "files":
        return self._fetch_from_files(...)
```

---

## Next Steps

Once logs are working:

1. **Add real metrics** (optional)
   - Similar process for Prometheus/Datadog
   - Modify `metric_window` method in engine

2. **Test incident detection**
   - Should detect issues from real logs
   - AI will investigate using real data

3. **Customize services**
   - Update service catalog to match yours
   - Adjust remediation actions for your stack

---

## Need Help?

Common issues:
- **Logs not found:** Check path, check permissions
- **Wrong format:** Update `_parse_plain_log` to match yours
- **Performance:** Increase buffer size in `_fetch_from_files`
- **Multiple files:** Use log rotation patterns like `app.log*`

