---
title: Installation
description: Set up Thunderbird AI Search from scratch in five steps.
---

## Prerequisites

- [Docker](https://docker.com) and Docker Compose
- [Ollama](https://ollama.ai) running on the Docker host
- [Thunderbird](https://www.thunderbird.net/) 128+

---

## 1. Install Ollama and pull the model

Follow the [Ollama installation guide](https://ollama.ai) for your OS, then pull the embedding model:

```bash
ollama pull nomic-embed-text
```

Verify it's running:

```bash
curl http://localhost:11434/api/tags
```

---

## 2. Clone and configure

```bash
git clone https://github.com/giovi321/thunderbird-ai-search.git
cd thunderbird-ai-search
cp config.example.yaml config.yaml
```

Edit `config.yaml` with your IMAP account details. At minimum:

- `accounts[0].host` — your IMAP server (e.g. `imap.gmail.com`)
- `accounts[0].username` — your email address
- `accounts[0].password` — your password or [app password](https://support.google.com/accounts/answer/185833)

For Gmail you must use an [App Password](https://support.google.com/accounts/answer/185833), not your regular password.

See the [Configuration](/thunderbird-ai-search/getting-started/configuration/) page for the full list of settings.

---

## 3. Start the server

```bash
docker compose up -d
```

This starts two containers:

- **qdrant** — vector database on port 6333
- **server** — indexer + search API on port 8342

Check the logs to monitor indexing progress:

```bash
docker compose logs -f server
```

Verify everything is healthy:

```bash
curl http://localhost:8342/health
# {"qdrant":"ok","ollama":"ok"}
```

---

## 4. Install the Thunderbird addon

Package and install:

```bash
cd addon
zip -r ../ai-email-search.xpi *
cd ..
```

1. Open Thunderbird
2. Go to **Tools → Add-ons and Themes**
3. Click the gear icon → **Install Add-on From File…**
4. Select `ai-email-search.xpi`

For development (temporary install, resets on restart):

1. Go to **Tools → Developer Tools → Debug Add-ons**
2. Click **Load Temporary Add-on**
3. Select `addon/manifest.json`

---

## 5. Configure the addon and search

1. Click the magnifying glass icon in the Thunderbird toolbar
2. Click the gear icon (top right) to open settings
3. Set the **Server URL** (default: `http://localhost:8342`)
4. Set the **API Key** if you configured one in `config.yaml`
5. Click **Test Connection** to verify

Then click the magnifying glass icon, type a natural language query like _"invoice from last month"_ or _"meeting notes about the project deadline"_, and press Enter. Click any result to open the email directly in Thunderbird.
