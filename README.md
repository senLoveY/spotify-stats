<div align="center">

<img src="assets/spotify-logo.svg" alt="Spotify" width="80" />

# Spotify Analytics

**Веб-приложение для анализа музыкальных предпочтений на основе Spotify API**

[![Python](https://img.shields.io/badge/Python-3.9+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Flask](https://img.shields.io/badge/Flask-3.0+-000000?style=flat-square&logo=flask&logoColor=white)](https://flask.palletsprojects.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15-4169E1?style=flat-square&logo=postgresql&logoColor=white)](https://postgresql.org)
[![Snowflake](https://img.shields.io/badge/Snowflake-Analytics-29B5E8?style=flat-square&logo=snowflake&logoColor=white)](https://snowflake.com)
[![License](https://img.shields.io/badge/License-Apache%202.0-green?style=flat-square)](LICENSE)

</div>

---

## О проекте

Spotify Analytics — это Flask-приложение, которое подключается к вашему аккаунту Spotify и показывает персональную статистику прослушиваний: топ-треки и артистов, распределение жанров, активность по часам и дням недели, рекомендации и «музыкальный вайб».

Архитектура разделяет операционные данные (PostgreSQL) и аналитику (Snowflake):

```
┌──────────────┐     ┌─────────────┐     ┌──────────────┐
│   Браузер    │────▶│  Flask App  │────▶│  PostgreSQL  │  пользователи, сессии, логи
│  (Jinja2)    │     │   app.py    │     └──────────────┘
└──────────────┘     │             │     ┌──────────────┐
                     │             │────▶│  Snowflake   │  треки, артисты, история
                     │             │     └──────────────┘
                     │             │     ┌──────────────┐
                     └─────────────┘────▶│ Spotify API  │
                                         └──────────────┘
```

## Возможности

| Раздел | Описание |
|--------|----------|
| **Дашборд** | Графики активности, топ-треки, жанры, heatmap прослушиваний |
| **Рекомендации** | Персональные рекомендации артистов на основе истории |
| **Профиль** | Настройки, привязка Spotify, смена пароля и email |
| **Отчёты** | Отправка PDF-отчёта с графиками на email |
| **Админ-панель** | Управление пользователями, обратная связь, экспорт/импорт Excel |
| **Безопасность** | 2FA по email, сброс пароля, аудит действий, роли user/admin |

## Быстрый старт

### 1. Клонирование и зависимости

```bash
git clone <repo-url>
cd spotify-stats
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Переменные окружения

```bash
cp .env.example .env
```

Заполните `.env` — минимум нужны `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` и параметры PostgreSQL.

### 3. Spotify Developer App

1. Откройте [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. Создайте приложение
3. В **Redirect URIs** добавьте `http://127.0.0.1:5000/callback`
4. Скопируйте Client ID и Client Secret в `.env`

### 4. База данных

```bash
docker compose up -d
```

Поднимаются PostgreSQL (порт `5432`) и pgAdmin (порт `5055`).

| Сервис | URL | Логин | Пароль |
|--------|-----|-------|--------|
| pgAdmin | http://localhost:5055 | admin@admin.com | root |
| PostgreSQL | localhost:5432 | postgres | password |

### 5. Запуск

```bash
python app.py
```

Приложение доступно на **http://127.0.0.1:5000**

При первом запуске автоматически создаются таблицы и учётные записи:

| Логин | Пароль | Роль |
|-------|--------|------|
| `admin` | `admin` | Администратор |
| `tatyanka` | `tatyanka` | Пользователь |

## Структура проекта

```
spotify-stats/
├── app.py                 # Основное приложение Flask
├── docker-compose.yml     # PostgreSQL + pgAdmin
├── requirements.txt       # Python-зависимости
├── .env.example           # Шаблон конфигурации
├── assets/
│   └── spotify-logo.svg
└── templates/             # HTML-шаблоны
    ├── base.html
    ├── index.html
    ├── dashboard.html
    ├── recommendations.html
    ├── profile.html
    ├── admin.html
    └── ...
```

## Конфигурация

### Spotify

| Переменная | Описание |
|------------|----------|
| `SPOTIFY_CLIENT_ID` | Client ID из Dashboard |
| `SPOTIFY_CLIENT_SECRET` | Client Secret |
| `SPOTIFY_REDIRECT_URI` | Callback URL (по умолчанию `http://127.0.0.1:5000/callback`) |
| `SPOTIFY_SCOPE` | OAuth scopes |

### Snowflake (опционально)

Без Snowflake дашборд и рекомендации работать не будут. Настройте `SNOWFLAKE_*` переменные в `.env`.

### Email (опционально)

Для 2FA, сброса пароля и email-отчётов настройте `SMTP_*` переменные.

## Устранение неполадок

### `INVALID_CLIENT: Invalid client`

Ошибка Spotify OAuth. Проверьте:

1. `SPOTIFY_CLIENT_ID` и `SPOTIFY_CLIENT_SECRET` в `.env` без лишних пробелов
2. Redirect URI в Dashboard **точно** совпадает с `SPOTIFY_REDIRECT_URI`
3. Приложение активно в [Spotify Dashboard](https://developer.spotify.com/dashboard)

### Не подключается PostgreSQL

```bash
docker compose ps          # контейнер запущен?
docker compose logs db   # логи
```

Убедитесь, что `POSTGRES_PASSWORD` в `.env` совпадает с `docker-compose.yml`.

### Графики не отображаются

```bash
pip install matplotlib numpy
```

## Технологии

- **Backend:** Python, Flask, Jinja2
- **Базы данных:** PostgreSQL 15, Snowflake
- **API:** Spotify Web API
- **Визуализация:** Matplotlib
- **Инфраструктура:** Docker Compose

## Лицензия

Проект распространяется под лицензией [Apache 2.0](LICENSE).
