
# Docker Guardian

Docker Guardian is a lightweight monitoring and automation tool for Docker environments.  
It tracks container health, system resource usage, and can automatically recover failed services.

It is designed for self-hosted systems where simplicity and reliability matter.

---

## Features

- Real-time Docker container monitoring
- CPU and memory tracking (psutil-based)
- Automatic restart of unhealthy containers
- SQLite-based local storage (no external DB required)
- Simple Docker Compose deployment
- Extensible for alerts and automation
- Lightweight and fast

---

## Installation

### Clone repository

```bash
git clone https://gitea.tmayt.ir/thaiostream/docker-guardian.git
cd docker-guardian
````

---

### Docker Compose

Create `docker-compose.yml`:

```yaml
services:
  docker-guardian:
    image: gitea.tmayt.ir/thaiostream/docker-guardian:latest
    container_name: docker-guardian
    restart: unless-stopped

    pid: "host"

    ports:
      - "127.0.0.1:8000:8000"

    volumes:
      - ./data:/app/data

    environment:
      CPU_THRESHOLD: 80
      MEMORY_THRESHOLD: 75
      CHECK_INTERVAL: 60
      AUTO_RESTART: "true"
```

Run:

```bash
docker compose up -d
```

---

## Configuration

| Variable         | Description                       | Default |
| ---------------- | --------------------------------- | ------- |
| CPU_THRESHOLD    | CPU usage limit (%)               | 80      |
| MEMORY_THRESHOLD | Memory usage limit (%)            | 75      |
| CHECK_INTERVAL   | Monitoring interval (seconds)     | 60      |
| AUTO_RESTART     | Auto restart unhealthy containers | true    |

---

## Data Storage

SQLite database is used for persistence.

Default path:

```
/app/data/db.sqlite
```

To persist data:

```yaml
volumes:
  - ./data:/app/data
```

---

## How It Works

* Collects system metrics using psutil
* Reads Docker container states
* Compares against thresholds
* Detects unhealthy services
* Optionally restarts containers
* Stores logs in SQLite

---

## Use Cases

* Prevent downtime in self-hosted apps
* Auto-restart crashed containers
* Detect memory or CPU leaks
* Lightweight alternative to Prometheus/Grafana
* Simple server health automation

---

## Requirements

* Docker 20+
* Docker Compose v2+
* Linux host recommended

---

## Security Notes

* `pid: host` is required for accurate monitoring
* Do not expose publicly without authentication
* Use reverse proxy if needed
* Restrict Docker socket access

---

## Development

Run locally:

```bash
pip install -r requirements.txt
python main.py
```

---

## Project Structure

```
docker-guardian/
│
├── app/
├── data/
├── docker-compose.yml
├── requirements.txt
└── main.py
```

---

## License

Add your license here (MIT / Apache-2.0 / etc.)

---

## Contributing

Pull requests are welcome.
Open an issue first for major changes.

---

## Roadmap

* Web dashboard
* Telegram/email alerts
* Prometheus metrics export
* Multi-node support

---

## Author

Thaiostream
