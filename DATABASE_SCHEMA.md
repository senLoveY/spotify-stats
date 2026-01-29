# Описание структуры базы данных PostgreSQL

## База данных: `spotify_project`

---

## 📊 ТАБЛИЦЫ (Tables)

### 1. **users** - Пользователи системы

**Назначение:** Хранит информацию о зарегистрированных пользователях системы.

| Поле | Тип данных | Ограничения | Описание |
|------|------------|-------------|----------|
| `id` | SERIAL | PRIMARY KEY | Уникальный идентификатор пользователя |
| `username` | VARCHAR(100) | UNIQUE, NOT NULL | Логин пользователя (уникальный) |
| `password_hash` | VARCHAR(255) | DEFAULT NULL | Хэш пароля (может быть NULL для Spotify-пользователей) |
| `email` | VARCHAR(255) | UNIQUE, DEFAULT NULL | Email адрес (уникальный, может быть NULL) |
| `spotify_id` | VARCHAR(100) | UNIQUE, DEFAULT NULL | Spotify ID пользователя (уникальный, может быть NULL) |
| `display_name` | VARCHAR(150) | DEFAULT NULL | Отображаемое имя пользователя |
| `role` | VARCHAR(20) | DEFAULT 'user', CHECK ('user' OR 'admin') | Роль пользователя: 'user' или 'admin' |
| `is_blocked` | BOOLEAN | DEFAULT FALSE | Флаг блокировки пользователя |
| `email_verified` | BOOLEAN | DEFAULT FALSE | Флаг подтверждения email адреса |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | Дата и время регистрации |

**Ограничения:**
- `chk_users_role` - проверка, что роль может быть только 'user' или 'admin'
- `chk_users_email_format` - проверка формата email (регулярное выражение)

**Индексы:** Нет явных индексов, но есть UNIQUE ограничения на `username`, `email`, `spotify_id`

**Связи:**
- Связана со многими таблицами через внешние ключи (user_sessions, feedback, audit_logs, user_preferences, verification_codes)

---

### 2. **verification_codes** - Коды верификации для 2FA

**Назначение:** Хранит коды подтверждения для двухфакторной аутентификации и регистрации.

| Поле | Тип данных | Ограничения | Описание |
|------|------------|-------------|----------|
| `id` | SERIAL | PRIMARY KEY | Уникальный идентификатор записи |
| `user_id` | INTEGER | REFERENCES users(id) ON DELETE CASCADE, DEFAULT NULL | ID пользователя (может быть NULL) |
| `email` | VARCHAR(255) | NOT NULL | Email адрес для отправки кода |
| `code` | VARCHAR(6) | NOT NULL, CHECK (LENGTH(code) = 6) | 6-значный код подтверждения |
| `purpose` | VARCHAR(50) | NOT NULL, DEFAULT 'registration' | Назначение кода (например, 'registration', '2fa') |
| `expires_at` | TIMESTAMP | NOT NULL | Дата и время истечения кода |
| `used` | BOOLEAN | DEFAULT FALSE | Флаг использования кода |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | Дата и время создания кода |

**Ограничения:**
- `chk_code_expires_after_created` - проверка, что expires_at > created_at
- `chk_code_length` - проверка, что длина кода равна 6 символам

**Индексы:** Нет явных индексов

---

### 3. **audit_logs** - Логи аудита

**Назначение:** Журнал всех действий пользователей в системе для аудита и отладки.

| Поле | Тип данных | Ограничения | Описание |
|------|------------|-------------|----------|
| `id` | SERIAL | PRIMARY KEY | Уникальный идентификатор записи |
| `user_id` | INTEGER | REFERENCES users(id), DEFAULT NULL | ID пользователя, выполнившего действие |
| `action` | VARCHAR(100) | NOT NULL | Тип действия (например, 'LOGIN', 'LOGOUT', 'UPDATE_FEEDBACK') |
| `details` | TEXT | DEFAULT NULL | Дополнительная информация о действии |
| `timestamp` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | Дата и время действия |

**Индексы:** Нет явных индексов

**Примечание:** Используется для отслеживания всех важных действий пользователей в системе.

---

### 4. **user_sessions** - Сессии пользователей

**Назначение:** Хранит информацию о активных и неактивных сессиях пользователей.

| Поле | Тип данных | Ограничения | Описание |
|------|------------|-------------|----------|
| `id` | SERIAL | PRIMARY KEY | Уникальный идентификатор сессии |
| `user_id` | INTEGER | REFERENCES users(id) ON DELETE CASCADE | ID пользователя |
| `session_token` | VARCHAR(255) | UNIQUE, NOT NULL | Уникальный токен сессии |
| `ip_address` | VARCHAR(45) | DEFAULT NULL | IP адрес пользователя (поддерживает IPv6) |
| `user_agent` | TEXT | DEFAULT NULL | User Agent браузера |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | Дата и время создания сессии |
| `expires_at` | TIMESTAMP | NOT NULL | Дата и время истечения сессии |
| `is_active` | BOOLEAN | DEFAULT TRUE | Флаг активности сессии |

