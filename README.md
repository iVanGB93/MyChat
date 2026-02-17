# ChatConnect — Real-time Chat, Voice & Video Calls

A Django backend for instant messaging, voice calls, and video calls — powered by Django Channels (WebSockets) and LiveKit (WebRTC).

## Tech Stack

| Layer | Technology |
|---|---|
| Framework | Django 5+ / Django REST Framework |
| Auth | JWT (SimpleJWT) |
| Real-time chat | Django Channels + Redis |
| Voice / Video | LiveKit (WebRTC) |
| Database | PostgreSQL |
| Task queue | Celery + Redis |
| ASGI server | Daphne |

## Project Structure

```
Comm-app/
├── config/           # Django project settings, ASGI, Celery, URLs
├── users/            # Registration, login, profiles, contacts
├── chat/             # Chat rooms, messages, WebSocket consumers
├── calls/            # LiveKit token generation, call history
├── docker-compose.yml
├── requirements.txt
└── .env
```

## Quick Start

### 1. Prerequisites

- Python 3.11+
- Docker & Docker Compose (for PostgreSQL, Redis, LiveKit)

### 2. Start Services

```bash
docker compose up -d
```

This starts PostgreSQL, Redis, and a local LiveKit server.

### 3. Configure Environment

Copy `.env.example` to `.env` and update values as needed:

```bash
cp .env.example .env
```

### 4. Install Dependencies

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 5. Run Migrations

```bash
python manage.py migrate
python manage.py createsuperuser
```

### 6. Run the Server

```bash
# Development (Daphne ASGI server — supports HTTP + WebSocket)
python manage.py runserver
```

### 7. Start Celery Worker (separate terminal)

```bash
celery -A config worker -l info
```

## API Endpoints

### Authentication
| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/users/register/` | Register new user |
| POST | `/api/users/token/` | Obtain JWT token pair |
| POST | `/api/users/token/refresh/` | Refresh access token |

### Users & Contacts
| Method | Endpoint | Description |
|---|---|---|
| GET/PUT | `/api/users/profile/` | Get/update own profile |
| GET | `/api/users/search/?q=name` | Search users |
| GET/POST | `/api/users/contacts/` | List/add contacts |
| DELETE | `/api/users/contacts/{id}/` | Remove contact |

### Chat
| Method | Endpoint | Description |
|---|---|---|
| GET/POST | `/api/chat/rooms/` | List/create chat rooms |
| GET | `/api/chat/rooms/{id}/` | Get room details |
| GET | `/api/chat/rooms/{id}/messages/` | Paginated message history |

### Calls
| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/calls/initiate/` | Start a call (returns LiveKit token) |
| POST | `/api/calls/{id}/join/` | Accept a call (returns LiveKit token) |
| POST | `/api/calls/{id}/end/` | End or reject a call |
| GET | `/api/calls/history/` | Call history |

## WebSocket Endpoints

### Chat
```
ws://<host>/ws/chat/<room_id>/
```
Send: `{"message": "Hello!", "message_type": "text"}`
Receive: `{"id": "...", "sender": "username", "content": "Hello!", ...}`

### Notifications (incoming calls, etc.)
```
ws://<host>/ws/notifications/
```
Receive: `{"event": "incoming_call", "caller": "username", "room_name": "...", ...}`

## Frontend Integration

This backend is designed to work with:
- **React** (web) — connect via REST API + WebSocket + LiveKit React SDK
- **React Native** (mobile) — connect via REST API + WebSocket + LiveKit React Native SDK

Use the JWT token from `/api/users/token/` for both REST API (`Authorization: Bearer <token>`) and WebSocket authentication.

## License

MIT
