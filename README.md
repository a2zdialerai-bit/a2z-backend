# A2Z Dialer Backend

FastAPI backend for A2Z Dialer.

## Features

- JWT auth
- Multi-tenant workspaces
- Lead lists + CSV upload
- Leads
- Pathways (deterministic JSON router)
- Campaigns
- Call logs
- Appointments
- DNC management
- Twilio outbound calling
- Twilio Gather fallback
- Twilio Media Streams websocket scaffold
- Google Calendar OAuth + event creation
- Calendly fallback
- Stripe scaffold
- Worker/autopilot tick endpoints
- Railway deploy config

## Project structure

```text
backend/
  __init__.py
  auth.py
  billing.py
  calendar_sync.py
  classifier.py
  config.py
  db.py
  main.py
  models.py
  notifications.py
  pathway_engine.py
  realtime_bridge.py
  requirements.txt
  schemas.py
  twilio_voice.py
  worker.py
  .env.example
  railway.json