# Решение ошибки INVALID_CLIENT: Invalid client

## Описание проблемы

Ошибка `INVALID_CLIENT: Invalid client` возникает при попытке авторизации через Spotify OAuth. Это означает, что Spotify API не может проверить ваши учетные данные (Client ID и Client Secret) или Redirect URI не совпадает с настройками.

---

## Причины ошибки

1. **Отсутствуют или неверные Client ID и Client Secret**
2. **Redirect URI не совпадает с настройками в Spotify Dashboard**
3. **Неправильная кодировка base64 при создании Authorization заголовка**
4. **Приложение не активировано в Spotify Developer Dashboard**

---

## Пошаговое решение

### Шаг 1: Проверка переменных окружения

Убедитесь, что в файле `.env` (в корне проекта) правильно указаны учетные данные:

```env
SPOTIFY_CLIENT_ID=ваш_client_id
SPOTIFY_CLIENT_SECRET=ваш_client_secret
SPOTIFY_REDIRECT_URI=http://127.0.0.1:5000/callback
```

**Важно:**
- Убедитесь, что нет лишних пробелов
- Убедитесь, что значения в кавычках (или без, зависит от парсера)
- Проверьте, что файл называется именно `.env` (не `.env.example`)

### Шаг 2: Получение учетных данных Spotify

Если у вас нет Client ID и Client Secret:

1. Перейдите на [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. Войдите в свой аккаунт Spotify
3. Нажмите "Create app" или выберите существующее приложение
4. Заполните форму:
   - **App name**: название вашего приложения
   - **App description**: описание
   - **Redirect URI**: `http://127.0.0.1:5000/callback`
   - **Website**: (опционально) URL вашего сайта
5. Нажмите "Save"
6. Скопируйте **Client ID** и **Client Secret**

### Шаг 3: Настройка Redirect URI

**КРИТИЧЕСКИЕ МОМЕНТЫ:**

1. **Redirect URI должен точно совпадать** в трех местах:
   - В Spotify Developer Dashboard (настройки приложения)
   - В файле `.env` (переменная `SPOTIFY_REDIRECT_URI`)
   - В коде приложения (используется автоматически из `.env`)

2. **Для локальной разработки используйте:**
   ```
   http://127.0.0.1:5000/callback
   ```
   или
   ```
   http://localhost:5000/callback
   ```

3. **Для production используйте:**
   ```
   https://ваш-домен.com/callback
   ```

4. **Можно добавить несколько Redirect URI** (через запятую в Spotify Dashboard)

### Шаг 4: Проверка настроек приложения в Spotify Dashboard

1. Откройте ваше приложение в [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. Убедитесь, что:
   - Приложение **активировано** (Status: Active)
   - Redirect URI указан правильно: `http://127.0.0.1:5000/callback`
   - Client ID и Client Secret скопированы правильно

### Шаг 5: Проверка кода авторизации

Убедитесь, что в файле `app.py` правильно используется base64 кодирование:

```python
# Строка 2960 в app.py
auth_str = f"{CLIENT_ID}:{CLIENT_SECRET}"
b64_auth = base64.b64encode(auth_str.encode()).decode()
```

Проверьте, что `CLIENT_ID` и `CLIENT_SECRET` не `None`:

```python
# Добавьте проверку в начало функции callback
if not CLIENT_ID or not CLIENT_SECRET:
    flash("Ошибка: SPOTIFY_CLIENT_ID или SPOTIFY_CLIENT_SECRET не настроены", "error")
    return redirect('/')
```

### Шаг 6: Перезапуск приложения

После изменения `.env` файла:

1. **Остановите** приложение (Ctrl+C)
2. **Запустите** заново:
   ```bash
   python app.py
   ```

Примечание: Если используете IDE, перезапустите сервер через интерфейс IDE.

---

## Диагностика проблемы

### Проверка 1: Вывод переменных окружения

Добавьте временный код в `app.py` для проверки (после строки 72):

```python
# Временная отладка (удалите после проверки)
print(f"DEBUG: CLIENT_ID = {CLIENT_ID}")
print(f"DEBUG: CLIENT_SECRET = {'*' * len(CLIENT_SECRET) if CLIENT_SECRET else 'None'}")
print(f"DEBUG: REDIRECT_URI = {REDIRECT_URI}")
```

Запустите приложение и проверьте вывод. Если значения `None`, значит переменные окружения не загружаются.

### Проверка 2: Проверка запроса к Spotify

Добавьте отладку в функцию `callback` (после строки 2962):

```python
# Временная отладка
print(f"DEBUG: Response status = {resp.status_code}")
print(f"DEBUG: Response body = {resp.text}")
if resp.status_code != 200:
    print(f"DEBUG: Error = {resp.json()}")
```

Это поможет увидеть точную ошибку от Spotify API.

---

## Частые ошибки и решения

### Ошибка 1: "redirect_uri_mismatch"

**Причина:** Redirect URI в запросе не совпадает с настройками в Spotify Dashboard.

**Решение:**
- Проверьте, что в Spotify Dashboard указан точно такой же URI
- Убедитесь, что нет лишних слешей `/`
- Проверьте протокол (`http` vs `https`)

### Ошибка 2: "invalid_client_secret"

**Причина:** Client Secret неверный или истек.

**Решение:**
- Скопируйте Client Secret заново из Spotify Dashboard
- Проверьте, что нет лишних пробелов при копировании
- Сгенерируйте новый Client Secret (если доступно)

### Ошибка 3: "invalid_client"

**Причина:** Client ID неверный или приложение не активировано.

**Решение:**
- Проверьте Client ID в Spotify Dashboard
- Убедитесь, что приложение имеет статус "Active"
- Создайте новое приложение, если старое было удалено

---

## Пример правильного .env файла

```env
# Spotify API
SPOTIFY_CLIENT_ID=abc123def456ghi789jkl012mno345pqr678
SPOTIFY_CLIENT_SECRET=XYZ789ABC123DEF456GHI789JKL012MNO345PQR678
SPOTIFY_REDIRECT_URI=http://127.0.0.1:5000/callback
SPOTIFY_SCOPE=user-read-recently-played user-top-read

# Flask
FLASK_SECRET_KEY=your-secret-key-here

# PostgreSQL
POSTGRES_DBNAME=spotify_project
POSTGRES_USER=postgres
POSTGRES_PASSWORD=password
POSTGRES_HOST=localhost
POSTGRES_PORT=5432

# Snowflake
SNOWFLAKE_USER=your_snowflake_user
SNOWFLAKE_PASSWORD=your_snowflake_password
SNOWFLAKE_ACCOUNT=your_account
SNOWFLAKE_WAREHOUSE=COMPUTE_WH
SNOWFLAKE_DATABASE=SPOTIFY_DB
SNOWFLAKE_SCHEMA=PUBLIC

# Email (для 2FA)
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your_email@gmail.com
SMTP_PASSWORD=your_app_password
FROM_EMAIL=your_email@gmail.com
FROM_NAME=Spotify Analytics
```

---

## Дополнительные советы

1. **Используйте разные приложения для dev и production:**
   - Dev: `http://127.0.0.1:5000/callback`
   - Production: `https://yourdomain.com/callback`

2. **Храните `.env` в безопасности:**
   - Добавьте `.env` в `.gitignore` (уже должно быть)
   - Никогда не коммитьте `.env` в git
   - Используйте `.env.example` для шаблона (без реальных значений)

3. **Для production:**
   - Используйте переменные окружения сервера (вместо файла `.env`)
   - Используйте HTTPS для всех Redirect URI
   - Настройте правильный домен в Spotify Dashboard

---

## Если проблема не решена

1. Проверьте логи приложения на наличие дополнительных ошибок
2. Убедитесь, что используете актуальную версию библиотек:
   ```bash
   pip install --upgrade requests
   ```
3. Попробуйте создать новое приложение в Spotify Dashboard
4. Проверьте, что ваш IP не заблокирован Spotify API
5. Убедитесь, что используете правильный тип приложения (Web API, не другие)

---

*После решения проблемы удалите временные отладочные print'ы из кода*