**Ограничения:**
- `chk_session_expires_after_created` - проверка, что expires_at > created_at

**Индексы:** Нет явных индексов, но есть UNIQUE ограничение на `session_token`

---

### 5. **user_preferences** - Настройки пользователей

**Назначение:** Хранит пользовательские настройки и предпочтения.

| Поле | Тип данных | Ограничения | Описание |
|------|------------|-------------|----------|
| `id` | SERIAL | PRIMARY KEY | Уникальный идентификатор записи |
| `user_id` | INTEGER | REFERENCES users(id) ON DELETE CASCADE, UNIQUE | ID пользователя (уникальный, один набор настроек на пользователя) |
| `language` | VARCHAR(10) | DEFAULT 'ru', CHECK ('ru' OR 'en' OR 'uk') | Язык интерфейса |
| `notifications_enabled` | BOOLEAN | DEFAULT TRUE | Включены ли уведомления |
| `email_notifications` | BOOLEAN | DEFAULT TRUE | Включены ли email уведомления |
| `default_view` | VARCHAR(50) | DEFAULT 'dashboard', CHECK ('dashboard' OR 'recommendations' OR 'profile') | Вид по умолчанию при входе |
| `timezone` | VARCHAR(50) | DEFAULT 'UTC' | Часовой пояс пользователя |
| `updated_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | Дата и время последнего обновления |

**Индексы:** Нет явных индексов, но есть UNIQUE ограничение на `user_id`

---

### 6. **feedback** - Обратная связь

**Назначение:** Хранит отзывы, жалобы, запросы функций и другую обратную связь от пользователей.

| Поле | Тип данных | Ограничения | Описание |
|------|------------|-------------|----------|
| `id` | SERIAL | PRIMARY KEY | Уникальный идентификатор обратной связи |
| `user_id` | INTEGER | REFERENCES users(id) ON DELETE SET NULL, DEFAULT NULL | ID пользователя (может быть NULL для анонимных) |
| `feedback_type` | VARCHAR(50) | NOT NULL, DEFAULT 'other', CHECK ('bug' OR 'feature_request' OR 'complaint' OR 'praise' OR 'other') | Тип обратной связи |
| `title` | VARCHAR(255) | NOT NULL | Заголовок обратной связи |
| `message` | TEXT | NOT NULL | Текст сообщения |
| `page_url` | VARCHAR(500) | DEFAULT NULL | URL страницы, с которой отправлена обратная связь |
| `status` | VARCHAR(20) | DEFAULT 'new', CHECK ('new' OR 'reviewed' OR 'in_progress' OR 'resolved' OR 'rejected') | Статус обработки |
| `priority` | VARCHAR(10) | DEFAULT NULL, CHECK ('low' OR 'medium' OR 'high' OR 'critical') | Приоритет обратной связи |
| `admin_notes` | TEXT | DEFAULT NULL | Заметки администратора |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | Дата и время создания |
| `updated_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | Дата и время последнего обновления |

**Ограничения:**
- `chk_feedback_updated_after_created` - проверка, что updated_at >= created_at
- `chk_feedback_title_length` - проверка, что длина заголовка от 1 до 255 символов
- `chk_feedback_message_length` - проверка, что сообщение не пустое

**Индексы:**
- `idx_feedback_user_id` - индекс по user_id
- `idx_feedback_status` - индекс по status
- `idx_feedback_type` - индекс по feedback_type
- `idx_feedback_created_at` - индекс по created_at (DESC)

---

### 7. **users_history** - История изменений пользователей

**Назначение:** Журнал всех изменений в таблице users (триггерная таблица для аудита).

| Поле | Тип данных | Ограничения | Описание |
|------|------------|-------------|----------|
| `id` | SERIAL | PRIMARY KEY | Уникальный идентификатор записи |
| `user_id` | INTEGER | - | ID пользователя (без внешнего ключа, так как пользователь может быть удален) |
| `operation_date` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | Дата и время операции |
| `operation_type` | VARCHAR(10) | NOT NULL, CHECK ('INSERT' OR 'UPDATE' OR 'DELETE') | Тип операции |
| `old_value` | JSONB | DEFAULT NULL | Старое значение записи (JSON) |
| `new_value` | JSONB | DEFAULT NULL | Новое значение записи (JSON) |
| `username` | VARCHAR(100) | DEFAULT NULL | Имя пользователя, выполнившего операцию |
| `changed_by_user_id` | INTEGER | REFERENCES users(id), DEFAULT NULL | ID пользователя, выполнившего изменение |

**Индексы:**
- `idx_users_history_user_id` - индекс по user_id
- `idx_users_history_operation_date` - индекс по operation_date (DESC)

**Триггер:** `trigger_users_history` - автоматически создает запись при INSERT, UPDATE, DELETE в таблице users

---

### 8. **feedback_history** - История изменений обратной связи

**Назначение:** Журнал всех изменений в таблице feedback (триггерная таблица для аудита).

| Поле | Тип данных | Ограничения | Описание |
|------|------------|-------------|----------|
| `id` | SERIAL | PRIMARY KEY | Уникальный идентификатор записи |
| `feedback_id` | INTEGER | - | ID обратной связи (без внешнего ключа) |
| `operation_date` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | Дата и время операции |
| `operation_type` | VARCHAR(10) | NOT NULL, CHECK ('INSERT' OR 'UPDATE' OR 'DELETE') | Тип операции |
| `old_value` | JSONB | DEFAULT NULL | Старое значение записи (JSON) |
| `new_value` | JSONB | DEFAULT NULL | Новое значение записи (JSON) |
| `username` | VARCHAR(100) | DEFAULT NULL | Имя пользователя, выполнившего операцию |
| `changed_by_user_id` | INTEGER | REFERENCES users(id), DEFAULT NULL | ID пользователя, выполнившего изменение |

**Индексы:**
- `idx_feedback_history_feedback_id` - индекс по feedback_id
- `idx_feedback_history_operation_date` - индекс по operation_date (DESC)

**Триггер:** `trigger_feedback_history` - автоматически создает запись при INSERT, UPDATE, DELETE в таблице feedback

---

## 👁️ ПРЕДСТАВЛЕНИЯ (Views)

### 1. **v_active_users_with_sessions** - Активные пользователи с сессиями

**Назначение:** Предоставляет информацию об активных (не заблокированных) пользователях и их активных сессиях.

**Колонки:**
- `id`, `username`, `display_name`, `email`, `role`, `is_blocked`, `email_verified`, `created_at` (из users)
- `active_sessions_count` - количество активных сессий
- `last_session_date` - дата последней сессии
- `latest_session_expires` - дата истечения последней сессии

**Условие:** Только пользователи с `is_blocked = FALSE`

---

### 2. **v_feedback_with_users** - Обратная связь с информацией о пользователях

**Назначение:** Объединяет данные feedback с информацией о пользователях.

**Колонки:**
- Все поля из `feedback`
- `user_id`, `username`, `display_name`, `email`, `user_role` (из users)
- `status_order` - числовой порядок статуса для сортировки

**Сортировка:** По дате создания (DESC)

---

### 3. **v_user_statistics** - Статистика по пользователям

**Назначение:** Предоставляет статистику по каждому пользователю.

**Колонки:**
- `id`, `username`, `display_name`, `role`, `registration_date` (из users)
- `total_sessions` - общее количество сессий
- `active_sessions` - количество активных сессий
- `feedback_count` - количество отправленных обратных связей
- `new_feedback_count` - количество новых обратных связей
- `audit_logs_count` - количество записей в логах аудита
- `last_login_date` - дата последнего входа

---

### 4. **v_recent_user_activity** - Последние действия пользователей

**Назначение:** Объединяет информацию о сессиях и действиях пользователей.

**Колонки:**
- `activity_type` - тип активности ('session' или 'audit')
- `activity_id` - ID записи
- `user_id`, `username`, `display_name`
- `action_description` - описание действия
- `activity_date` - дата действия
- `ip_address`, `user_agent` - информация о подключении

**Ограничение:** Последние 1000 записей

**Сортировка:** По дате действия (DESC)

---

## ⚙️ ФУНКЦИИ И ТРИГГЕРЫ

### Функции:

1. **log_users_changes()**
   - Тип: TRIGGER FUNCTION
   - Назначение: Логирует изменения в таблице users в users_history
   - Использует переменную сессии `app.current_user_id` для определения пользователя, выполнившего изменение

2. **log_feedback_changes()**
   - Тип: TRIGGER FUNCTION
   - Назначение: Логирует изменения в таблице feedback в feedback_history
   - Использует переменную сессии `app.current_user_id` для определения пользователя, выполнившего изменение

### Триггеры:

1. **trigger_users_history**
   - Таблица: `users`
   - События: AFTER INSERT, UPDATE, DELETE
   - Функция: `log_users_changes()`

2. **trigger_feedback_history**
   - Таблица: `feedback`
   - События: AFTER INSERT, UPDATE, DELETE
   - Функция: `log_feedback_changes()`

---

## 🔗 СВЯЗИ МЕЖДУ ТАБЛИЦАМИ

```
users (id)
  ├── verification_codes (user_id) → CASCADE DELETE
  ├── audit_logs (user_id) → SET NULL
  ├── user_sessions (user_id) → CASCADE DELETE
  ├── user_preferences (user_id) → CASCADE DELETE, UNIQUE
  ├── feedback (user_id) → SET NULL
  ├── users_history (changed_by_user_id) → SET NULL
  └── feedback_history (changed_by_user_id) → SET NULL
```

---

## 📝 ПРИМЕЧАНИЯ

1. **Пароли:** Хранятся в виде хэшей (password_hash), может быть NULL для пользователей, зарегистрированных через Spotify
2. **Аудит:** Все изменения в users и feedback автоматически логируются через триггеры
3. **Сессии:** Используется токен-подход для управления сессиями
4. **Обратная связь:** Поддерживает анонимные обращения (user_id может быть NULL)
5. **Многоязычность:** Поддерживаются языки: ru, en, uk
6. **Роли:** Система ролей - 'user' и 'admin'

---

*Документация создана на основе анализа кода приложения*



